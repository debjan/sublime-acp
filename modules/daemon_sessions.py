"""Daemon session operations - list/switch/new/fork/adopt/rename over live connections."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Callable

import sublime

from ..protocol import (
    ACPError,
    acp_log,
    fork_session,
    list_sessions,
    load_session_with_replay,
    new_session,
)
from . import broadcast, cache, ui
from .config import STATUS_KEY_USAGE, TOOL_CALLS_DEFAULT
from .config import settings as load_settings
from .daemon import (
    _ACP_TRANSPORT,
    _apply_switched_config,
    _build_env,
    _cache_dir,
    _extract_config_update,
    _make_commands_updater,
    _make_session_info_updater,
    _make_usage_updater,
    _reconnect_and_resume,
    _refresh_daemon_status,
    _resume_on_conn,
    _update_agent_session_id,
    _window_for_state,
    get_state,
)
from .rpc import (
    _extract_available_commands,
    _extract_session_info_update,
    _extract_usage_update,
    _replay_text,
    format_replay_update,
)


def list_daemon_sessions(window_id: int, on_done: Callable) -> None:
    """List sessions on the running daemon's live connection.

    Calls *on_done(sessions, supported)* on the main thread. *sessions* is
    ``None`` when the request failed; *supported* is ``False`` when the agent
    does not advertise ``session/list``.
    """
    state = get_state(window_id)
    if state is None or not state.is_running():
        ui.on_main(lambda: on_done(None, False))
        return
    conn = state.get('conn')
    loop = state.get('loop')
    work_dir = state.get('work_dir')
    if conn is None or loop is None or loop.is_closed():
        ui.on_main(lambda: on_done(None, False))
        return

    if not state.supports('list'):
        ui.on_main(lambda: on_done(None, False))
        return

    limit = load_settings().get('session_list_limit', 10) or 10

    async def _fetch():
        sessions: list = []
        cursor = None
        while len(sessions) < limit:
            page, cursor = await list_sessions(conn, cwd=work_dir, cursor=cursor)
            if not page:
                break
            sessions.extend(page)
            if not cursor:
                break
        return sessions[:limit]

    try:
        future = asyncio.run_coroutine_threadsafe(_fetch(), loop)
    except RuntimeError:
        ui.on_main(lambda: on_done(None, False))
        return

    def _done(f):
        try:
            sessions = f.result()
            ui.on_main(lambda: on_done(sessions, True))
        except ACPError as exc:
            supported = getattr(exc, 'code', None) != -32601
            acp_log('switch_session', f'session/list failed: {exc!r}')
            ui.on_main(lambda: on_done(None, supported))
        except Exception as exc:
            acp_log('switch_session', f'session/list failed: {exc!r}')
            ui.on_main(lambda: on_done(None, True))

    future.add_done_callback(_done)


def _cache_session_title(cache_dir: Path, cmd: list, session_id: str, title: str | None) -> None:
    """Record or clear the local title override for *session_id*."""
    try:
        cache.set_session_title(cache_dir, cmd, session_id, title)
    except Exception as exc:
        acp_log('daemon', f'failed to cache session title: {exc!r}')


def rename_daemon_session(window_id: int, title: str, on_done: Callable | None = None) -> None:
    """Store a local title override for the daemon's current session.

    ACP has no client-to-agent session rename method, so this records the
    title client-side only; the agent's own store keeps its title. The
    override is dropped if the agent later reports a title for the session.
    """
    ctx = _require_idle_daemon(
        window_id, 'ACP: Wait for the current prompt to finish before renaming', on_done,
    )
    if ctx is None:
        return
    state, _conn, _loop, cmd, _env, _work_dir = ctx
    session_id = state.get('session_id')
    if not session_id or not cmd:
        sublime.status_message('ACP: No active session to rename')
        if on_done is not None:
            on_done(False, 'No active session')
        return

    label = ' '.join(title.split())
    _cache_session_title(_cache_dir(), cmd, session_id, label or None)
    state.patch_session(session_id, 'title_override', label)
    acp_log('daemon', f'local title set to "{label}" for sid={session_id}', window_id)
    sublime.status_message(f'ACP: Session renamed to "{label or session_id}"')
    if on_done is not None:
        on_done(True)


def get_daemon_session_title(window_id: int) -> str:
    """Return the current session's local title override, or ``''``."""
    state = get_state(window_id)
    if state is None or not state.is_running():
        return ''
    session_id = state.get('session_id')
    cmd = state.get('agent_cmd')
    if not session_id or not cmd:
        return ''
    try:
        return cache.get_session_title_override(_cache_dir(), cmd, session_id) or ''
    except Exception as exc:
        acp_log('daemon', f'failed to read session title: {exc!r}')
        return ''


