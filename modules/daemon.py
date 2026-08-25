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
from typing import Callable

import sublime

from ..protocol import (
    STATUS_ERROR,
    STATUS_LOADED,
    STATUS_NEW,
    STATUS_RESUMED,
    acp_log,
    cleanup_process,
    close_writer,
    signal_process_group,
)
from . import broadcast, cache, ui
from .config import (
    DEFAULT_PERMISSIONS,
    IDLE_TIMEOUT_DEFAULT,
    IDLE_TIMER_INTERVAL,
    PERMISSION_PROMPT_TIMEOUT,
    STATUS_KEY_DAEMON,
    TURN_DIVIDER,
)
from .config import settings as load_settings
from .permissions import dismiss_permission_prompt, resolve_permission
from .rpc import (
    _extract_available_commands,
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

    def is_running(self) -> bool:
        with self._lock:
            return bool(self.running)

    def set(self, **kwargs) -> None:
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)

    def get(self, *keys: str):
        if not keys:
            raise ValueError("DaemonState.get() requires at least one key")
        with self._lock:
            d = {k: getattr(self, k, None) for k in keys}
            return d[keys[0]] if len(keys) == 1 else d

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
            win = output_view.window() if output_view else None
            broadcast.erase_broadcast_status(STATUS_KEY_DAEMON, win)

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


def stop_all_daemons(stop_func) -> None:
    """Stop all running daemons across all windows."""
    wids = list(running_windows())
    threads = [
        threading.Thread(target=stop_func, args=(wid, 2.0), daemon=True)
        for wid in wids
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2.5)

# Cache helpers

def _cache_dir() -> Path:
    """Get the ACP cache directory path."""
    return Path(sublime.cache_path()) / 'ACP'


def _update_agent_session_id(cmd, session_id):
    """Update the cached session ID for an agent command."""
    try:
        cache.update_session_id(_cache_dir(), cmd, session_id)
    except Exception as e:
        sublime.status_message(f'✗ Failed to update session ID: {e}')


def _clear_agent_session_id(cmd):
    """Invalidate the cached session ID for an agent command."""
    try:
        cache.clear_session_id(_cache_dir(), cmd)
    except Exception as e:
        sublime.status_message(f'✗ Failed to clear session ID: {e}')


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


def _install_notification_handler(conn, cmd):
    """Keep a persistent notification handler on the daemon connection.

    Persists ``available_commands_update`` payloads to the agent cache as they
    arrive, so commands are captured even when the agent announces them after
    the init phase has returned. Other notifications are ignored here; prompt
    streaming installs its own callbacks via ``swap_callbacks``, which
    restores this handler afterwards.
    """
    update_commands = _make_commands_updater(cmd)

    def on_notification(method: str, params: dict) -> None:
        matched, commands = _extract_available_commands(method, params)
        if matched:
            acp_log('daemon', f'available_commands_update ({len(commands or [])} commands)')
            update_commands(commands)

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
        if ui.dividers_enabled():
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
    session_id: str | None = None,
    permissions_config: dict | None = None,
    auth: bool | None = None,
) -> None:
    """Main daemon thread function for persistent agent sessions.

    Spawns the agent process, initializes the session, and processes prompts
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
        session_id: Optional session ID to resume.
        permissions_config: Optional permissions configuration.
        auth: Optional authentication flag.
    """
    acp_log(
        'daemon_session',
        f'daemon thread started for "{agent_name}" (thread={threading.current_thread().ident})'
    )
    acp_log('daemon_session', f'cmd={cmd}, session_id={session_id}')

    state = get_state(window_id)
    if state is None:
        acp_log('daemon_session', f'no daemon state for window {window_id} - creating')
        state = DaemonState()
        set_state(window_id, state)

    loop, async_queue = _setup_async_loop_and_queue(state)
    stream_callback = ui.make_stream_callback(state.get('output_view'), state)

    async def _wrapper():
        current_env = _build_env(env)
        proc = conn = None
        acp_log('daemon_session', 'calling spawn_and_init()')
        result = await spawn_and_init(cmd, current_env, model, session_id, work_dir,
                                       permissions_config=permissions_config,
                                       auth=auth)
        if result is None:
            acp_log('daemon_session', 'spawn_and_init returned None - init failed')
            if session_id:
                _clear_agent_session_id(cmd)
            return None, 'error'
        proc, conn, init_result = result
        try:
            sid = init_result['session_id']
            opened_via = init_result.get('opened_via', STATUS_NEW)
            acp_log('daemon_session', f'spawn_and_init succeeded: session_id={init_result.get("session_id")}, opened_via={init_result.get("opened_via")}, proc={proc.pid if proc else None}')
            _cache_daemon_agent_info(cmd, init_result)
            _install_notification_handler(conn, cmd)

            state.set(
                proc=proc, conn=conn,
                session_id=sid, is_busy=False,
            )

            session_error = init_result.get('session_error')
            if session_id:
                if opened_via in (STATUS_RESUMED, STATUS_LOADED):
                    msg = f'\n*[Resumed session: {sid}]*\n\n'
                elif session_error:
                    msg = f'\n**[{session_error}]**\n*[Started new session]*\n\n'
                else:
                    msg = '\n*[Started new session]*\n\n'
                ui.on_main(
                    lambda v=output_view, m=msg: ui.append_to_output_view(v, m),
                )

            first_prompt = True
            while True:
                item = await async_queue.get()
                if item is None:
                    acp_log('daemon_session', 'received sentinel - breaking prompt loop')
                    async_queue.task_done()
                    break

                prompt_text = item
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
                )
                if not ok:
                    dismiss_permission_prompt(window_id)
                first_prompt = False

                acp_log('daemon_session', 'prompt completed')
                async_queue.task_done()
                ui.append_turn_divider(output_view)
                state.set(
                    is_busy=False, last_activity=time.monotonic(),
                )
                ui.on_main(
                    lambda ov=output_view: ui.reopen_daemon_input_panel(
                        cmd, model, timeout, system_prompt, state.get('session_id'),
                        agent_name, ov.window(),
                        env=state.get('env') or {},
                        auth=state.get('auth'),
                    ),
                )

            acp_log('daemon_session', 'exiting prompt loop normally')
        finally:
            acp_log('daemon_session', 'entering _wrapper() finally - cleaning up')
            if conn is not None:
                await conn.close()
            if proc is not None:
                await cleanup_process(proc, conn.writer if conn is not None else None)
            acp_log('daemon_session', '_wrapper() finally - cleanup done')

        return sid, 'new'

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
