"""Sublime command classes - wiring between modules."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from textwrap import dedent
from typing import Any, Callable

import sublime
import sublime_plugin

from ..protocol import acp_log
from . import broadcast, cache, file_walker, ui
from .config import (
    CACHE_TTL_DEFAULT,
    DEFAULT_TIMEOUT,
    INPUT_VIEW_NAME,
    ONE_SHOT_PROMPT,
    SESSION_PROMPT,
    STATUS_KEY_DAEMON,
    STATUS_KEY_NOTIFY,
    STATUS_KEY_USAGE,
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
    list_daemon_sessions,
    new_daemon_session,
    set_state,
    switch_daemon_session,
)


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


def _format_local_time(value: Any) -> str | None:
    """Convert an ACP UTC timestamp to local time for display, or ``None``."""
    if value is None or value == '':
        return None
    try:
        from datetime import datetime, timezone
        if isinstance(value, (int, float)):
            ts = value / 1000.0 if value > 1e11 else float(value)
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M')
        text = str(value).strip().replace('Z', '+00:00')
        if not text:
            return None
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime('%Y-%m-%d %H:%M')
    except (ValueError, OverflowError, OSError):
        pass
    return str(value)


def _display_title(session: dict) -> str:
    """Return a session title, hiding auto-generated system-prompt titles."""
    from .config import SESSION_PROMPT
    title = (session.get('title') or '').strip()
    if not title:
        return session.get('sessionId', 'untitled')
    normalized_title = ' '.join(title.split())
    prompt_head = ' '.join(SESSION_PROMPT.split())
    if normalized_title.startswith(prompt_head[:40]) or prompt_head[:60] in normalized_title:
        return '(no title)'
    return title or session.get('sessionId', 'untitled')


def _input_panel_kwargs(cmd, model, env, timeout, system_prompt,
                        session_id=None, use_daemon=False, auth=None):
    """Build the kwargs dict for the ``acp_input`` command."""
    return {
        'cmd': cmd,
        'model': model,
        'env': env or {},
        'timeout': timeout or DEFAULT_TIMEOUT,
        'system_prompt': system_prompt,
        'session_id': session_id,
        'use_daemon': use_daemon,
        'auth': auth,
    }


def _acp_input_kwargs(state, system_prompt=''):
    """Build kwargs dict for the ``acp_input`` command targeting a running daemon."""
    return _input_panel_kwargs(
        cmd=state.get('agent_cmd') or [],
        model=settings().get('model'),
        env=state.get('env') or {},
        timeout=settings().get('timeout', DEFAULT_TIMEOUT),
        system_prompt=settings().get('system_prompt') or system_prompt or '',
        session_id=state.get('session_id'),
        use_daemon=True,
        auth=state.get('auth'),
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
                lambda p: _execute_prompt_daemon(p, self.window.active_view(),
                                                 force_selection=True),
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
        system_prompt = settings().get('system_prompt') or ONE_SHOT_PROMPT
        agent_name = cmd_item.get('title', cmd[0])

        _dispatch_action(
            self.window, action,
            lambda p: self.execute(p, cmd, model, env, timeout, agent_name, auth,
                                   system_prompt=system_prompt,
                                   force_selection=True),
            _input_panel_kwargs(cmd, model, env, timeout, system_prompt, auth=auth),
        )

    def execute(self, prompt, cmd, model, env, timeout, agent_name='', auth=None,
                system_prompt=None, force_selection=False):
        """Execute a prompt against an agent, routing to daemon if one is running."""
        win = self.window
        state = get_state(win.id())
        source_view = win.active_view()
        if state is not None and state.is_running():
            _execute_prompt_daemon(prompt, source_view,
                                   force_selection=force_selection)
            return
        execute_prompt(
            window=win,
            source_view=source_view,
            prompt=prompt, cmd=cmd, model=model, env=env,
            timeout=timeout, system_prompt=system_prompt or ONE_SHOT_PROMPT,
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


class AcpInputCommand(sublime_plugin.WindowCommand):
    """Shows the prompt input panel with @ file autocomplete and runs the ACP command."""

    def run(self, cmd=None, model=None, env=None, timeout=None,
            system_prompt=None, initial_text='',
            use_daemon=False, session_id=None, auth=None):
        """Open a prompt input panel with ``@`` file and ``/`` slash-command autocomplete.

        Args:
            cmd: Agent command list.
            model: Optional model override.
            env: Environment variables for the agent subprocess.
            timeout: Prompt timeout in seconds.
            system_prompt: System prompt to prepend.
            initial_text: Pre-filled text in the input panel.
            use_daemon: Whether to route the prompt to a running daemon.
            session_id: Session ID to continue.
            auth: Authentication flag override.
        """
        exec_state = _input_panel_kwargs(
            cmd, model, env, timeout, system_prompt, session_id, use_daemon, auth
        )

        agents = _load_agents()
        slash_commands = agents.get(cmd[0] if cmd else '', {}).get('commands')

        def on_cancel():
            pass

        def on_done(text):
            if not exec_state.get('cmd'):
                sublime.error_message('ACP: No command configured')
                return
            self._execute_direct(text, exec_state)

        caption = '✨'
        input_view = self.window.show_input_panel(
            caption, initial_text, on_done, None, on_cancel
        )
        if input_view:
            input_view.set_name(INPUT_VIEW_NAME)
            input_view.settings().set('auto_complete', True)
            input_view.settings().set('auto_complete_selector', 'text')
            input_view.settings().set('acp_slash_commands', slash_commands or [])
        # Record the input view on this window's daemon state (if any)
        daemon_state = get_state(self.window.id())
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
        source_view = (_find_view_with_selection(self.window)
                       if settings().get('attach_selection', False)
                       else None)
        if source_view is None:
            source_view = self.window.active_view()
        if state.get('use_daemon'):
            _execute_prompt_daemon(prompt, source_view)
            return
        execute_prompt(
            window=self.window,
            source_view=source_view,
            prompt=prompt, cmd=state['cmd'], model=state['model'],
            env=state['env'], timeout=state['timeout'],
            system_prompt=state.get('system_prompt', ''),
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
            _stop_daemon_async(
                window_id,
                on_done=lambda: _pick_agent_command(self.window, self.on_select),
            )
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
        system_prompt = settings().get('system_prompt') or SESSION_PROMPT
        agent_name = cmd_item.get('title', cmd[0])
        work_dir = ui.resolve_work_dir(self.window, self.window.active_view())

        # Create the output view early so the spinner and errors have a target
        output_view = ui.create_output_view(self.window, agent_name, role='daemon')
        ui.open_split_for_output(self.window, output_view)

        window_id = self.window.id()

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
            args=(window_id, cmd, agent_name, env, model, system_prompt,
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
                broadcast.set_broadcast_status(STATUS_KEY_DAEMON, f'✗ Failed to initialize agent "{agent_name}"', daemon_window)
                ui.on_main(lambda: broadcast.erase_broadcast_status(STATUS_KEY_DAEMON, daemon_window), 5000)
                return

            if not state.get('is_busy'):
                broadcast.set_broadcast_status(STATUS_KEY_DAEMON, broadcast.daemon_status_text(agent_name, state.get('agent_cmd')), daemon_window)
                _start_idle_timer(window_id)
                self.window.run_command(
                    'acp_input', _acp_input_kwargs(state, system_prompt=SESSION_PROMPT)
                )

        def is_init_done():
            st = get_state(window_id)
            return st is None or not st.is_running() or not st.get('is_busy')

        broadcast.show_spinner(
            view,
            is_init_done,
            f'{agent_name} initializing',
            on_done=on_done,
        )


class AcpSwitchSessionCommand(sublime_plugin.WindowCommand):
    """List recent sessions and switch the running daemon to the selected one."""

    def is_enabled(self):
        """Enable only while the daemon is running and idle."""
        state = get_state(self.window.id())
        return (
            state is not None and state.is_running()
            and not state.get('is_busy')
        )

    def run(self):
        """List the active agent's recent sessions and switch on selection."""
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
        if not sessions:
            sublime.status_message('ACP: no previous sessions found')
            return
        self._sessions = sessions
        items = [['+ Start new session', 'Start fresh without restarting the agent']]
        items.extend(self._label(s) for s in sessions)
        self.window.show_quick_panel(items, self._on_pick, placeholder='Select session to switch to')

    @staticmethod
    def _label(s):
        title = _display_title(s)
        updated = _format_local_time(s.get('updatedAt'))
        sid = s.get('sessionId', '')
        detail = f'{sid[-8:]}' if len(sid) > 8 else sid
        if updated:
            detail = f'{updated} · {detail}'
        return [title, detail]

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


