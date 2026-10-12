"""Sublime command classes - wiring between modules."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Callable

import sublime
import sublime_plugin

from ..protocol import acp_log
from . import broadcast, cache, debug_panel, file_walker, ui
from .config import (
    CACHE_TTL_DEFAULT,
    DEFAULT_TIMEOUT,
    INPUT_VIEW_NAME,
    ONE_SHOT_PROMPT,
    SESSION_PROMPT,
    STATUS_KEY_DAEMON,
    resolve_session_prompt,
    settings,
)
from .daemon import (
    DaemonState,
    _daemon_thread_main,
    _execute_prompt_daemon,
    _load_permissions,
    _start_idle_timer,
    _stop_daemon_async,
    execute_prompt,
    get_state,
    is_unloading,
    set_state,
)
from .daemon_sessions import refresh_session_cache


def _find_action_prompt(action: str) -> str | None:
    """Return the prompt string for *action* from the ``actions`` settings, or ``None``."""
    if not action:
        return None
    for a in settings().get('actions') or []:
        if a.get('title') == action:
            return a.get('prompt')
    return None


def _daemon_running(window_id: int) -> bool:
    """Return ``True`` when a daemon is registered and running in *window_id*."""
    state = get_state(window_id)
    return state is not None and state.is_running()


def _find_view_with_selection(window: sublime.Window) -> sublime.View | None:
    v = window.active_view_in_group(window.active_group())
    if not v or v.settings().get('is_widget') or v.sel()[0].empty():
        return None
    return v


def _pick_agent_command(window: sublime.Window, on_select: Callable) -> None:
    """Show the predefined agents quick panel; auto-pick when only one entry.

    Calls ``on_select(cmd_item)`` on the main thread with the chosen command
    dict, or ``on_select(None)`` if the user cancels.
    """
    commands = settings().get('commands', [])
    if not commands:
        sublime.error_message('No predefined commands found in settings')
        return

    if len(commands) == 1:
        on_select(commands[0])
        return

    items = [item['title'] for item in commands]
    window.show_quick_panel(items, lambda i: on_select(None if i == -1 else commands[i]), placeholder='Select agent')


def _load_agents() -> dict:
    """Load the persisted per-agent cache (session IDs, config options, slash commands).

    Runs on the main thread, so it reads the configured ``cache_ttl`` itself;
    background threads must use ``cache.load_agents`` with an explicit ttl
    (or the default) and never touch settings under ``cache_lock``.
    """
    return cache.load_agents(
        Path(sublime.cache_path()) / 'ACP',
        ttl=settings().get('cache_ttl', CACHE_TTL_DEFAULT),
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


def _input_panel_kwargs(cmd, model, env, timeout, session_prompt,
                        session_id=None, use_daemon=False, auth=None,
                        daemon_window_id=None):
    """Build the kwargs dict for the ``acp_input`` command."""
    return {
        'cmd': cmd,
        'model': model,
        'env': env or {},
        'timeout': timeout or DEFAULT_TIMEOUT,
        'session_prompt': session_prompt,
        'session_id': session_id,
        'use_daemon': use_daemon,
        'auth': auth,
        'daemon_window_id': daemon_window_id,
    }


def _acp_input_kwargs(state, session_prompt=''):
    """Build kwargs dict for the ``acp_input`` command targeting a running daemon."""
    return _input_panel_kwargs(
        cmd=state.get('agent_cmd') or [],
        model=settings().get('model'),
        env=state.get('env') or {},
        timeout=settings().get('timeout', DEFAULT_TIMEOUT),
        session_prompt=resolve_session_prompt(settings().get('session_prompt'), session_prompt),
        session_id=state.get('session_id'),
        use_daemon=True,
        auth=state.get('auth'),
        daemon_window_id=state.get('window_id'),
    )


def _dispatch_action(window, action, run_prompt, panel_kwargs):
    """Run *action*'s mapped prompt via *run_prompt*, or open the input panel."""
    action_prompt = _find_action_prompt(action)
    if action_prompt is not None:
        run_prompt(action_prompt)
    else:
        window.run_command('acp_input', panel_kwargs)


