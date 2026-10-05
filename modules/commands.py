"""Sublime command classes - wiring between modules."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import suppress
from pathlib import Path
from textwrap import dedent
from typing import Any, Callable

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
    STATUS_KEY_NOTIFY,
    STATUS_KEY_USAGE,
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
    adopt_manual_session,
    execute_prompt,
    fork_daemon_session,
    get_daemon_session_title,
    get_state,
    is_unloading,
    list_daemon_sessions,
    new_daemon_session,
    refresh_session_cache,
    rename_daemon_session,
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


def _apply_title_overrides(cmd: list | None, sessions: list) -> None:
    """Annotate *sessions* in place with their local title overrides."""
    if not cmd or not sessions:
        return
    try:
        agents = _load_agents()
    except Exception as exc:
        acp_log('switch_session', f'failed to load title overrides: {exc!r}')
        return
    titles = agents.get(cmd[0], {}).get('session_titles') or {}
    if not titles:
        return
    for s in sessions:
        if not isinstance(s, dict):
            continue
        override = titles.get(s.get('sessionId'))
        if isinstance(override, str) and override.strip():
            s['title_override'] = override


def _display_title(session: dict) -> str:
    """Return a session title, falling back to the full session ID.

    A local override set via ``ACP: Rename Session`` is explicit user intent,
    so it wins. Otherwise the agent's ``title`` is used, unless it is a
    verbatim echo of :data:`~modules.config.SESSION_PROMPT`. The session ID is
    the final fallback.
    """
    from .config import SESSION_PROMPT

    if override := (session.get('title_override') or '').strip():
        return override
    if title := (session.get('title') or '').strip():
        normalized_title = ' '.join(title.split())
        prompt_head = ' '.join(SESSION_PROMPT.split())
        is_echo = (
            normalized_title.startswith(prompt_head[:40])
            or prompt_head[:60] in normalized_title
        )
        if not is_echo:
            return title
    return session.get('sessionId', 'untitled') or 'untitled'


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


def _window_by_id(window_id):
    """Return the open window matching *window_id*, or ``None``."""
    if not isinstance(window_id, int):
        return None
    for w in sublime.windows():
        if w.id() == window_id:
            return w
    return None


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
        slash_commands = agents.get(cmd[0] if cmd else '', {}).get('commands')

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
                owner = _window_by_id(owner_id)
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
        list_item_cls = getattr(sublime, 'ListInputItem', None)
        items = []
        new_kind = (navigation_id, '+', '')
        if list_item_cls is not None:
            items.append(list_item_cls(
                '+ Start new session', '',
                details='Start fresh without restarting the agent', kind=new_kind,
            ))
        else:  # pragma: no cover - older Sublime without ListInputItem
            items.append(sublime.QuickPanelItem(
                '+ Start new session',
                details='Start fresh without restarting the agent', kind=new_kind,
            ))
        selected_index = 0
        for i, s in enumerate(self._sessions, start=1):
            title = _display_title(s)
            sid = s.get('sessionId', '')
            updated = _format_local_time(s.get('updatedAt'))
            detail = sid if len(sid) <= 32 else f'…{sid[-16:]}'
            if updated:
                detail = f'{updated} · {detail}'
            if is_current := bool(sid) and sid == self._current:
                selected_index = i
            kind = (green_id, '✓', '') if is_current else (ambiguous_id, '', '')
            if list_item_cls is not None:
                items.append(list_item_cls(title, sid, details=detail, kind=kind))
            else:
                items.append(sublime.QuickPanelItem(title, details=detail, kind=kind))
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
            title = _display_title(s)
            sid = s.get('sessionId', '')
            updated = _format_local_time(s.get('updatedAt'))
            detail = sid if len(sid) <= 32 else f'…{sid[-16:]}'
            if updated:
                detail = f'{updated} · {detail}'
            if is_current := bool(sid) and sid == current:
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
            title = _display_title(s)
            sid = s.get('sessionId', '')
            updated = _format_local_time(s.get('updatedAt'))
            detail = sid if len(sid) <= 32 else f'…{sid[-16:]}'
            if updated:
                detail = f'{updated} · {detail}'
            if is_current := bool(sid) and sid == current:
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

    def _apply_option(self, config_id: str, value: str) -> None:
        """Send ``session/set_config_option`` for :attr:`config_id` and update the cache."""
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
                response = await conn.send_request('session/set_config_option', {
                    'sessionId': session_id,
                    'configId': config_id,
                    'value': value,
                })
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
                return await conn2.send_request('session/set_config_option', {
                    'sessionId': sid2,
                    'configId': config_id,
                    'value': value,
                })

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
                    entry = agents.get(agent_cmd[0], {})
                    entry['config_options'] = refreshed
                    agents[agent_cmd[0]] = entry
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
        list_item_cls = getattr(sublime, 'ListInputItem', None)
        items = []
        selected_index = 0
        for i, o in enumerate(self._options):
            name = o.get('name') or o.get('value', '')
            value_str = o.get('value', '')
            group = o.get('group') or ''
            if group:
                name = f'{group} / {name}'
            if is_current := o.get('value') == self._current:
                selected_index = i
            kind = (green_id, '✓', '') if is_current else (ambiguous_id, '', '')
            if list_item_cls is not None:
                items.append(list_item_cls(name, value_str, details=value_str, kind=kind))
            else:
                items.append(sublime.QuickPanelItem(name, details=value_str, kind=kind))
        return (items, selected_index)


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
                    asyncio.run_coroutine_threadsafe(conn.cancel_pending_request(msg_id, sid), loop)
                    acp_log('daemon_session', f'interrupt sent: msg_id={msg_id}, sid={sid}', window_id)
                    sublime.status_message('ACP: Interrupted')
                else:
                    acp_log('daemon_session', 'interrupt skipped: no pending request id', window_id)
            except RuntimeError:
                acp_log('daemon_session', 'interrupt failed: daemon already stopped', window_id)
                sublime.status_message('ACP: daemon already stopped')
        if output_view := state.get('output_view'):
            ui.append_to_output_view(output_view, '\n*[Interrupted]*\n')


_MANUAL_ALWAYS_METHODS = (
    'session/prompt',
    'session/cancel',
    'session/new',
    'session/set_config_option',
    'session/set_mode',
)

_MANUAL_GATED_METHODS = (
    ('session/list', 'list'),
    ('session/load', 'load'),
    ('session/resume', 'resume'),
    ('session/close', 'close'),
    ('session/delete', 'delete'),
    ('session/fork', 'fork'),
)

# Pseudo-method for a fully user-defined manual request. Appended after the
# sorted agent methods so it is always last in the quick panel.
_MANUAL_CUSTOM_METHOD = 'custom'

# Short description per method, shown in the quick panel details column.
_MANUAL_METHOD_DESCRIPTIONS = {
    'custom': 'Send a raw request with a custom method',
    'session/prompt': 'Send a prompt to the session',
    'session/cancel': 'Cancel the in-flight prompt',
    'session/new': 'Start a new session',
    'session/set_config_option': 'Set a config option (e.g. model)',
    'session/set_mode': 'Set the agent mode',
    'session/list': 'List the agent sessions',
    'session/load': 'Load a persisted session',
    'session/resume': 'Resume a session',
    'session/close': 'Close a session',
    'session/delete': 'Permanently delete a session (destructive)',
    'session/fork': 'Fork a session into a new one',
}

_MANUAL_BUSY_ALLOWED = frozenset({'session/cancel', 'session/list'})

# Methods that irreversibly change agent-side state and need confirmation
# before dispatch. ``session/close`` is deliberately absent: closed sessions
# can be resumed or reloaded, deleted ones cannot.
_MANUAL_DESTRUCTIVE_METHODS = frozenset({'session/delete'})

_MANUAL_REFRESH_CACHE = frozenset({
    'session/list', 'session/new', 'session/load', 'session/resume',
    'session/close', 'session/delete', 'session/fork',
})

# Manual methods that change agent-side state and therefore hold the daemon
# busy while in flight, mirroring the built-in commands. ``session/cancel``
# and ``session/list`` stay exempt via ``_MANUAL_BUSY_ALLOWED``.
_MANUAL_BUSY_METHODS = frozenset({
    'session/prompt', 'session/new', 'session/load', 'session/resume', 'session/fork',
})


def _manual_skeleton(method: str, session_id: str | None, work_dir: str) -> str:
    """Return a pretty-printed JSON skeleton for *method*."""
    sid = session_id or ''
    if method == 'session/prompt':
        params: dict = {'sessionId': sid, 'prompt': [{'type': 'text', 'text': ''}]}
    elif method == 'session/cancel':
        params = {'sessionId': sid}
    elif method == 'session/list':
        params = {'cwd': work_dir}
    elif method == 'session/new':
        params = {'cwd': work_dir, 'mcpServers': []}
    elif method in ('session/load', 'session/resume'):
        params = {'sessionId': sid, 'cwd': work_dir, 'mcpServers': []}
    elif method == 'session/set_config_option':
        params = {'sessionId': sid, 'configId': 'model', 'value': ''}
    elif method == 'session/set_mode':
        params = {'sessionId': sid, 'modeId': ''}
    elif method in ('session/close', 'session/delete'):
        params = {'sessionId': sid}
    elif method == 'session/fork':
        params = {'sessionId': sid, 'cwd': work_dir, 'mcpServers': []}
    else:
        params = {}
    return json.dumps(params, indent=2)


def _sync_manual_config_cache(method, params, result, agent_cmd, cache_dir, window_id) -> None:
    """Patch the cached config option after a manual set request succeeds.

    Manual ``session/set_mode`` responses carry no usable state (opencode
    answers with ``{}``), so without this the Switch Mode panel keeps showing
    the old value. Patch from the request params instead; same for manual
    ``session/set_config_option``.
    """
    if not agent_cmd:
        return
    try:
        if method == 'session/set_mode':
            mode_id = params.get('modeId')
            if isinstance(mode_id, str) and mode_id:
                cache.set_config_option_value(cache_dir, agent_cmd, 'mode', mode_id)
        elif method == 'session/set_config_option':
            refreshed = result.get('configOptions') if isinstance(result, dict) else None
            if refreshed:
                with cache.cache_lock:
                    agents = cache.load_agents(cache_dir)
                    entry = agents.get(agent_cmd[0], {})
                    entry['config_options'] = refreshed
                    agents[agent_cmd[0]] = entry
                    cache.save_agents(cache_dir, agents)
            else:
                config_id = params.get('configId')
                value = params.get('value')
                if isinstance(config_id, str) and config_id and isinstance(value, str):
                    cache.set_config_option_value(cache_dir, agent_cmd, config_id, value)
    except Exception as exc:
        acp_log('manual_request', f'failed to sync cached config: {exc!r}', window_id)


_MANUAL_PARAMS_SYNTAX = 'Packages/JavaScript/JSON.sublime-syntax'
_MANUAL_PARAMS_SETTING = 'acp_manual_params'

# view id -> {'method': str, 'command': AcpSendManualRequestCommand}
_manual_params_state: dict = {}

# view id -> sublime.PhantomSet (kept alive so phantoms render)
_manual_params_phantoms: dict = {}

_PHANTOM_HTML = (
    '<body id="acp-manual-params">'
    '<br/><a href="send">&#9654; Execute manual request</a>'
    '&nbsp;&nbsp;<small>edit the JSON, then click Execute</small>'
    '</body>'
)


def _manual_params_phantom(view: sublime.View) -> None:
    phs = _manual_params_phantoms.get(view.id())
    if phs is None:
        phs = sublime.PhantomSet(view, 'acp_manual_params')
        _manual_params_phantoms[view.id()] = phs

    def on_navigate(href: str) -> None:
        if href == 'send':
            view.run_command('acp_manual_params_submit')

    phs.update([
        sublime.Phantom(
            sublime.Region(view.size()), _PHANTOM_HTML,
            sublime.LAYOUT_BLOCK, on_navigate,
        )
    ])


def _close_manual_params_view(view: sublime.View) -> None:
    window = view.window()
    _manual_params_state.pop(view.id(), None)
    _manual_params_phantoms.pop(view.id(), None)
    if window is not None:
        window.focus_view(view)
        window.run_command('close_file')


def _show_manual_params_view(command, method: str, initial: str) -> None:
    """Open a scratch tab with the JSON params skeleton for *method*."""
    window = command.window
    view = window.new_file()
    view.set_name(f'Manual request: {method}')
    view.set_scratch(True)
    view.assign_syntax(_MANUAL_PARAMS_SYNTAX)
    view.settings().set('word_wrap', True)
    view.settings().set(_MANUAL_PARAMS_SETTING, True)
    view.run_command('acp_manual_params_set_text', {'characters': initial})
    _manual_params_state[view.id()] = {'method': method, 'command': command}
    _manual_params_phantom(view)
    sublime.set_timeout(lambda: _clear_manual_params_undo(view), 0)
    # Land the cursor inside the first empty string value (e.g. "" in "text": "")
    # so the user can type immediately.
    pos = initial.find('""')
    cursor = pos + 1 if pos != -1 else 0
    sel = view.sel()
    sel.clear()
    sel.add(sublime.Region(cursor, cursor))
    view.show(cursor)
    window.focus_view(view)


def _clear_manual_params_undo(view: sublime.View) -> None:
    with suppress(Exception):
        view.clear_undo_stack()


class AcpManualParamsSetTextCommand(sublime_plugin.TextCommand):
    """Replace the whole buffer with *characters*."""

    def run(self, edit, characters=''):
        self.view.erase(edit, sublime.Region(0, self.view.size()))
        self.view.insert(edit, 0, characters or '')


class AcpManualParamsSubmitCommand(sublime_plugin.TextCommand):
    """Submit the manual request params view (phantom link)."""

    def run(self, edit):
        state = _manual_params_state.pop(self.view.id(), None)
        if state is None:
            return
        text = self.view.substr(sublime.Region(0, self.view.size()))
        try:
            params = json.loads(text) if text.strip() else {}
        except ValueError as exc:
            _manual_params_state[self.view.id()] = state
            sublime.error_message(f'ACP: Invalid JSON params: {exc}')
            return
        if not isinstance(params, dict):
            _manual_params_state[self.view.id()] = state
            sublime.error_message('ACP: Params must be a JSON object')
            return
        _close_manual_params_view(self.view)
        state['command']._on_done(state['method'], text)


class AcpManualParamsCancelCommand(sublime_plugin.TextCommand):
    """Discard the manual request params view (escape)."""

    def run(self, edit):
        _close_manual_params_view(self.view)


class AcpManualParamsListener(sublime_plugin.EventListener):
    """Keep the execute phantom pinned to the end and clean up state."""

    def on_modified(self, view):
        if view.settings().get(_MANUAL_PARAMS_SETTING) and view.id() in _manual_params_phantoms:
            _manual_params_phantom(view)

    def on_close(self, view):
        _manual_params_state.pop(view.id(), None)
        _manual_params_phantoms.pop(view.id(), None)


class AcpSendManualRequestCommand(sublime_plugin.WindowCommand):
    """Send a raw ACP request for debugging (daemon-only)."""

    def is_visible(self):
        """Show only when debug mode is enabled."""
        return bool(settings().get('debug', False))

    def is_enabled(self):
        """Enable only in debug mode with a running daemon."""
        return self.is_visible() and _daemon_running(self.window.id())

    def run(self):
        """Show the method quick panel for a manual request."""
        window_id = self.window.id()
        state = get_state(window_id)
        if state is None or not state.is_running():
            sublime.status_message('ACP: No agent session running in this window')
            return
        methods = list(_MANUAL_ALWAYS_METHODS)
        for acp_method, cap in _MANUAL_GATED_METHODS:
            try:
                if state.supports(cap):
                    methods.append(acp_method)
            except ValueError:
                continue
        self._methods = sorted(methods)
        self._methods.append(_MANUAL_CUSTOM_METHOD)
        items = [
            [method, _MANUAL_METHOD_DESCRIPTIONS.get(method, '')]
            for method in self._methods
        ]
        self.window.show_quick_panel(
            items, self._on_pick, placeholder='Select ACP method',
        )

    def _on_pick(self, index):
        """Open the JSON input panel for the picked method."""
        if index == -1:
            return
        method = self._methods[index]
        state = get_state(self.window.id())
        if state is None or not state.is_running():
            sublime.status_message('ACP: No agent session running in this window')
            return
        if state.get('is_busy') and method not in _MANUAL_BUSY_ALLOWED:
            sublime.status_message('ACP: Wait for the current prompt to finish')
            return
        if method == _MANUAL_CUSTOM_METHOD:
            self.window.show_input_panel(
                'ACP method:', '', self._on_custom_method, None, None,
            )
            return
        s = state.get('session_id', 'work_dir')
        initial = _manual_skeleton(method, s['session_id'], s['work_dir'] or '.')
        _show_manual_params_view(self, method, initial)

    def _on_custom_method(self, text):
        """Open the params view with an empty object for a typed method name."""
        method = (text or '').strip()
        if not method:
            return
        state = get_state(self.window.id())
        if state is None or not state.is_running():
            sublime.status_message('ACP: No agent session running in this window')
            return
        if state.get('is_busy') and method not in _MANUAL_BUSY_ALLOWED:
            sublime.status_message('ACP: Wait for the current prompt to finish')
            return
        _show_manual_params_view(self, method, json.dumps({}))

    def _on_done(self, method, text):
        """Validate JSON params and dispatch the manual request."""
        try:
            params = json.loads(text) if text.strip() else {}
        except ValueError as exc:
            sublime.error_message(f'ACP: Invalid JSON params: {exc}')
            return
        if not isinstance(params, dict):
            sublime.error_message('ACP: Params must be a JSON object')
            return
        if method in _MANUAL_DESTRUCTIVE_METHODS:
            target = params.get('sessionId') or '<unknown>'
            if not sublime.ok_cancel_dialog(
                f'ACP: `{method}` will permanently delete agent session '
                f'`{target}`. This cannot be undone. Continue?',
                ok_title='Delete',
            ):
                return
        window_id = self.window.id()
        state = get_state(window_id)
        if state is None or not state.is_running():
            sublime.status_message('ACP: No agent session running in this window')
            return
        s = state.get('conn', 'loop')
        conn, loop = s['conn'], s['loop']
        if conn is None or loop is None or loop.is_closed():
            sublime.status_message('ACP: Daemon connection not available')
            return
        if state.get('is_busy') and method not in _MANUAL_BUSY_ALLOWED:
            sublime.status_message('ACP: Wait for the current prompt to finish')
            return
        timeout = settings().get('timeout', DEFAULT_TIMEOUT)
        is_notification = method == 'session/cancel'
        agent_cmd = state.get('agent_cmd')
        cache_dir = Path(sublime.cache_path()) / 'ACP'
        track_busy = method in _MANUAL_BUSY_METHODS
        if track_busy:
            state.set(is_busy=True)

        async def _send():
            if is_notification:
                await conn.send_notification(method, params)
                return None
            return await conn.send_request(method, params, timeout=timeout)

        try:
            future = asyncio.run_coroutine_threadsafe(_send(), loop)
        except RuntimeError:
            if track_busy:
                state.set(is_busy=False)
            sublime.status_message('ACP: daemon already stopped')
            return

        def _done(f):
            st = get_state(window_id)
            if track_busy and st is not None and st.get('conn') is conn:
                st.set(is_busy=False)
            try:
                result = f.result()
            except Exception as exc:
                self._render(method, params, f'**[Manual request failed]:** `{exc}`', window_id)
            else:
                body = 'null' if result is None else json.dumps(result, indent=2)
                self._render(method, params, body, window_id)
                if st is not None and st.is_running():
                    st.set(last_activity=time.monotonic())
                _sync_manual_config_cache(method, params, result, agent_cmd, cache_dir, window_id)
                if method in ('session/new', 'session/load', 'session/resume', 'session/fork'):
                    adopt_manual_session(window_id, method, params, result)
                if method in ('session/delete', 'session/close'):
                    deleted = params.get('sessionId')
                    st = get_state(window_id)
                    if deleted and st is not None and st.is_running() and deleted == st.get('session_id'):
                        acp_log('manual_request', f'{method} removed current session - starting new session', window_id)
                        sublime.set_timeout(lambda: new_daemon_session(window_id), 0)

        future.add_done_callback(_done)

    def _render(self, method, params, body, window_id):
        """Append a manual request and its result to the daemon output view."""
        state = get_state(window_id)
        view = state.get('output_view') if state is not None else None
        if view is None:
            return
        params_text = json.dumps(params, indent=2)
        ui.append_to_output_view(view, f'## Manual request `{method}`\n\n```json\n{params_text}\n```\n\n```json\n{body}\n```')
        ui.append_turn_divider(view)
        acp_log('manual_request', f'{method} sent', window_id)
        if method in _MANUAL_REFRESH_CACHE:
            refresh_session_cache(window_id)