def refresh_session_cache(window_id: int) -> None:
    """Background refresh of the cached ``session/list`` result."""
    state = get_state(window_id)
    if state is None or not state.is_running() or not state.supports('list'):
        return

    def _on_done(sessions, supported):
        if sessions is None:
            return
        st = get_state(window_id)
        if st is not None and st.is_running():
            st.set(sessions_cache=sessions)

    list_daemon_sessions(window_id, _on_done)


def _require_idle_daemon(window_id: int, busy_msg: str, on_done: Callable | None):
    """Return idle daemon context or ``None`` after reporting the blocker."""
    state = get_state(window_id)
    if state is None or not state.is_running():
        sublime.status_message('ACP: No agent session running in this window')
        if on_done is not None:
            on_done(False, 'No agent session running')
        return None
    if state.get('is_busy'):
        sublime.status_message(busy_msg)
        if on_done is not None:
            on_done(False, 'Agent is busy')
        return None
    s = state.get('conn', 'loop', 'agent_cmd', 'env', 'work_dir')
    conn, loop, cmd, env, work_dir = (
        s['conn'], s['loop'], s['agent_cmd'], s['env'], s['work_dir'])
    if conn is None or loop is None or loop.is_closed():
        sublime.status_message('ACP: Daemon connection not available')
        if on_done is not None:
            on_done(False, 'Daemon connection not available')
        return None
    return state, conn, loop, cmd, env, work_dir


def _submit_daemon_task(loop, task_factory: Callable, on_done: Callable | None):
    """Submit a coroutine created by *task_factory*; return its future."""
    try:
        return asyncio.run_coroutine_threadsafe(task_factory(), loop)
    except RuntimeError:
        sublime.status_message('ACP: daemon already stopped')
        if on_done is not None:
            on_done(False, 'daemon already stopped')
        return None


def _finish_session_change(state, cmd, sid, ok, error, success_view, success_status,
                           fail_view, fail_status, on_done, replay_text=None,
                           session_prompt_pending: bool | None = None) -> None:
    if ok:
        acp_log('session_change', f'ok: sid={sid}', state.get('window_id'))
        _update_agent_session_id(cmd, sid)
        state.set(
            session_id=sid,
            usage_used=None, usage_size=None,
            has_replied=False, last_activity=time.monotonic(),
        )
        if session_prompt_pending is not None:
            state.set(session_prompt_pending=session_prompt_pending)
        refresh_session_cache(state.get('window_id'))
        output_view = state.get('output_view')
        if output_view is not None:
            if replay_text:
                ui.replace_output_view(output_view, success_view(sid) + replay_text)
                ui.append_turn_divider(output_view)
            else:
                ui.append_to_output_view(output_view, success_view(sid))
        broadcast.erase_broadcast_status(STATUS_KEY_USAGE, _window_for_state(state))
        sublime.status_message(success_status(sid))
    else:
        acp_log('session_change', f'failed: {error}', state.get('window_id'))
        output_view = state.get('output_view')
        if output_view is not None:
            ui.append_to_output_view(output_view, fail_view(error))
        sublime.status_message(fail_status(error))
    if on_done is not None:
        on_done(ok, error)