class _AcpSwitchConfigOptionCommand(sublime_plugin.WindowCommand):
    """Base for commands that switch a ``session/set_config_option`` value.

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
        cmd = state.get('agent_cmd')
        if not cmd:
            return None
        agents = _load_agents()
        config_options = agents.get(cmd[0], {}).get('config_options') or []
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

    def run(self) -> None:
        """Show a quick panel of available options and apply the selection."""
        state = get_state(self.window.id())
        if state is None or not state.is_running():
            sublime.status_message('ACP: No agent session running')
            return
        if state.get('is_busy'):
            sublime.status_message(
                'ACP: Wait for the current prompt to finish before switching'
            )
            return
        opt = self._config_option()
        if opt is None:
            sublime.status_message(
                f'ACP: Active agent does not support {self.label.lower()} switching'
            )
            return
        config_id = opt.get('id') or self.config_id
        options = opt.get('options') or []
        current = opt.get('currentValue')

        items = []
        selected_index = 0
        for i, o in enumerate(options):
            name = o.get('name') or o.get('value', '')
            value_str = o.get('value', '')
            if o.get('value') == current:
                name = f'✓ {name}'
                selected_index = i
            items.append([name, value_str])

        def on_select(index: int) -> None:
            if index == -1:
                return
            self._apply_option(config_id, options[index].get('value', ''))
            st = get_state(self.window.id())
            if st is not None:
                input_view = st.get('input_view')
                if input_view is not None and input_view.window() is not None:
                    self.window.focus_view(input_view)

        self.window.show_quick_panel(
            items, on_select, selected_index=selected_index,
            placeholder=f'Select {self.label.lower()}'
        )

    def _apply_option(self, config_id: str, value: str) -> None:
        """Send ``session/set_config_option`` for :attr:`config_id` and update the cache."""
        state = get_state(self.window.id())
        if state is None or not state.is_running():
            sublime.status_message('ACP: No daemon running')
            return
        if state.get('is_busy'):
            sublime.status_message(
                'ACP: Wait for the current prompt to finish before switching'
            )
            return
        s = state.get('conn', 'loop', 'session_id', 'agent_cmd')
        conn, loop, session_id, agent_cmd = s['conn'], s['loop'], s['session_id'], s['agent_cmd']
        if conn is None or loop is None or loop.is_closed():
            sublime.status_message('ACP: Daemon connection not available')
            return

        label = self.label
        agent_name = state.get('agent_name') or 'agent'

        async def _send() -> None:
            if state.get('is_busy'):
                acp_log('switch_config', f'skip {config_id} change: daemon busy')
                return
            try:
                response = await conn.send_request('session/set_config_option', {
                    'sessionId': session_id,
                    'configId': config_id,
                    'value': value,
                })
                confirmed = value
                if isinstance(response, dict):
                    confirmed = (
                        response.get('currentValue')
                        or response.get('value')
                        or value
                    )
                cache_dir = Path(sublime.cache_path()) / 'ACP'
                with cache.cache_lock:
                    agents = _load_agents()
                    entry = agents.get(agent_cmd[0], {})
                    for opt in entry.get('config_options') or []:
                        if opt.get('id') == config_id:
                            opt['currentValue'] = confirmed
                            break
                    agents[agent_cmd[0]] = entry
                    cache.save_agents(cache_dir, agents)
                acp_log(
                    f'switch_{config_id}',
                    f'{label.lower()} changed to {confirmed!r} (session={session_id})',
                )
                sublime.set_timeout(
                    lambda v=confirmed: broadcast.set_broadcast_status(
                        STATUS_KEY_NOTIFY, f'ACP: {label} -> {v}'
                    ), 0
                )
                sublime.set_timeout(
                    lambda: broadcast.erase_broadcast_status(STATUS_KEY_NOTIFY), 5000
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
                        lambda: broadcast.erase_broadcast_status(
                            STATUS_KEY_USAGE, self.window
                        ), 0
                    )
            except Exception as exc:
                acp_log(
                    f'switch_{config_id}',
                    f'failed to set {config_id} {value!r}: {type(exc).__name__}: {exc}',
                )
                sublime.set_timeout(
                    lambda e=exc: broadcast.set_broadcast_status(
                        STATUS_KEY_NOTIFY, f'ACP: Failed to set {label.lower()}: {e}'
                    ), 0
                )
                sublime.set_timeout(
                    lambda: broadcast.erase_broadcast_status(STATUS_KEY_NOTIFY), 5000
                )

        asyncio.run_coroutine_threadsafe(_send(), loop)


class AcpSwitchModelCommand(_AcpSwitchConfigOptionCommand):
    """Switch the active model on a running daemon session."""

    config_id = 'model'
    label = 'Model'


class AcpSwitchModeCommand(_AcpSwitchConfigOptionCommand):
    """Switch the active session mode on a running daemon session."""

    config_id = 'mode'
    label = 'Mode'


class AcpSwitchThoughtLevelCommand(_AcpSwitchConfigOptionCommand):
    """Switch the reasoning effort on a running daemon session."""

    config_category = 'thought_level'
    label = 'Thought level'


class AcpInterruptCommand(sublime_plugin.WindowCommand):
    """Interrupt the current agent prompt without stopping the daemon."""

    def is_enabled(self):
        """Enable whenever a daemon is running in this window."""
        return _daemon_running(self.window.id())

    def run(self):
        """Interrupt the current agent prompt without stopping the daemon."""
        window_id = self.window.id()
        state = get_state(window_id)
        if state is None or not state.is_running() or not state.get('is_busy'):
            sublime.status_message('No agent prompt in progress')
            return
        s = state.get('conn', 'loop', 'session_id')
        conn, loop, sid = s['conn'], s['loop'], s['session_id']
        if conn is not None and loop is not None and not loop.is_closed():
            state.set(has_replied=False)
            try:
                msg_id = conn.last_request_id
                if msg_id is not None:
                    asyncio.run_coroutine_threadsafe(
                        conn.cancel_pending_request(msg_id, sid), loop,
                    )
                    sublime.status_message('ACP: Interrupted')
            except RuntimeError:
                sublime.status_message('ACP: daemon already stopped')
        if output_view := state.get('output_view'):
            ui.append_to_output_view(output_view, '\n*[Interrupted]*\n')