class AcpCommand(sublime_plugin.WindowCommand):
    """Execute an ACP one-shot prompt or route to the running daemon.

    Delegates to the input panel if no action is specified, or runs the
    prompt directly when a matching action shortcut is found.
    """

    def run(self, action=None):
        """Execute an ACP prompt or route to a running daemon.

        Args:
            action: Optional action name for shortcut prompts.
        """
        if is_unloading():
            sublime.status_message('ACP is reloading, please retry in a moment')
            return
        window_id = self.window.id()

        # If this window has a running daemon, route to it
        state = get_state(window_id)
        if state is not None and state.is_running():
            _dispatch_action(
                self.window, action,
                lambda p: _execute_prompt_daemon(p, self.window.active_view(), force_selection=True),
                _acp_input_kwargs(state),
            )
            return

        _pick_agent_command(self.window, lambda cmd_item: self.on_select(cmd_item, action))

    def on_select(self, cmd_item, action=None):
        """Handle agent selection from the quick panel.

        Builds the command, model, env, and timeout from the selected item,
        then executes the prompt or opens the input panel.

        Args:
            cmd_item: The selected agent command dict, or ``None`` if cancelled.
            action: Optional action name for shortcut prompts.
        """
        if cmd_item is None:
            return
        cmd_str = cmd_item.get('cmd')
        if not cmd_str:
            sublime.error_message("ACP: agent entry is missing a 'cmd' value.")
            return
        cmd = [cmd_str] + cmd_item.get('args', [])
        model = cmd_item.get('model')
        env = cmd_item.get('env', {})
        auth = cmd_item.get('auth', None)
        timeout = cmd_item.get('timeout', settings().get('timeout', DEFAULT_TIMEOUT))
        session_prompt = resolve_session_prompt(settings().get('session_prompt'), ONE_SHOT_PROMPT)
        agent_name = cmd_item.get('title', cmd[0])

        _dispatch_action(
            self.window, action,
            lambda p: self.execute(p, cmd, model, env, timeout, agent_name, auth,
                                   session_prompt=session_prompt,
                                   force_selection=True),
            _input_panel_kwargs(cmd, model, env, timeout, session_prompt, auth=auth),
        )

    def execute(self, prompt, cmd, model, env, timeout, agent_name='', auth=None,
                session_prompt=None, force_selection=False):
        """Execute a prompt against an agent, routing to daemon if one is running."""
        win = self.window
        state = get_state(win.id())
        source_view = win.active_view()
        if state is not None and state.is_running():
            _execute_prompt_daemon(prompt, source_view, force_selection=force_selection)
            return
        execute_prompt(
            window=win,
            source_view=source_view,
            prompt=prompt, cmd=cmd, model=model, env=env,
            timeout=timeout, session_prompt=resolve_session_prompt(session_prompt, ONE_SHOT_PROMPT),
            agent_name=agent_name,
            settings=settings(),
            auth=auth,
            force_selection=force_selection,
        )


class AcpActionsCommand(sublime_plugin.WindowCommand):
    """Show a quick panel of available actions from the ``actions`` setting."""

    def run(self):
        """Show the quick panel of available actions."""
        actions = settings().get('actions') or []
        titles = [a['title'] for a in actions if a.get('title')]
        if not titles:
            return

        def on_done(index):
            if index != -1:
                self.window.run_command('acp', {'action': titles[index]})

        self.window.show_quick_panel(titles, on_done, placeholder='Select action')

    def is_visible(self):
        """Show only when text is selected and actions are configured."""
        view = self.window.active_view()
        if view is None:
            return False
        if not view.has_non_empty_selection_region():
            return False
        return bool(settings().get('actions'))


_pending_drafts: dict[int, str] = {}