def switch_daemon_session(window_id: int, session_id: str,
                          on_done: Callable | None = None) -> None:
    """Switch the running daemon to *session_id* in place.

    Tries ``session/resume``/``session/load`` on the live connection and falls
    back to reconnecting the subprocess when the agent rejects an in-place
    switch. Calls *on_done(ok, error)* on the main thread.
    """
    ctx = _require_idle_daemon(
        window_id, 'ACP: Wait for the current prompt to finish before switching', on_done)
    if ctx is None:
        return
    state, conn, loop, cmd, env, work_dir = ctx

    replay = bool(load_settings().get('session_replay_on_switch', False))
    thoughts_mode = load_settings().get('thoughts', 'enabled')
    show_tools = load_settings().get('tool_calls', TOOL_CALLS_DEFAULT) == 'enabled'

    async def _do_switch():
        state.set(is_busy=True)
        try:
            agent_caps = state.get('agent_caps')
            if not state.supports('resume_or_load'):
                return False, 'Agent does not support session resume or load', None
            # Full-history replay: session/load replays via session/update.
            if replay and state.supports('load'):
                chunks: list[str] = []
                tool_state: dict = {}
                commands_holder: list = []
                usage_holder: list = []
                config_holder: list = []
                pending_strip = [state.get('session_prompt') or '']
                on_session_info = _make_session_info_updater(state)

                def _on_replay(method: str, params: dict) -> None:
                    if method != 'session/update' or not isinstance(params, dict):
                        return
                    update = params.get('update', {})
                    if not isinstance(update, dict):
                        return
                    im, info_sid, info_title = _extract_session_info_update(method, params)
                    if im:
                        on_session_info(info_sid, info_title)
                        return
                    matched, commands = _extract_available_commands(method, params)
                    if matched:
                        commands_holder.append(commands)
                        return
                    um, used, size = _extract_usage_update(method, params)
                    if um:
                        usage_holder.append((used, size))
                        return
                    cm, config = _extract_config_update(method, params)
                    if cm:
                        config_holder.append(config)
                        return
                    kind = update.get('sessionUpdate', update.get('type', ''))
                    if kind in ('user_message', 'user_message_chunk') and pending_strip[0]:
                        text = _replay_text(update)
                        need = pending_strip[0]
                        if text.startswith(need):
                            pending_strip[0] = ''
                            text = text[len(need):]
                        elif need.startswith(text):
                            pending_strip[0] = need[len(text):]
                            return
                        else:
                            pending_strip[0] = ''
                        if text:
                            chunks.append(f'\n> **User**: {text}\n')
                        return
                    text = format_replay_update(
                        update, thoughts_mode, show_tools, tool_state,
                    )
                    if text:
                        chunks.append(text)

                try:
                    load_result = await load_session_with_replay(conn, session_id, work_dir, _on_replay)
                    if commands_holder and commands_holder[-1] is not None:
                        _make_commands_updater(cmd)(commands_holder[-1])
                    if usage_holder:
                        _make_usage_updater(state)(*usage_holder[-1])
                    _apply_switched_config(cmd, load_result, config_holder)
                    _refresh_daemon_status(state)
                    return True, None, ''.join(chunks)
                except _ACP_TRANSPORT as exc:
                    acp_log('switch_session', f'replay load failed ({exc!r}); falling back')
            try:
                _, resume_result = await _resume_on_conn(conn, agent_caps, session_id, work_dir)
                _apply_switched_config(cmd, resume_result, None)
                _refresh_daemon_status(state)
                return True, None, None
            except _ACP_TRANSPORT as exc:
                acp_log('switch_session', f'in-place resume failed ({exc!r}); reconnecting')
            try:
                ok, err = await _reconnect_and_resume(
                    state, cmd, _build_env(env or {}), work_dir, session_id,
                )
                if ok:
                    _refresh_daemon_status(state)
                return ok, err, None
            except Exception as exc:
                acp_log('switch_session', f'reconnect failed: {exc!r}')
                return False, str(exc), None
        finally:
            state.set(is_busy=False)

    future = _submit_daemon_task(loop, _do_switch, on_done)
    if future is None:
        return

    def _done(f):
        try:
            ok, error, replay_text = f.result()
        except Exception as exc:
            acp_log('switch_session', f'switch raised: {exc!r}')
            ok, error, replay_text = False, str(exc), None

        def _apply():
            header = f'\n# Switched to session `{session_id}`\n' if (ok and replay_text) else None
            _finish_session_change(
                state, cmd, session_id, ok, error,
                lambda sid: (header if header else f'\n*[Switched to session: {sid}]*\n'),
                lambda sid: f'ACP: Switched to session {sid[-8:]}',
                lambda err: f'\n**[Could not switch session: {err or "unknown error"}]**\n',
                lambda err: f'ACP: Could not switch session: {err or "unknown error"}',
                on_done,
                replay_text=replay_text,
                session_prompt_pending=False,
            )

        ui.on_main(_apply)

    future.add_done_callback(_done)


