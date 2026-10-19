"""Session and config switch commands - switch/config UI over live daemons."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import sublime
import sublime_plugin

from ..protocol import acp_log
from . import broadcast, cache, ui
from .commands import _acp_input_kwargs, _load_agents
from .config import STATUS_KEY_DAEMON, STATUS_KEY_NOTIFY, STATUS_KEY_USAGE
from .daemon import get_state, is_unloading
from .daemon_sessions import (
    fork_daemon_session,
    get_daemon_session_title,
    list_daemon_sessions,
    new_daemon_session,
    rename_daemon_session,
    switch_daemon_session,
)


def _apply_title_overrides(cmd: list | None, sessions: list) -> None:
    """Annotate *sessions* in place with their local title overrides."""
    if not cmd or not sessions:
        return
    try:
        agents = _load_agents()
    except Exception as exc:
        acp_log('switch_session', f'failed to load title overrides: {exc!r}')
        return
    titles = cache.get_entry(agents, cmd).get('session_titles') or {}
    if not titles:
        return
    for s in sessions:
        if not isinstance(s, dict):
            continue
        override = titles.get(s.get('sessionId'))
        if isinstance(override, str) and override.strip():
            s['title_override'] = override


class _AcpSessionInputHandler(sublime_plugin.ListInputHandler):
    """Session list with a leading new-session action row."""

    def __init__(self, sessions: list, current: str | None) -> None:
        self._sessions = sessions
        self._current = current

    def name(self) -> str:
        return 'Session'

    def placeholder(self) -> str:
        return ''

    def list_items(self) -> tuple:
        """Return ``(items, selected_index)`` with a gutter check on current."""
        ambiguous_id = getattr(sublime, 'KIND_ID_AMBIGUOUS', 0)
        green_id = getattr(sublime, 'KIND_ID_COLOR_GREENISH', ambiguous_id)
        navigation_id = getattr(sublime, 'KIND_ID_NAVIGATION', ambiguous_id)
        items = [
            sublime.ListInputItem(
                '+ Start new session', '',
                details='Start fresh without restarting the agent',
                kind=(navigation_id, '+', ''),
            ),
        ]
        selected_index = 0
        for i, s in enumerate(self._sessions, start=1):
            sid = s.get('sessionId', '')
            title, detail, is_current = ui.session_row(s, self._current)
            if is_current:
                selected_index = i
            kind = (green_id, '✓', '') if is_current else (ambiguous_id, '', '')
            items.append(sublime.ListInputItem(title, sid, details=detail, kind=kind))
        return (items, selected_index)


class AcpSwitchSessionCommand(sublime_plugin.WindowCommand):
    """List recent sessions and switch the running daemon to the selected one."""

    def is_enabled(self):
        """Enable only while the daemon is running and idle."""
        state = get_state(self.window.id())
        return (state is not None and state.is_running() and not state.get('is_busy'))

    def input(self, args: dict) -> Any | None:
        """Return session list from the warmed session cache."""
        if 'Session' in args or 'session_id' in args:
            return None
        state = get_state(self.window.id())
        if state is None or not state.is_running() or not state.supports('list'):
            return None
        sessions = state.get('sessions_cache')
        if sessions is None:
            return None
        _apply_title_overrides(state.get('agent_cmd'), sessions)
        return _AcpSessionInputHandler(sessions, state.get('session_id'),)

    def run(self, session_id: str | None = None, **kwargs: Any):
        """Apply the picked session, or fetch live when the cache is cold."""
        if session_id is None:
            session_id = kwargs.get('Session')
        if session_id is not None:
            if session_id == '':
                new_daemon_session(self.window.id(), on_done=self._on_switched)
            else:
                switch_daemon_session(self.window.id(), session_id, on_done=self._on_switched)
            return
        if is_unloading():
            sublime.status_message('ACP is reloading, please retry in a moment')
            return
        window_id = self.window.id()
        state = get_state(window_id)
        if state is None or not state.is_running():
            sublime.status_message('ACP: No agent session running in this window')
            return
        if state.get('is_busy'):
            sublime.status_message('ACP: Wait for the current prompt to finish before switching')
            return
        list_daemon_sessions(window_id, self._on_listed)

    def _on_listed(self, sessions, supported):
        if sessions is None:
            if supported:
                sublime.status_message('ACP: could not list sessions')
            else:
                sublime.status_message('ACP: agent does not support session/list')
            return
        window_id = self.window.id()
        state = get_state(window_id)
        if state is not None and state.is_running():
            _apply_title_overrides(state.get('agent_cmd'), sessions)
            state.set(sessions_cache=sessions)
        current = state.get('session_id') if state is not None else None
        ambiguous_id = getattr(sublime, 'KIND_ID_AMBIGUOUS', 0)
        green_id = getattr(sublime, 'KIND_ID_COLOR_GREENISH', ambiguous_id)
        navigation_id = getattr(sublime, 'KIND_ID_NAVIGATION', ambiguous_id)
        items = [
            sublime.QuickPanelItem(
                '+ Start new session',
                details='Start fresh without restarting the agent',
                kind=(navigation_id, '+', ''),
            )
        ]
        selected_index = 0
        for i, s in enumerate(sessions, start=1):
            title, detail, is_current = ui.session_row(s, current)
            if is_current:
                selected_index = i
            kind = (green_id, '✓', '') if is_current else (ambiguous_id, '', '')
            items.append(sublime.QuickPanelItem(title, details=detail, kind=kind))
        self._sessions = sessions
        self.window.show_quick_panel(items, self._on_pick, selected_index=selected_index)

    def _on_pick(self, index):
        if index == -1:
            return
        if index == 0:
            new_daemon_session(self.window.id(), on_done=self._on_switched)
            return
        session_id = (self._sessions or [])[index - 1].get('sessionId')
        if not session_id:
            return
        switch_daemon_session(self.window.id(), session_id, on_done=self._on_switched)

    def _on_switched(self, ok, error=None):
        """Focus the input panel after a switch, reopening it if missing."""
        if not ok:
            return
        state = get_state(self.window.id())
        if state is None:
            return
        input_view = state.get('input_view')
        if input_view is not None and input_view.window() is not None:
            self.window.focus_view(input_view)
        else:
            self.window.run_command('acp_input', _acp_input_kwargs(state))


class AcpForkSessionCommand(sublime_plugin.WindowCommand):
    """Fork a session via the unstable ``session/fork`` method."""

    def is_enabled(self):
        """Enable only while the daemon is idle and the agent supports fork."""
        state = get_state(self.window.id())
        return (
            state is not None and state.is_running()
            and not state.get('is_busy')
            and state.supports('fork')
        )

    def run(self, session_id: str | None = None, **kwargs: Any):
        """Fork *session_id*, or pick from ``session/list`` when omitted."""
        if session_id is None:
            session_id = kwargs.get('Session')
        if session_id is not None:
            self._fork(session_id)
            return
        if is_unloading():
            sublime.status_message('ACP is reloading, please retry in a moment')
            return
        window_id = self.window.id()
        state = get_state(window_id)
        if state is None or not state.is_running():
            sublime.status_message('ACP: No agent session running in this window')
            return
        if state.get('is_busy'):
            sublime.status_message('ACP: Wait for the current prompt to finish before forking')
            return
        if not state.supports('fork'):
            sublime.status_message('ACP: agent does not support session/fork')
            return
        list_daemon_sessions(window_id, self._on_listed)

    def _fork(self, session_id: str):
        """Fork *session_id* on the running daemon."""
        if not session_id:
            return
        fork_daemon_session(self.window.id(), session_id, on_done=self._on_forked)

    def _on_listed(self, sessions, supported):
        if sessions is None:
            if not supported:
                state = get_state(self.window.id())
                current = state.get('session_id') if state is not None else None
                if current:
                    self._fork(current)
                else:
                    sublime.status_message('ACP: agent does not support session/list')
            else:
                sublime.status_message('ACP: could not list sessions')
            return
        window_id = self.window.id()
        state = get_state(window_id)
        if state is not None and state.is_running():
            _apply_title_overrides(state.get('agent_cmd'), sessions)
            state.set(sessions_cache=sessions)
        current = state.get('session_id') if state is not None else None
        ambiguous_id = getattr(sublime, 'KIND_ID_AMBIGUOUS', 0)
        green_id = getattr(sublime, 'KIND_ID_COLOR_GREENISH', ambiguous_id)
        items = []
        selected_index = 0
        for i, s in enumerate(sessions):
            title, detail, is_current = ui.session_row(s, current)
            if is_current:
                selected_index = i
            kind = (green_id, '✓', '') if is_current else (ambiguous_id, '', '')
            items.append(sublime.QuickPanelItem(title, details=detail, kind=kind))
        self._sessions = sessions
        self.window.show_quick_panel(items, self._on_pick, selected_index=selected_index)

    def _on_pick(self, index):
        if index == -1:
            return
        session_id = (self._sessions or [])[index].get('sessionId')
        if not session_id:
            return
        self._fork(session_id)

    def _on_forked(self, ok, error=None):
        """Focus the input panel after a fork, reopening it if missing."""
        if not ok:
            return
        state = get_state(self.window.id())
        if state is None:
            return
        input_view = state.get('input_view')
        if input_view is not None and input_view.window() is not None:
            self.window.focus_view(input_view)
        else:
            self.window.run_command('acp_input', _acp_input_kwargs(state))


class _AcpSessionTitleInputHandler(sublime_plugin.TextInputHandler):
    """Free-text input for a local session title override."""

    def __init__(self, current: str) -> None:
        self._current = current

    def name(self) -> str:
        return 'title'

    def placeholder(self) -> str:
        return 'Session title (empty clears the override)'

    def initial_text(self) -> str:
        return self._current

    def validate(self, arg: str) -> bool:
        """Accept any single-line title."""
        return '\n' not in arg and '\r' not in arg


class AcpRenameSessionCommand(sublime_plugin.WindowCommand):
    """Set a local title override for the current session.

    ACP defines no client-to-agent session rename method, so the title is
    recorded client-side only. The agent keeps its own title and, if it later
    reports one via ``session_info_update``, that agent title wins and the
    override is dropped.
    """

    def is_enabled(self) -> bool:
        """Enable only while the daemon is running, idle, and has a session."""
        state = get_state(self.window.id())
        return (
            state is not None and state.is_running()
            and not state.get('is_busy')
            and bool(state.get('session_id'))
        )

    def input(self, args: dict) -> Any | None:
        """Ask for the new title when one was not supplied."""
        if 'title' in args:
            return None
        if not self.is_enabled():
            return None
        return _AcpSessionTitleInputHandler(get_daemon_session_title(self.window.id()))

    def run(self, title: str | None = None, **kwargs: Any) -> None:
        """Store the title chosen via the input handler."""
        if title is None:
            title = kwargs.get('title')
        if title is None:
            return
        rename_daemon_session(self.window.id(), title)


class _AcpSwitchConfigOptionCommand(sublime_plugin.WindowCommand):
    """Base for Model/Mode/Thought Level switch commands.

    Subclasses set :attr:`config_id` (the option identifier) and
    :attr:`label` (used in status-bar messages and the quick-panel placeholder).
    """

    config_id: str = ''
    config_category: str = ''
    label: str = ''

    def _config_option(self) -> dict | None:
        """Return the config option for :attr:`config_id` from the active daemon's cache."""
        state = get_state(self.window.id())
        if state is None or not state.is_running():
            return None
        if not (cmd := state.get('agent_cmd')):
            return None
        agents = _load_agents()
        config_options = cache.get_entry(agents, cmd).get('config_options') or []
        if self.config_id:
            return next((o for o in config_options if o.get('id') == self.config_id), None)
        if self.config_category:
            return next((o for o in config_options if o.get('category') == self.config_category), None)
        return None

    def is_enabled(self) -> bool:
        """Enable only while the daemon is idle and supports this config option."""
        state = get_state(self.window.id())
        return (
            state is not None and state.is_running()
            and not state.get('is_busy')
            and self._config_option() is not None
        )

    def input(self, args: dict) -> Any | None:
        """Return option list when ``value`` was not supplied."""
        if 'value' in args:
            return None
        if not self.is_enabled():
            return None
        opt = self._config_option()
        if opt is None:
            return None
        return _AcpConfigOptionInputHandler(
            opt.get('options') or [],
            opt.get('currentValue'), self.label,
        )

    def run(self, value: str | None = None, **kwargs: Any) -> None:
        """Apply the option chosen via the input handler."""
        if value is None:
            value = kwargs.get(self.label)
        if value is None:
            return
        opt = self._config_option()
        config_id = (opt or {}).get('id') or self.config_id
        self._apply_option(config_id, value)
        st = get_state(self.window.id())
        if st is not None:
            input_view = st.get('input_view')
            if input_view is not None and input_view.window() is not None:
                self.window.focus_view(input_view)

    def _request_spec(self, session_id: str, config_id: str, value: str) -> tuple[str, dict]:
        """Return ``(method, params)`` for applying *value* to *config_id*."""
        return 'session/set_config_option', {
            'sessionId': session_id,
            'configId': config_id,
            'value': value,
        }

    def _apply_option(self, config_id: str, value: str) -> None:
        """Send the switch request for :attr:`config_id` and update the cache."""
        state = get_state(self.window.id())
        if state is None or not state.is_running():
            sublime.status_message('ACP: No daemon running')
            return
        if state.get('is_busy'):
            sublime.status_message('ACP: Wait for the current prompt to finish before switching')
            return
        s = state.get('conn', 'loop', 'session_id', 'agent_cmd')
        conn, loop, session_id, agent_cmd = s['conn'], s['loop'], s['session_id'], s['agent_cmd']
        if conn is None or loop is None or loop.is_closed():
            sublime.status_message('ACP: Daemon connection not available')
            return

        label = self.label
        agent_name = state.get('agent_name') or 'agent'
        owner = self.window
        try:
            owner_id = owner.id()
        except Exception:
            owner_id = None

        async def _send() -> None:
            if state.get('is_busy'):
                acp_log('switch_config', f'skip {config_id} change: daemon busy', owner_id)
                return
            try:
                method, params = self._request_spec(session_id, config_id, value)
                response = await conn.send_request(method, params)
                _handle_success(response, session_id)
            except Exception as exc:
                failed = exc
                if _is_session_not_found(failed) and owner_id is not None:
                    acp_log(
                        f'switch_{config_id}',
                        f'session gone ({failed}); starting new session and retrying',
                        owner_id,
                    )
                    label_lower = label.lower()
                    sublime.set_timeout(
                        lambda lbl=label_lower: sublime.status_message(
                            f'ACP: Session gone - starting new session to set {lbl}'), 0
                        )
                    sublime.set_timeout(
                        lambda orig=failed: new_daemon_session(
                            owner_id, on_done=lambda ok, err: _on_recovered(ok, err, orig)), 0,
                        )
                    return
                _handle_failure(failed)

        def _on_recovered(ok, err, original_exc) -> None:
            """Retry the config change once on the fresh session."""
            if not ok:
                _handle_failure(original_exc)
                return
            s2 = state.get('conn', 'loop', 'session_id')
            conn2, loop2, sid2 = s2['conn'], s2['loop'], s2['session_id']
            if conn2 is None or loop2 is None or loop2.is_closed() or not sid2:
                _handle_failure(original_exc)
                return

            async def _retry():
                method, params = self._request_spec(sid2, config_id, value)
                return await conn2.send_request(method, params)

            try:
                future2 = asyncio.run_coroutine_threadsafe(_retry(), loop2)
            except RuntimeError:
                _handle_failure(original_exc)
                return

            def _done2(f2):
                try:
                    response2 = f2.result()
                except Exception as exc2:
                    _handle_failure(exc2)
                else:
                    _handle_success(response2, sid2)

            future2.add_done_callback(_done2)

        def _handle_success(response, sid) -> None:
            confirmed = value
            refreshed = None
            if isinstance(response, dict):
                refreshed = response.get('configOptions')
                confirmed = (response.get('currentValue') or response.get('value') or value)
            cache_dir = Path(sublime.cache_path()) / 'ACP'
            if refreshed:
                with cache.cache_lock:
                    agents = _load_agents()
                    key = cache.agent_key(agent_cmd)
                    entry = cache.get_entry(agents, agent_cmd)
                    entry['config_options'] = refreshed
                    if key is not None:
                        agents[key] = entry
                        cache.save_agents(cache_dir, agents)
            else:
                cache.set_config_option_value(cache_dir, agent_cmd, config_id, confirmed)
            acp_log(
                f'switch_{config_id}',
                f'{label.lower()} changed to {confirmed!r} (session={sid})',
                owner_id,
            )
            sublime.set_timeout(
                lambda v=confirmed, w=owner: broadcast.set_broadcast_status(
                    STATUS_KEY_NOTIFY, f'ACP: {label} -> {v}', w), 0
                )
            sublime.set_timeout(
                lambda w=owner: broadcast.erase_broadcast_status(STATUS_KEY_NOTIFY, w), 5000
            )
            sublime.set_timeout(
                lambda: broadcast.set_broadcast_status(
                    STATUS_KEY_DAEMON,
                    broadcast.daemon_status_text(agent_name, agent_cmd),
                    self.window,
                ), 0
            )
            if config_id == 'model':
                state.set(usage_used=None, usage_size=None)
                sublime.set_timeout(
                    lambda: broadcast.erase_broadcast_status(STATUS_KEY_USAGE, self.window), 0
                )

        def _handle_failure(exc) -> None:
            acp_log(
                f'switch_{config_id}',
                f'failed to set {config_id} {value!r}: {type(exc).__name__}: {exc}',
                owner_id,
            )
            sublime.set_timeout(
                lambda e=exc, w=owner: broadcast.set_broadcast_status(
                    STATUS_KEY_NOTIFY, f'ACP: Failed to set {label.lower()}: {e}', w), 0
            )
            sublime.set_timeout(
                lambda w=owner: broadcast.erase_broadcast_status(STATUS_KEY_NOTIFY, w), 5000
            )

        asyncio.run_coroutine_threadsafe(_send(), loop)


