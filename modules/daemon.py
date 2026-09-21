"""Daemon management: state, lifecycle, session, and idle timer.

Manages per-window daemon state (``DaemonState``), launches one-shot ACP
workers or persistent session daemon threads, handles idle-timeout cleanup,
and provides prompt enqueue/dequeue for interactive sessions.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import sublime

from ..protocol import (
    STATUS_ERROR,
    STATUS_LOADED,
    STATUS_NEW,
    STATUS_RESUMED,
    ACPError,
    acp_log,
    cleanup_process,
    close_writer,
    list_sessions,
    new_session,
    signal_process_group,
    supports_list,
    supports_load,
    supports_resume,
    supports_resume_or_load,
)
from . import broadcast, cache, git_summary, ui
from .config import (
    DEFAULT_PERMISSIONS,
    IDLE_TIMEOUT_DEFAULT,
    IDLE_TIMER_INTERVAL,
    PERMISSION_PROMPT_TIMEOUT,
    STATUS_KEY_DAEMON,
    STATUS_KEY_USAGE,
    TOOL_CALLS_DEFAULT,
    TURN_DIVIDER,
)
from .config import settings as load_settings
from .permissions import dismiss_permission_prompt, resolve_permission
from .rpc import (
    PROMPT_CONNECTION_CLOSED,
    PROMPT_OK,
    PROMPT_SESSION_NOT_FOUND,
    _extract_available_commands,
    _extract_usage_update,
    acp,
    send_prompt_and_stream,
    spawn_and_init,
)

# Per-window daemon registry

_daemon_registry: dict[int, DaemonState] = {}
_registry_lock = threading.Lock()


class DaemonState:
    """Thread-safe daemon lifecycle state."""

    def __init__(self):
        self._lock = threading.Lock()
        self.running: bool = False
        self.agent_cmd: list | None = None
        self.agent_name: str | None = None
        self.window_id: int | None = None
        self.session_id: str | None = None
        self.thread: threading.Thread | None = None
        self.proc = None
        self.conn = None
        self.output_view = None
        self.input_view = None
        self.last_activity: float | None = None
        self.queue = None
        self.is_busy: bool = False
        self.has_replied: bool = False
        self.loop = None
        self.env: dict | None = None
        self.auth: bool | None = None
        self.permission_pending: bool = False
        self.usage_used: int | None = None
        self.usage_size: int | None = None
        self.agent_caps: dict | None = None
        self.work_dir: str | None = None
        self.model: str | None = None
        self.permissions_config: dict | None = None

    def is_running(self) -> bool:
        with self._lock:
            return bool(self.running)

    def set(self, **kwargs) -> None:
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)

    def get(self, *keys: str):
        if not keys:
            raise ValueError('DaemonState.get() requires at least one key')
        with self._lock:
            d = {k: getattr(self, k, None) for k in keys}
            return d[keys[0]] if len(keys) == 1 else d

    def supports(self, method: str) -> bool:
        """Return whether the active agent advertises support for *method*.

        Args:
            method: One of ``'resume'``, ``'load'``, ``'resume_or_load'``,
                or ``'list'``, mapping to ``session/resume``,
                ``session/load``, either of those, or ``session/list``.

        Returns:
            ``True`` when the cached ``agent_caps`` advertises the method.
        """
        with self._lock:
            caps = self.agent_caps
        if method == 'list':
            return supports_list(caps)
        if method == 'resume':
            return supports_resume(caps)
        if method == 'load':
            return supports_load(caps)
        if method == 'resume_or_load':
            return supports_resume_or_load(caps)
        raise ValueError(f'Unknown capability method: {method!r}')

    def reset(self, stop_idle_timer_func=None) -> None:
        with self._lock:
            window_id = self.window_id
            input_view = self.input_view
            output_view = self.output_view
            permission_pending = self.permission_pending
            self.running = False
            self.agent_cmd = None
            self.agent_name = None
            self.window_id = None
            self.session_id = None
            self.thread = None
            self.proc = None
            self.conn = None
            self.output_view = None
            self.input_view = None
            self.last_activity = None
            self.queue = None
            self.is_busy = False
            self.has_replied = False
            self.loop = None
            self.env = None
            self.auth = None
            self.permission_pending = False
            self.usage_used = None
            self.usage_size = None
            self.agent_caps = None
            self.work_dir = None
            self.model = None
            self.permissions_config = None

        if stop_idle_timer_func:
            stop_idle_timer_func(window_id)

        if permission_pending and window_id is not None:
            dismiss_permission_prompt(window_id)

        if input_view is not None:
            def _hide_input_panel():
                w = input_view.window()
                if w is not None:
                    w.run_command('hide_panel', {'cancel': True})
            ui.on_main(_hide_input_panel)
        acp_log('daemon_state', 'daemon state reset complete')

        def _clear_status():
            current = get_state(window_id) if window_id is not None else None
            if current is not None and current is not self:
                return
            win = output_view.window() if output_view else None
            if win is None and window_id is not None:
                win = next((w for w in sublime.windows() if w.id() == window_id), None)
            broadcast.erase_broadcast_status(STATUS_KEY_DAEMON, win)
            broadcast.erase_broadcast_status(STATUS_KEY_USAGE, win)

        ui.on_main(_clear_status)


def get_state(window_id: int) -> DaemonState | None:
    with _registry_lock:
        return _daemon_registry.get(window_id)


def set_state(window_id: int, state: DaemonState) -> None:
    with _registry_lock:
        _daemon_registry[window_id] = state


def remove_state(window_id: int) -> None:
    with _registry_lock:
        _daemon_registry.pop(window_id, None)


def any_running() -> bool:
    with _registry_lock:
        return any(s.is_running() for s in _daemon_registry.values())


def running_windows() -> list[int]:
    with _registry_lock:
        return [wid for wid, s in _daemon_registry.items() if s.is_running()]


_unloading: bool = False


def request_unload() -> None:
    """Mark the plugin as unloading so stale threads stop touching the UI."""
    global _unloading
    _unloading = True


def clear_unload() -> None:
    """Reset the unloading flag once the plugin finishes loading."""
    global _unloading
    _unloading = False


def is_unloading() -> bool:
    """Return True while the plugin is unloading (reload/disable)."""
    return _unloading


def stop_all_daemons(stop_func, join_timeout: float | None = 2.5) -> None:
    """Stop all running daemons across all windows.

    When *join_timeout* is ``None`` the stop threads are fire-and-forget
    (never block the caller - required on the main thread during
    ``plugin_unloaded``).
    """
    wids = list(running_windows())
    threads = [
        threading.Thread(target=stop_func, args=(wid, 1.0), daemon=True)
        for wid in wids
    ]
    for t in threads:
        t.start()
    if join_timeout is None:
        return
    for t in threads:
        t.join(timeout=join_timeout)

# Cache helpers

def _cache_dir() -> Path:
    """Get the ACP cache directory path."""
    return Path(sublime.cache_path()) / 'ACP'


def _update_agent_session_id(cmd, session_id):
    """Update the cached session ID for an agent command."""
    try:
        cache.update_session_id(_cache_dir(), cmd, session_id)
    except Exception as e:
        msg = f'✗ Failed to update session ID: {e}'
        ui.on_main(lambda: sublime.status_message(msg))


def _clear_agent_session_id(cmd):
    """Invalidate the cached session ID for an agent command."""
    try:
        cache.clear_session_id(_cache_dir(), cmd)
    except Exception as e:
        msg = f'✗ Failed to clear session ID: {e}'
        ui.on_main(lambda: sublime.status_message(msg))


def _make_commands_updater(cmd):
    """Return a callback that persists ``available_commands`` for *cmd*."""

    def _update(commands):
        if not isinstance(commands, list):
            return
        try:
            with cache.cache_lock:
                cache_dir = _cache_dir()
                agents = cache.load_agents(cache_dir)
                entry = agents.get(cmd[0], {})
                if entry.get('commands') == commands:
                    return
                entry['commands'] = commands
                agents[cmd[0]] = entry
                cache.save_agents(cache_dir, agents)
        except Exception as e:
            acp_log('daemon', f'error caching commands: {e}')

    return _update


def _make_usage_updater(state: DaemonState):
    """Return an ``on_usage(used, size)`` callback updating the status bar.

    Stores the latest counts in *state* (memory only) and broadcasts
    ``ctx 27% (53k/200k)`` on ``STATUS_KEY_USAGE``. Sublime API calls are
    dispatched to the main thread via ``ui.on_main``.
    """

    def _update(used: int, size: int) -> None:
        state.set(usage_used=used, usage_size=size)
        text = broadcast.usage_status_text(used, size)

        def _apply():
            output_view = state.get('output_view')
            win = output_view.window() if output_view is not None else None
            if win is None:
                win_id = state.get('window_id')
                if win_id is not None:
                    win = next((w for w in sublime.windows() if w.id() == win_id), None)
            if win is None:
                return
            if load_settings().get('context_usage', True):
                broadcast.set_broadcast_status(STATUS_KEY_USAGE, text, win)
            else:
                broadcast.erase_broadcast_status(STATUS_KEY_USAGE, win)

        ui.on_main(_apply)

    return _update


def _install_notification_handler(conn, cmd, on_usage=None):
    """Keep a persistent notification handler on the daemon connection.

    Persists ``available_commands_update`` payloads to the agent cache as they
    arrive, so commands are captured even when the agent announces them after
    the init phase has returned. Forwards ``usage_update`` payloads to
    *on_usage*; other notifications are ignored here; prompt streaming
    installs its own callbacks via ``swap_callbacks``, which restores this
    handler afterwards.
    """
    update_commands = _make_commands_updater(cmd)

    def on_notification(method: str, params: dict) -> None:
        matched, commands = _extract_available_commands(method, params)
        if matched:
            acp_log('daemon', f'available_commands_update ({len(commands or [])} commands)')
            update_commands(commands)
            return
        usage_matched, used, size = _extract_usage_update(method, params)
        if usage_matched and on_usage is not None:
            acp_log('daemon', f'usage_update (used={used}, size={size})')
            on_usage(used, size)

    conn.notification_callback = on_notification


def _cache_daemon_agent_info(cmd, init_result):
    """Cache agent information from initialization result."""
    try:
        with cache.cache_lock:
            agents = cache.load_agents(_cache_dir())
            entry = agents.get(cmd[0], {})
            entry['commands'] = init_result.get('available_commands') or entry.get('commands')
            entry['config_options'] = init_result.get('config_options') or entry.get('config_options')
            confirmed_model = init_result.get('model')
            if confirmed_model and isinstance(entry.get('config_options'), list):
                for opt in entry['config_options']:
                    if isinstance(opt, dict) and opt.get('id') == 'model':
                        opt['currentValue'] = confirmed_model
                        break
            entry['capabilities'] = {'result': init_result.get('initialize_result', {})}
            entry['last_sync'] = datetime.now().isoformat()
            agents[cmd[0]] = entry
            cache.save_agents(_cache_dir(), agents)
    except Exception as e:
        acp_log('daemon', f'error caching agent info: {e}')


def _finalize_daemon_connection(conn, cmd, init_result, on_usage=None, strict=False):
    """Wire up a freshly initialized daemon connection.

    Caches agent info, installs the persistent notification handler,
    and derives the session id plus agent capabilities. Returns
    ``(session_id, agent_caps)``.
    """
    _cache_daemon_agent_info(cmd, init_result)
    _install_notification_handler(conn, cmd, on_usage)
    session_id = init_result['session_id'] if strict else init_result.get('session_id')
    agent_caps = (init_result.get('initialize_result') or {}).get('agentCapabilities') or {}
    return session_id, agent_caps


def _build_env(env: dict) -> dict:
    """Build environment variables by merging with system environment."""
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return merged


def _load_permissions(settings) -> dict:
    """Load permissions configuration from settings."""
    return settings.get('permissions') or DEFAULT_PERMISSIONS

# Lifecycle helpers

def _ensure_output_view(window, state: DaemonState, agent_name: str):
    """Ensure an output view exists for the daemon session."""
    view = state.get('output_view')
    if view is not None and view.window() is not None:
        return view
    new_view = ui.create_output_view(window, agent_name, role='daemon')
    ui.open_split_for_output(window, new_view)
    state.set(output_view=new_view)
    return new_view


def _setup_async_loop_and_queue(state: DaemonState):
    """Create and configure an asyncio event loop and queue for daemon operations."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    state.set(loop=loop)
    async_queue = asyncio.Queue()
    state.set(queue=async_queue)
    return loop, async_queue