def new_daemon_session(window_id: int, on_done: Callable | None = None) -> None:
    """Create a fresh session on the running daemon without respawning it.

    Sends ``session/new`` on the live connection and resets usage/reply
    tracking. Calls *on_done(ok, error)* on the main thread.
    """
    ctx = _require_idle_daemon(
        window_id, 'ACP: Wait for the current prompt to finish', on_done)
    if ctx is None:
        return
    state, conn, loop, cmd, _env, work_dir = ctx

    async def _do_new():
        state.set(is_busy=True)
        try:
            try:
                new_sid, _, _, _ = await new_session(
                    conn, {'cwd': work_dir, 'mcpServers': []})
            except Exception as exc:
                acp_log('new_session', f'session/new failed: {exc!r}')
                return None, str(exc)
            if not new_sid:
                return None, 'Agent did not return a sessionId'
            return new_sid, None
        finally:
            state.set(is_busy=False)

    future = _submit_daemon_task(loop, _do_new, on_done)
    if future is None:
        return

    def _done(f):
        try:
            new_sid, error = f.result()
        except Exception as exc:
            acp_log('new_session', f'new session raised: {exc!r}')
            new_sid, error = None, str(exc)
        ok = new_sid is not None

        def _apply():
            _finish_session_change(
                state, cmd, new_sid, ok, error,
                lambda sid: f'\n*[Started new session: {sid}]*\n',
                lambda sid: f'ACP: Started new session {sid[-8:]}',
                lambda err: f'\n**[Could not start new session: {err or "unknown error"}]**\n',
                lambda err: f'ACP: Could not start new session: {err or "unknown error"}',
                on_done,
                session_prompt_pending=True,
            )

        ui.on_main(_apply)

    future.add_done_callback(_done)