def _is_session_not_found(exc: BaseException) -> bool:
    """Return ``True`` when *exc* reports a missing agent-side session."""
    return 'session not found' in str(exc).lower()


def _flatten_select_options(options: list) -> list:
    """Flatten ACP select options, expanding grouped entries.

    The ACP schema allows ``options`` to mix plain ``{value, name}``
    entries with grouped ``{group, options: [...]}`` entries (as sent
    by e.g. ``dsh acp``). Sublime's list input has no grouping, so
    grouped children are hoisted with their group label preserved.
    """
    flat: list = []
    for o in options or []:
        if not isinstance(o, dict):
            continue
        nested = o.get('options')
        if isinstance(nested, list) and 'value' not in o:
            group = o.get('group') or o.get('name') or ''
            for child in nested:
                if not isinstance(child, dict):
                    continue
                item = dict(child)
                if group and not item.get('group'):
                    item['group'] = group
                flat.append(item)
        else:
            flat.append(o)
    return flat


class _AcpConfigOptionInputHandler(sublime_plugin.ListInputHandler):
    """Option list for Switch Model/Mode/Thought Level."""

    def __init__(self, options: list, current: Any, label: str) -> None:
        self._options = _flatten_select_options(options)
        self._current = current
        self._label = label

    def name(self) -> str:
        return self._label

    def placeholder(self) -> str:
        return ''

    def list_items(self) -> tuple:
        """Return ``(items, selected_index)`` with a gutter check on current."""
        ambiguous_id = getattr(sublime, 'KIND_ID_AMBIGUOUS', 0)
        green_id = getattr(sublime, 'KIND_ID_COLOR_GREENISH', ambiguous_id)
        items = []
        selected_index = 0
        for i, o in enumerate(self._options):
            name = o.get('name') or o.get('value', '')
            value_str = o.get('value', '')
            if group := o.get('group') or '':
                name = f'{group} / {name}'
            if is_current := o.get('value') == self._current:
                selected_index = i
            kind = (green_id, '✓', '') if is_current else (ambiguous_id, '', '')
            items.append(sublime.ListInputItem(name, value_str, details=value_str, kind=kind))
        return (items, selected_index)


class AcpSwitchModelCommand(_AcpSwitchConfigOptionCommand):
    """Switch the active model on a running daemon session."""

    config_id = 'model'
    label = 'Model'


class AcpSwitchModeCommand(_AcpSwitchConfigOptionCommand):
    """Switch the active session mode on a running daemon session."""

    config_id = 'mode'
    label = 'Mode'

    def _request_spec(self, session_id: str, config_id: str, value: str) -> tuple[str, dict]:
        """Return a ``session/set_mode`` request (modes use ``modeId``)."""
        return 'session/set_mode', {
            'sessionId': session_id,
            'modeId': value,
        }


class AcpSwitchThoughtLevelCommand(_AcpSwitchConfigOptionCommand):
    """Switch the reasoning effort on a running daemon session."""

    config_category = 'thought_level'
    label = 'Thought level'