def _safe_shutdown_loop(loop):
    """Safely shutdown an asyncio event loop."""
    try:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(asyncio.sleep(0.05))
    except Exception as exc:
        acp_log('daemon', f'error shutting down event loop: {exc}')
    finally:
        loop.close()


def _maybe_reset_daemon_on_exit(window_id: int, cmd, agent_name, state):
    """Reset daemon state and remove from registry if the agent command matches."""
    if state and state.get('agent_cmd') == cmd:
        def _on_exit():
            state.reset()
            if get_state(window_id) is state:
                remove_state(window_id)
            sublime.status_message(f"✓ Agent '{agent_name}' stopped")
        ui.on_main(_on_exit)

# One-shot worker thread

def _run_acp_worker(
    cmd: list,
    prompt: str,
    model: str,
    system_prompt: str,
    work_dir: str,
    env: dict,
    timeout: int,
    session_id: str | None,
    output_view,
    settings,
    permissions_config: dict | None = None,
    auth: bool | None = None,
) -> None:
    """Run a one-shot ACP worker thread for a single prompt.

    Args:
        cmd: Agent command list.
        prompt: User prompt text.
        model: Model name to use.
        system_prompt: System prompt for the agent.
        work_dir: Working directory for the agent.
        env: Environment variables.
        timeout: Request timeout in seconds.
        session_id: Optional session ID to resume.
        output_view: Output view for streaming responses.
        settings: Sublime settings dictionary.
        permissions_config: Optional permissions configuration.
        auth: Optional authentication flag.
    """
    on_chunk = ui.make_stream_callback(output_view)
    args = (on_chunk, cmd, prompt, model, system_prompt, work_dir, env, timeout, session_id, settings, permissions_config, auth)
    thread = threading.Thread(target=_worker_thread, args=args, daemon=True)

    def _on_done():
        output_view.set_status('acp_status', '✓ ACP Request Complete!')
        ui.on_main(lambda: output_view.erase_status('acp_status'), 5000)

    try:
        thread.start()
    except Exception as exc:
        on_chunk(f'\n**[Agent Error]:** `could not start worker thread: {exc}`\n')
        _on_done()
        return

    broadcast.show_spinner(
        output_view,
        lambda: not thread.is_alive(),
        'ACP Request',
        on_done=_on_done,
    )