def fork_daemon_session(window_id: int, session_id: str | None = None,
                        on_done: Callable | None = None) -> None:
    """Fork a session via the unstable ``session/fork`` method.

    Sends ``session/fork`` for *session_id* (defaulting to the daemon's
    current session) on the live connection and switches the daemon to
    the forked session without respawning it. The fork inherits the
    source session's history, so ``session_prompt`` is not resent. Calls
    *on_done(ok, error)* on the main thread.
    """
    ctx = _require_idle_daemon(
        window_id, 'ACP: Wait for the current prompt to finish before forking', on_done)
    if ctx is None:
        return
    state, conn, loop, cmd, _env, work_dir = ctx
    source_id = session_id or state.get('session_id')
    if not source_id:
        sublime.status_message('ACP: No active session to fork')
        if on_done is not None:
            on_done(False, 'No active session')
        return
    if not state.supports('fork'):
        sublime.status_message('ACP: agent does not support session/fork')
        if on_done is not None:
            on_done(False, 'Agent does not support session/fork')
        return

    async def _do_fork():
        state.set(is_busy=True)
        try:
            try:
                new_sid, _, fork_opts, _ = await fork_session(conn, source_id, work_dir)
            except Exception as exc:
                acp_log('fork_session', f'session/fork failed: {exc!r}')
                return None, None, str(exc)
            if not new_sid:
                return None, None, 'Agent did not return a sessionId'
            return new_sid, fork_opts, None
        finally:
            state.set(is_busy=False)

    future = _submit_daemon_task(loop, _do_fork, on_done)
    if future is None:
        return

    def _done_fork(f):
        try:
            new_sid, fork_opts, error = f.result()
        except Exception as exc:
            acp_log('fork_session', f'fork session raised: {exc!r}')
            new_sid, fork_opts, error = None, None, str(exc)
        ok = new_sid is not None

        def _apply():
            if ok and fork_opts:
                _apply_switched_config(cmd, {'configOptions': fork_opts}, None)
                _refresh_daemon_status(state)
            _finish_session_change(
                state, cmd, new_sid, ok, error,
                lambda sid: f'\n*[Forked session: {sid}]*\n',
                lambda sid: f'ACP: Forked session {sid[-8:]}',
                lambda err: f'\n**[Could not fork session: {err or "unknown error"}]**\n',
                lambda err: f'ACP: Could not fork session: {err or "unknown error"}',
                on_done,
                session_prompt_pending=False,
            )

        ui.on_main(_apply)

    future.add_done_callback(_done_fork)


def adopt_manual_session(window_id: int, method: str, params: dict, result) -> None:
    """Switch the daemon to the session from a manual session-changing request.

    Manual ``session/new|load|resume|fork`` results are otherwise only
    rendered to the output view, leaving the daemon talking to the previous
    session. Mirrors the state transition of the built-in session commands:
    a fresh ``new`` session keeps ``session_prompt_pending`` so the session
    prompt is sent with the next user prompt (cf. :func:`new_daemon_session`),
    while ``load``/``resume``/``fork`` inherit history (cf.
    :func:`switch_daemon_session`, :func:`fork_daemon_session`). No-op
    unless a usable session id is found and the daemon is still running.
    Safe to call from a background thread; the transition itself runs on
    the main thread.

    Unlike the built-in switch, no transcript is replayed into the output
    view; only a one-line note is appended.
    """
    if method == 'session/new':
        new_sid = result.get('sessionId') if isinstance(result, dict) else None
        note, status, prompt_pending = (
            '\n*[Started new session: {sid}]*\n',
            'ACP: Started new session {short}',
            True,
        )
    elif method in ('session/load', 'session/resume'):
        new_sid = params.get('sessionId')
        note, status, prompt_pending = (
            '\n*[Switched to session: {sid}]*\n',
            'ACP: Switched to session {short}',
            False,
        )
    elif method == 'session/fork':
        new_sid = result.get('sessionId') if isinstance(result, dict) else None
        note, status, prompt_pending = (
            '\n*[Forked session: {sid}]*\n',
            'ACP: Forked session {short}',
            False,
        )
    else:
        return
    if not isinstance(new_sid, str) or not new_sid:
        return
    state = get_state(window_id)
    if state is None or not state.is_running():
        return
    cmd = state.get('agent_cmd')

    def _apply():
        st = get_state(window_id)
        if st is None or not st.is_running():
            return
        if isinstance(result, dict) and result.get('configOptions'):
            _apply_switched_config(cmd, result, None)
            _refresh_daemon_status(st)
        _finish_session_change(
            st, cmd, new_sid, True, None,
            lambda sid: note.format(sid=sid),
            lambda sid: status.format(sid=sid, short=sid[-8:]),
            lambda err: f'\n**[Could not switch session: {err or "unknown error"}]**\n',
            lambda err: f'ACP: Could not switch session: {err or "unknown error"}',
            None,
            session_prompt_pending=prompt_pending,
        )

    ui.on_main(_apply)