class AcpInputCommand(sublime_plugin.WindowCommand):
    """Shows the prompt input panel with @ file autocomplete and runs the ACP command."""

    def run(self, cmd=None, model=None, env=None, timeout=None,
            session_prompt=None, initial_text='',
            use_daemon=False, session_id=None, auth=None,
            daemon_window_id=None):
        """Open a prompt input panel with ``@`` file and ``/`` slash-command autocomplete.

        Args:
            cmd: Agent command list.
            model: Optional model override.
            env: Environment variables for the agent subprocess.
            timeout: Prompt timeout in seconds.
            session_prompt: Prompt to prepend.
            initial_text: Pre-filled text in the input panel.
            use_daemon: Whether to route the prompt to a running daemon.
            session_id: Session ID to continue.
            auth: Authentication flag override.
            daemon_window_id: Owning daemon window id; the panel's prompt
                routes there even if this command runs in another window.
        """
        exec_state = _input_panel_kwargs(
            cmd, model, env, timeout, session_prompt, session_id, use_daemon, auth, daemon_window_id,)

        agents = _load_agents()
        slash_commands = (cache.get_entry(agents, cmd) if cmd else {}).get('commands')

        owner_id = daemon_window_id if isinstance(daemon_window_id, int) else self.window.id()
        if not initial_text:
            initial_text = _pending_drafts.get(owner_id, '')

        def on_change(text):
            _pending_drafts[owner_id] = text

        def on_cancel():
            pass

        def on_done(text):
            _pending_drafts.pop(owner_id, None)
            if not exec_state.get('cmd'):
                sublime.error_message('ACP: No command configured')
                return
            self._execute_direct(text, exec_state)

        caption = '✨'
        input_view = self.window.show_input_panel(
            caption, initial_text, on_done, on_change, on_cancel
        )
        if input_view:
            input_view.set_name(INPUT_VIEW_NAME)
            input_view.settings().set('auto_complete', True)
            input_view.settings().set('auto_complete_selector', 'text')
            input_view.settings().set('acp_slash_commands', slash_commands or [])
        daemon_state = get_state(owner_id)
        if daemon_state is not None:
            daemon_state.set(input_view=input_view)
        # Warm the file cache early so @ completions work on first try
        file_walker.load_project_files_for_window(self.window, settings())

    def _execute_direct(self, prompt, state):
        """Execute the ACP prompt via the given invocation state.

        Args:
            prompt: The prompt text submitted from the input panel.
            state: The ``exec_state`` dict captured by the submitting input panel.
        """
        if state.get('use_daemon'):
            owner_id = state.get('daemon_window_id')
            if isinstance(owner_id, int):
                owner = ui.window_by_id(owner_id)
                if owner is None:
                    sublime.status_message('ACP: Owning window closed - prompt not sent')
                    return
                target = owner
            else:
                target = self.window
            source_view = (
                _find_view_with_selection(target) if settings().get('attach_selection', False) else None
            )
            if source_view is None:
                source_view = target.active_view()
            _execute_prompt_daemon(prompt, source_view)
            return
        source_view = (
            _find_view_with_selection(self.window) if settings().get('attach_selection', False) else None
        )
        if source_view is None:
            source_view = self.window.active_view()
        execute_prompt(
            window=self.window,
            source_view=source_view,
            prompt=prompt, cmd=state['cmd'], model=state['model'],
            env=state['env'], timeout=state['timeout'],
            session_prompt=resolve_session_prompt(state.get('session_prompt'), ONE_SHOT_PROMPT),
            session_id=state.get('session_id'),
            agent_name=state['cmd'][0],
            settings=settings(),
            auth=state.get('auth'),
        )


class AcpStartCommand(sublime_plugin.WindowCommand):
    """Start an ACP agent as a persistent background daemon."""

    def is_enabled(self):
        """Enable only when no daemon is running in this window."""
        return not _daemon_running(self.window.id())

    def run(self):
        """Start a persistent agent daemon in the current window."""
        if is_unloading():
            sublime.status_message('ACP is reloading, please retry in a moment')
            return
        window_id = self.window.id()
        state = get_state(window_id)
        if state is not None and state.is_running():
            old_agent = state.get('agent_name') or 'unknown'
            sublime.status_message(f'Stopping {old_agent}...')
            _stop_daemon_async(window_id, on_done=lambda: _pick_agent_command(self.window, self.on_select),)
            return

        _pick_agent_command(self.window, self.on_select)

    def on_select(self, cmd_item):
        """Handle agent selection from the quick panel and start the daemon.

        Builds the command, model, env, and timeout from the selected item,
        creates the output view, registers daemon state, and spawns the
        background daemon thread.

        Args:
            cmd_item: The selected agent command dict, or ``None`` if cancelled.
        """
        if cmd_item is None:
            return
        cmd_str = cmd_item.get('cmd')
        if not cmd_str:
            sublime.error_message("ACP: agent entry is missing a 'cmd' value.")
            return
        cmd = [cmd_str] + cmd_item.get('args', [])
        model = cmd_item.get('model') or settings().get('model')
        env = cmd_item.get('env', {})
        auth = cmd_item.get('auth', None)  # None means default behavior
        timeout = cmd_item.get('timeout', settings().get('timeout', DEFAULT_TIMEOUT))
        session_prompt = resolve_session_prompt(settings().get('session_prompt'), SESSION_PROMPT)
        agent_name = cmd_item.get('title', cmd[0])
        work_dir = ui.resolve_work_dir(self.window, self.window.active_view())

        # Create the output view early so the spinner and errors have a target
        output_view = ui.create_output_view(self.window, agent_name, role='daemon')
        ui.open_split_for_output(self.window, output_view)

        window_id = self.window.id()
        debug_panel.clear_window_log(window_id)

        # Register per-window daemon state BEFORE spawning the thread (avoids race)
        state = DaemonState()
        state.set(
            running=True,
            agent_cmd=cmd,
            agent_name=agent_name,
            window_id=window_id,
            output_view=output_view,
            last_activity=time.monotonic(),
            is_busy=True,  # busy during init
            env=env,
            auth=auth,
        )
        set_state(window_id, state)

        thread = threading.Thread(
            target=_daemon_thread_main,
            args=(window_id, cmd, agent_name, env, model, session_prompt,
                  work_dir, timeout, output_view, settings(),
                  _load_permissions(settings()),
                  auth),
            daemon=True,
        )
        state.set(thread=thread)
        thread.start()

        # Start spinner on the output view
        # Poll until the daemon is out of the init phase
        self._poll_init(output_view, agent_name, window_id)

    def _poll_init(self, view, agent_name, window_id):
        """Poll daemon state. On init done -> show ready. On init fail -> clean up."""
        daemon_window = view.window()

        def on_done():
            state = get_state(window_id)
            if state is None or not state.is_running():
                acp_log('daemon_session', f'init failed for "{agent_name}" - no running daemon', window_id)
                broadcast.set_broadcast_status(STATUS_KEY_DAEMON, f'✗ Failed to initialize agent "{agent_name}"', daemon_window)
                ui.on_main(lambda: broadcast.erase_broadcast_status(STATUS_KEY_DAEMON, daemon_window), 5000)
                return

            if not state.get('is_busy'):
                acp_log('daemon_session', f'init completed for "{agent_name}"', window_id)
                broadcast.set_broadcast_status(STATUS_KEY_DAEMON, broadcast.daemon_status_text(agent_name, state.get('agent_cmd')), daemon_window)
                _start_idle_timer(window_id)
                refresh_session_cache(window_id)
                self.window.run_command('acp_input', _acp_input_kwargs(state, session_prompt=SESSION_PROMPT))

        def is_init_done():
            st = get_state(window_id)
            return st is None or not st.is_running() or not st.get('is_busy')

        broadcast.show_spinner(
            view,
            is_init_done,
            f'{agent_name} initializing',
            on_done=on_done,
        )