def _worker_thread(
    on_chunk,
    cmd: list,
    prompt: str,
    model: str | None,
    system_prompt: str | None,
    work_dir: str,
    env: dict,
    timeout: float,
    session_id: str | None = None,
    settings=None,
    permissions_config: dict | None = None,
    auth: bool | None = None,
):
    """Worker thread function for one-shot ACP requests.

    Args:
        on_chunk: Callback for streaming response chunks.
        cmd: Agent command list.
        prompt: User prompt text.
        model: Optional model name.
        system_prompt: Optional system prompt.
        work_dir: Working directory for the agent.
        env: Environment variables.
        timeout: Request timeout in seconds.
        session_id: Optional session ID to resume.
        settings: Sublime settings dictionary.
        permissions_config: Optional permissions configuration.
        auth: Optional authentication flag.
    """
    async def async_wrapper():
        current_env = _build_env(env)
        tool_calls_mode = settings.get('tool_calls', TOOL_CALLS_DEFAULT) if settings else TOOL_CALLS_DEFAULT
        result_session_id, status, session_error = await acp(
            cmd=cmd,
            prompt=prompt,
            model=model,
            system_prompt=system_prompt,
            env=current_env,
            callback=on_chunk,
            callback_timeout=timeout,
            session_id=session_id,
            cwd=work_dir,
            permissions_config=permissions_config,
            auth=auth,
            thoughts_mode=settings.get('thoughts', 'enabled') if settings else 'enabled',
            show_tool_calls=tool_calls_mode == 'enabled',
        )
        if status == STATUS_ERROR or result_session_id is None:
            if session_id:
                on_chunk(
                    '\n**[Could not resume previous session, see console]**\n'
                )
                _clear_agent_session_id(cmd)
            else:
                on_chunk(
                    '\n**[Could not start session, see console]**\n'
                )
        else:
            if session_id and status in (STATUS_RESUMED, STATUS_LOADED):
                on_chunk(f'\n*[Resumed session: {session_id}]*\n\n')
            elif session_id and status == STATUS_NEW:
                if session_error:
                    on_chunk(f'\n**[{session_error}]**\n\n')
                on_chunk('\n**[Started new session]**\n\n')
            _update_agent_session_id(cmd, result_session_id)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        loop.run_until_complete(async_wrapper())
    except Exception as exc:
        on_chunk(f'\n**[Agent Error]:** `{exc}`\n')
    finally:
        dividers = settings.get('turn_dividers', True) if settings is not None else True
        if dividers:
            on_chunk(TURN_DIVIDER)
        _safe_shutdown_loop(loop)

# Idle timer

_idle_timer_active: dict[int, bool] = {}


def _start_idle_timer(window_id: int):
    """Start the idle timeout check timer for a window."""
    _idle_timer_active[window_id] = True
    _check_idle_timeout(window_id)


def _check_idle_timeout(window_id: int):
    """Check if the daemon has been idle longer than the configured timeout."""
    if not _idle_timer_active.get(window_id, False):
        return

    state = get_state(window_id)
    if state is None or not state.is_running():
        _idle_timer_active.pop(window_id, None)
        return

    idle_timeout = load_settings().get('daemon_idle_timeout', IDLE_TIMEOUT_DEFAULT)
    last_activity = state.get('last_activity')
    busy_awaiting_permission = state.get('is_busy') and not state.get('permission_pending')
    idle_exceeded = (
        idle_timeout > 0
        and not busy_awaiting_permission
        and last_activity is not None
        and (time.monotonic() - last_activity) > idle_timeout
    )
    if idle_exceeded:
        agent_name = state.get('agent_name') or 'unknown'
        _idle_timer_active.pop(window_id, None)

        def _stop_via_command():
            _stop_daemon_async(window_id)
            sublime.status_message(f'Agent "{agent_name}" stopped (idle timeout)')

        ui.on_main(_stop_via_command)
        return

    ui.on_main(lambda: _check_idle_timeout(window_id), IDLE_TIMER_INTERVAL * 1000)


def _stop_idle_timer(window_id: int | None = None):
    """Stop the idle timeout timer for a window or all windows."""
    if window_id is not None:
        _idle_timer_active.pop(window_id, None)
    else:
        _idle_timer_active.clear()

# Prompt execution

def _handle_daemon_stopped(state: DaemonState, window_id: int) -> None:
    """Reset daemon state after an unexpected loop/thread failure."""
    state.reset(_stop_idle_timer)
    remove_state(window_id)
    sublime.status_message('ACP daemon stopped unexpectedly')