class AcpStopCommand(sublime_plugin.WindowCommand):
    """Terminate the running agent daemon."""

    def is_enabled(self):
        """Enable only when a daemon is running in this window."""
        return _daemon_running(self.window.id())

    def run(self):
        """Terminate the running agent daemon via a background stop thread."""
        window_id = self.window.id()
        state = get_state(window_id)
        if state is None or not state.is_running():
            sublime.status_message('No agent session running in this window')
            return
        agent_name = state.get('agent_name') or 'unknown'
        _stop_daemon_async(
            window_id,
            on_done=lambda: sublime.status_message(f'✓ Agent "{agent_name}" stopped'),
        )


class AcpInterruptCommand(sublime_plugin.WindowCommand):
    """Interrupt the current agent prompt without stopping the daemon."""

    def is_enabled(self):
        """Enable only while a daemon prompt is in progress."""
        state = get_state(self.window.id())
        return state is not None and state.is_running() and bool(state.get('is_busy'))

    def is_visible(self):
        """Show only while a daemon prompt is in progress."""
        return self.is_enabled()

    def run(self):
        """Interrupt the current agent prompt without stopping the daemon."""
        window_id = self.window.id()
        state = get_state(window_id)
        if state is None or not state.is_running() or not state.get('is_busy'):
            sublime.status_message('No agent prompt in progress')
            return
        s = state.get('conn', 'loop', 'session_id', 'prompt_msg_id')
        conn, loop, sid = s['conn'], s['loop'], s['session_id']
        if conn is not None and loop is not None and not loop.is_closed():
            state.set(has_replied=False)
            try:
                prompt_msg_id = s.get('prompt_msg_id')
                msg_id = prompt_msg_id if prompt_msg_id is not None else conn.last_request_id
                if msg_id is not None:
                    asyncio.run_coroutine_threadsafe(conn.cancel_pending_request(msg_id, sid), loop)
                    source = 'prompt' if prompt_msg_id is not None else 'last-request fallback'
                    acp_log('daemon_session', f'interrupt sent: msg_id={msg_id} ({source}), sid={sid}', window_id)
                    sublime.status_message('ACP: Interrupted')
                else:
                    acp_log('daemon_session', 'interrupt skipped: no pending request id', window_id)
            except RuntimeError:
                acp_log('daemon_session', 'interrupt failed: daemon already stopped', window_id)
                sublime.status_message('ACP: daemon already stopped')
        if output_view := state.get('output_view'):
            ui.append_to_output_view(output_view, '\n*[Interrupted]*\n')


class AcpWindowCleanupListener(sublime_plugin.EventListener):
    """Drop per-window ephemeral state when a window closes."""

    def on_pre_close_window(self, window):
        _pending_drafts.pop(window.id(), None)
        ui.forget_one_shot_view(window.id())