def _execute_prompt_daemon(prompt: str, source_view,
                           force_selection: bool = False) -> None:
    """Execute a prompt in a persistent daemon session."""
    window = source_view.window() if source_view else sublime.active_window()
    if window is None:
        sublime.status_message('No window available')
        return
    window_id = window.id()
    state = get_state(window_id)
    if state is None or not state.is_running():
        sublime.status_message('No agent session running in this window')
        return

    if state.get('is_busy'):
        sublime.status_message(
            'Agent is busy. Cancel the current prompt first or wait.'
        )
        return

    agent_name = state.get('agent_name') or 'agent'
    output_view = _ensure_output_view(
        window,
        state,
        agent_name,
    )

    ui.append_prompt_turn(output_view, prompt, source_view,
                          force_selection=force_selection)
    prompt = ui.attach_selection_to_prompt(prompt, source_view,
                                           force=force_selection)

    state.set(is_busy=True, has_replied=False, last_activity=time.monotonic())

    s = state.get('loop', 'queue')
    loop, queue = s['loop'], s['queue']
    if loop is not None and queue is not None:
        if loop.is_closed():
            _handle_daemon_stopped(state, window_id)
            return
        async def _enqueue():
            await queue.put(prompt)
        try:
            asyncio.run_coroutine_threadsafe(_enqueue(), loop)
        except RuntimeError:
            _handle_daemon_stopped(state, window_id)


def execute_prompt(
    *,
    window,
    source_view,
    prompt: str,
    cmd: list,
    model: str,
    env: dict,
    timeout: int,
    system_prompt: str,
    session_id: str | None = None,
    agent_name: str = '',
    settings=None,
    auth: bool | None = None,
    force_selection: bool = False,
) -> None:
    """Execute a one-shot prompt without a persistent daemon session.

    Args:
        window: Sublime window instance.
        source_view: Source view for context and selection attachment.
        prompt: User prompt text.
        cmd: Agent command list.
        model: Model name to use.
        env: Environment variables.
        timeout: Request timeout in seconds.
        system_prompt: System prompt for the agent.
        session_id: Optional session ID to resume.
        agent_name: Name of the agent for the output view title.
        settings: Sublime settings dictionary.
        auth: Optional authentication flag.
    """
    output_view = ui.create_output_view(window, agent_name, role='daemon')
    ui.open_split_for_output(window, output_view)

    work_dir = ui.resolve_work_dir(window, source_view)

    ui.append_prompt_turn(output_view, prompt, source_view,
                          force_selection=force_selection)
    prompt = ui.attach_selection_to_prompt(prompt, source_view,
                                           force=force_selection)

    _run_acp_worker(
        cmd, prompt, model, system_prompt, work_dir, env, timeout,
        session_id, output_view, settings, _load_permissions(settings),
        auth,
    )

# Daemon thread

async def _reconnect_daemon_session(
    cmd: list,
    env: dict,
    model: str | None,
    session_id: str | None,
    work_dir: str,
    output_view,
    permissions_config: dict | None,
    auth: bool | None,
    old_conn,
    old_proc,
    on_usage=None,
) -> tuple[Any, Any, str | None]:
    """Resolve a daemon session whose live process dropped it.

    Closes the stale subprocess/connection and spawns a fresh process,
    attempting to resume *session_id* (which stays cached for later
    ``Continue session`` use). Falls back to a new session if the resume
    fails even on a fresh process.

    Returns ``(proc, conn, session_id)`` on success, or ``(None, None, None)``
    when no session could be established (the daemon should then stop).
    """
    def _note(msg: str) -> None:
        ui.on_main(lambda m=msg: ui.append_to_output_view(output_view, m))

    _note('\n*[Agent session was dropped; reconnecting to resume it]*\n')
    if old_conn is not None:
        await old_conn.close()
    if old_proc is not None:
        await cleanup_process(old_proc, old_conn.writer if old_conn is not None else None)

    result = await spawn_and_init(
        cmd, env, model, session_id, work_dir,
        permissions_config=permissions_config,
        auth=auth,
    )
    if result is None and session_id:
        acp_log('daemon_session', 'resume failed on a fresh process; falling back to a new session')
        _clear_agent_session_id(cmd)
        result = await spawn_and_init(
            cmd, env, model, None, work_dir,
            permissions_config=permissions_config,
            auth=auth,
        )
    if result is None:
        return None, None, None

    proc, conn, init_result = result
    new_sid, _ = _finalize_daemon_connection(conn, cmd, init_result, on_usage)
    opened_via = init_result.get('opened_via', STATUS_NEW)
    if opened_via in (STATUS_RESUMED, STATUS_LOADED):
        _note(f'*[Resumed session: {new_sid}]*\n')
    else:
        if new_sid:
            _update_agent_session_id(cmd, new_sid)
        if session_error := init_result.get('session_error'):
            _note(f'\n**[{session_error}]**\n\n')
        _note('*[Started a new session]*\n')
    return proc, conn, new_sid


def _window_for_state(state: DaemonState):
    """Return the Sublime window owning *state*, or ``None``."""
    output_view = state.get('output_view')
    if output_view is not None:
        win = output_view.window()
        if win is not None:
            return win
    window_id = state.get('window_id')
    if window_id is not None:
        return next((w for w in sublime.windows() if w.id() == window_id), None)
    return None


async def _resume_on_conn(conn, agent_caps: dict | None,
                          session_id: str, cwd: str | None) -> str:
    """Resume *session_id* on a live *conn* via ``session/resume``/``load``.

    Returns the status constant on success and raises :class:`ACPError` when
    the agent does not support resume/load or rejects the request.
    """
    params = {
        'sessionId': session_id,
        'cwd': cwd or os.getcwd(),
        'mcpServers': [],
    }
    if supports_resume(agent_caps):
        await conn.send_request('session/resume', params)
        return STATUS_RESUMED
    if supports_load(agent_caps):
        await conn.send_request('session/load', params)
        return STATUS_LOADED
    raise ACPError(-32601, 'Agent does not support session resume or load')


async def _reconnect_and_resume(state: DaemonState, cmd: list, env: dict,
                                work_dir: str,
                                session_id: str) -> tuple[bool, str | None]:
    """Reconnect the daemon subprocess and resume *session_id*.

    Fallback for agents that reject ``session/resume``/``session/load`` on an
    already-initialized connection. Spawns a fresh subprocess and resumes the
    session during init, tearing down the old subprocess only once the new one
    is up (so a failed spawn leaves the daemon intact).
    """
    old_conn = state.get('conn')
    old_proc = state.get('proc')

    result = await spawn_and_init(
        cmd, env, state.get('model'), session_id, work_dir,
        permissions_config=state.get('permissions_config'),
        auth=state.get('auth'),
    )
    if result is None:
        return False, 'Could not resume session'
    proc, conn, init_result = result

    if old_conn is not None:
        await old_conn.close()
    if old_proc is not None:
        await cleanup_process(old_proc, old_conn.writer if old_conn is not None else None)

    new_sid, agent_caps = _finalize_daemon_connection(
        conn, cmd, init_result, _make_usage_updater(state))
    state.set(
        proc=proc, conn=conn,
        session_id=new_sid,
        agent_caps=agent_caps,
    )
    if init_result.get('opened_via', STATUS_NEW) not in (STATUS_RESUMED, STATUS_LOADED):
        return False, init_result.get('session_error') or 'Could not resume session'
    return True, None


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
                           fail_view, fail_status, on_done) -> None:
    if ok:
        _update_agent_session_id(cmd, sid)
        state.set(
            session_id=sid,
            usage_used=None, usage_size=None,
            has_replied=False, last_activity=time.monotonic(),
        )
        output_view = state.get('output_view')
        if output_view is not None:
            ui.append_to_output_view(output_view, success_view(sid))
        broadcast.erase_broadcast_status(STATUS_KEY_USAGE, _window_for_state(state))
        sublime.status_message(success_status(sid))
    else:
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

    async def _do_switch():
        state.set(is_busy=True)
        try:
            agent_caps = state.get('agent_caps')
            if not state.supports('resume_or_load'):
                return False, 'Agent does not support session resume or load'
            try:
                await _resume_on_conn(conn, agent_caps, session_id, work_dir)
                return True, None
            except (ACPError, asyncio.TimeoutError, ConnectionError) as exc:
                acp_log('switch_session', f'in-place resume failed ({exc!r}); reconnecting')
            try:
                return await _reconnect_and_resume(
                    state, cmd, _build_env(env or {}), work_dir, session_id,
                )
            except Exception as exc:
                acp_log('switch_session', f'reconnect failed: {exc!r}')
                return False, str(exc)
        finally:
            state.set(is_busy=False)

    future = _submit_daemon_task(loop, _do_switch, on_done)
    if future is None:
        return

    def _done(f):
        try:
            ok, error = f.result()
        except Exception as exc:
            acp_log('switch_session', f'switch raised: {exc!r}')
            ok, error = False, str(exc)

        def _apply():
            _finish_session_change(
                state, cmd, session_id, ok, error,
                lambda sid: f'\n*[Switched to session: {sid}]*\n',
                lambda sid: f'ACP: Switched to session {sid[-8:]}',
                lambda err: f'\n**[Could not switch session: {err or "unknown error"}]**\n',
                lambda err: f'ACP: Could not switch session: {err or "unknown error"}',
                on_done,
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
            )

        ui.on_main(_apply)

    future.add_done_callback(_done)


def _daemon_thread_main(
    window_id: int,
    cmd: list,
    agent_name: str,
    env: dict,
    model: str | None,
    system_prompt: str,
    work_dir: str,
    timeout: float,
    output_view,
    settings,
    permissions_config: dict | None = None,
    auth: bool | None = None,
) -> None:
    """Main daemon thread function for persistent agent sessions.

    Spawns the agent process, initializes a new session, and processes prompts
    from an async queue until stopped.

    Args:
        window_id: Window ID for the daemon session.
        cmd: Agent command list.
        agent_name: Name of the agent.
        env: Environment variables.
        model: Optional model name.
        system_prompt: Optional system prompt.
        work_dir: Working directory for the agent.
        timeout: Request timeout in seconds.
        output_view: Output view for streaming responses.
        settings: Sublime settings dictionary.
        permissions_config: Optional permissions configuration.
        auth: Optional authentication flag.
    """
    acp_log(
        'daemon_session',
        f'daemon thread started for "{agent_name}" (thread={threading.current_thread().ident})'
    )
    acp_log('daemon_session', f'cmd={cmd}')

    state = get_state(window_id)
    if state is None:
        acp_log('daemon_session', f'no daemon state for window {window_id} - creating')
        state = DaemonState()
        set_state(window_id, state)

    loop, async_queue = _setup_async_loop_and_queue(state)
    stream_callback = ui.make_stream_callback(state.get('output_view'), state)
    usage_updater = _make_usage_updater(state)

    async def _wrapper():
        current_env = _build_env(env)
        proc = conn = None
        acp_log('daemon_session', 'calling spawn_and_init()')
        result = await spawn_and_init(cmd, current_env, model, None, work_dir,
                                       permissions_config=permissions_config,
                                       auth=auth)
        if result is None:
            acp_log('daemon_session', 'spawn_and_init returned None - init failed')
            return None, 'error'
        proc, conn, init_result = result
        try:
            sid, agent_caps = _finalize_daemon_connection(conn, cmd, init_result, usage_updater, strict=True)
            acp_log('daemon_session', f'spawn_and_init succeeded: session_id={init_result.get("session_id")}, opened_via={init_result.get("opened_via")}, proc={proc.pid if proc else None}')

            state.set(
                proc=proc, conn=conn,
                session_id=sid, is_busy=False,
                agent_caps=agent_caps, work_dir=work_dir,
                model=model, permissions_config=permissions_config,
            )

            first_prompt = True
            while True:
                item = await async_queue.get()
                if item is None:
                    acp_log('daemon_session', 'received sentinel - breaking prompt loop')
                    async_queue.task_done()
                    break

                prompt_text = item
                # Read the live session fields each iteration so a switch or
                # reconnect scheduled on this loop is picked up.
                conn = state.get('conn')
                sid = state.get('session_id')
                state.set(is_busy=True, has_replied=False)
                acp_log('daemon_session', f'processing prompt ({len(prompt_text)} chars)')
                ui.on_main(
                    lambda: broadcast.show_spinner(
                        output_view,
                        lambda: not state.get('is_busy'),
                        f'{agent_name} processing',
                        on_done=lambda ov=output_view, a=agent_name, c=cmd: broadcast.set_broadcast_status(
                            STATUS_KEY_DAEMON, broadcast.daemon_status_text(a, c), ov.window()
                        ),
                    ),
                )

                async def _on_permission(params):
                    state.set(permission_pending=True, last_activity=time.monotonic())
                    try:
                        return await resolve_permission(
                            params, permissions_config, window_id, loop=loop,
                            timeout=settings.get(
                                'permission_prompt_timeout',
                                PERMISSION_PROMPT_TIMEOUT,
                            ),
                        )
                    finally:
                        state.set(permission_pending=False)

                git_mode = git_summary.resolve_mode(settings.get('git_turn_summary', 'counts'))
                git_base = git_summary.snapshot(work_dir) if git_mode != git_summary.GIT_SUMMARY_OFF else None

                ok = await send_prompt_and_stream(
                    conn, sid, prompt_text,
                    system_prompt if first_prompt else None,
                    callback=stream_callback, callback_timeout=timeout,
                    workspace_root=work_dir,
                    permissions_config=permissions_config,
                    mode='daemon',
                    on_permission_prompt=_on_permission,
                    thoughts_mode=settings.get('thoughts', 'enabled'),
                    on_commands=_make_commands_updater(cmd),
                    on_usage=usage_updater,
                    show_tool_calls=settings.get('tool_calls', TOOL_CALLS_DEFAULT) == 'enabled',
                )
                if ok != PROMPT_OK:
                    dismiss_permission_prompt(window_id)
                    if ok in (PROMPT_SESSION_NOT_FOUND, PROMPT_CONNECTION_CLOSED):
                        proc, conn, sid = await _reconnect_daemon_session(
                            cmd, current_env, model, state.get('session_id'), work_dir,
                            output_view, permissions_config, auth,
                            state.get('conn'), state.get('proc'),
                            usage_updater,
                        )
                        if conn is None or sid is None:
                            acp_log('daemon_session', 'session recovery failed - stopping daemon')
                            break
                        state.set(proc=proc, conn=conn, session_id=sid)
                first_prompt = False

                acp_log('daemon_session', 'prompt completed')
                async_queue.task_done()
                try:
                    summary = git_summary.summarize(
                        git_base, work_dir,
                        include_diff=(git_mode == git_summary.GIT_SUMMARY_DIFF),
                    )
                except Exception as exc:
                    acp_log('daemon_session', f'git summary failed: {exc}')
                    summary = None
                if summary:
                    ui.on_main(
                        lambda s=summary, ov=output_view: ui.append_to_output_view(ov, s),
                    )
                ui.append_turn_divider(
                    output_view,
                    enabled=settings.get('turn_dividers', True),
                )
                state.set(
                    is_busy=False, last_activity=time.monotonic(),
                )
                ui.on_main(
                    lambda ov=output_view: ui.reopen_daemon_input_panel(
                        cmd, model, timeout, system_prompt, state.get('session_id'),
                        agent_name, ov.window() if ov.window() is not None else False,
                        env=state.get('env') or {},
                        auth=state.get('auth'),
                    ),
                )

            acp_log('daemon_session', 'exiting prompt loop normally')
        finally:
            acp_log('daemon_session', 'entering _wrapper() finally - cleaning up')
            conn = state.get('conn')
            proc = state.get('proc')
            if conn is not None:
                await conn.close()
            if proc is not None:
                await cleanup_process(proc, conn.writer if conn is not None else None)
            acp_log('daemon_session', '_wrapper() finally - cleanup done')

        return state.get('session_id'), 'new'

    try:
        acp_log('daemon_session', 'running _wrapper() via loop.run_until_complete()')
        sid, status = loop.run_until_complete(_wrapper())
        acp_log('daemon_session', f'_wrapper() returned: sid={sid}, status={status}')
        if sid is not None:
            _update_agent_session_id(cmd, sid)
    except Exception as e:
        error_msg = str(e)
        acp_log('daemon_session', f'_wrapper() raised exception: {error_msg}')
        def _show_error():
            ui.append_to_output_view(
                output_view,
                f'\n**[Agent Error]:** `{error_msg}`\n',
            )
        ui.on_main(_show_error)
    finally:
        acp_log('daemon_session', 'daemon thread finally - shutting down loop and resetting state')
        _safe_shutdown_loop(loop)
        state.set(running=False)
        _maybe_reset_daemon_on_exit(window_id, cmd, agent_name, state)
        acp_log('daemon_session', 'daemon thread exiting')

# Stop daemon

def _stop_daemon(window_id: int, join_timeout: float = 5.0) -> None:
    """Stop a daemon session for a window.

    Posts a sentinel to the daemon queue to stop the prompt loop, closes
    the connection writer if busy, and waits for the daemon thread to exit.

    Args:
        window_id: Window ID for the daemon to stop.
        join_timeout: Maximum time to wait for thread exit in seconds.
    """
    state = get_state(window_id)
    if state is None or not state.is_running():
        acp_log('daemon_session', f'_stop_daemon called but daemon not running (window {window_id}) - no-op')
        return

    acp_log('daemon_session', f'_stop_daemon called for window {window_id}')
    agent_name = state.get('agent_name') or 'unknown'
    acp_log('daemon_session', f'stopping daemon for agent "{agent_name}"')

    if state.get('permission_pending'):
        dismiss_permission_prompt(window_id)

    s = state.get('loop', 'queue', 'is_busy', 'conn')
    loop, async_queue, is_busy, conn = s['loop'], s['queue'], s['is_busy'], s['conn']
    if loop is not None and async_queue is not None and not loop.is_closed():
        try:
            acp_log('daemon_session', 'posting sentinel to daemon queue')
            asyncio.run_coroutine_threadsafe(async_queue.put(None), loop)
            acp_log('daemon_session', 'sentinel posted (fire-and-forget)')
        except Exception as exc:
            acp_log('daemon_session', f'failed to post sentinel: {exc}')

    if is_busy and conn is not None and loop is not None and not loop.is_closed():
        try:
            asyncio.run_coroutine_threadsafe(close_writer(conn.writer), loop)
            acp_log('daemon_session', 'writer close scheduled (busy prompt cancel)')
        except Exception as exc:
            acp_log('daemon_session', f'failed to close writer: {exc}')

    thread = state.get('thread')
    if thread is not None:
        acp_log('daemon_session', f'joining daemon thread (timeout={join_timeout}s)')
        thread.join(timeout=join_timeout)
        if thread.is_alive():
            acp_log('daemon_session', f'thread join TIMED OUT - thread still alive after {join_timeout}s')
            proc = state.get('proc')
            if proc is not None:
                try:
                    signal_process_group(proc.pid, kill=True)
                except Exception as exc:
                    acp_log('daemon_session', f'proc.kill() failed: {exc}')
        else:
            acp_log('daemon_session', 'thread joined successfully')
    else:
        acp_log('daemon_session', 'no thread in daemon state')

    if state.is_running():
        acp_log('daemon_session', 'daemon still marked running after join - forcing reset')
        state.reset(_stop_idle_timer)
        if get_state(window_id) is state:
            remove_state(window_id)
    else:
        acp_log('daemon_session', 'daemon state already cleaned up by thread exit')


def _stop_daemon_async(window_id: int, on_done: Callable[[], None] | None = None) -> None:
    """Stop a daemon session asynchronously.

    Args:
        window_id: Window ID for the daemon to stop.
        on_done: Optional callback to run after stopping completes.
    """
    def _task():
        _stop_daemon(window_id)
        if on_done is not None:
            ui.on_main(on_done)
    threading.Thread(target=_task, daemon=True).start()
