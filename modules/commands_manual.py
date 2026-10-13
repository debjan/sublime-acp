"""Manual ACP requests - raw method prober for debugging (daemon-only)."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from pathlib import Path

import sublime
import sublime_plugin

from ..protocol import acp_log
from . import cache, ui
from .commands import _daemon_running
from .config import DEFAULT_TIMEOUT, settings
from .daemon import get_state
from .daemon_sessions import (
    adopt_manual_session,
    new_daemon_session,
    refresh_session_cache,
)

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
                    key = cache.agent_key(agent_cmd)
                    entry = cache.get_entry(agents, agent_cmd)
                    entry['config_options'] = refreshed
                    if key is not None:
                        agents[key] = entry
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
        initial = _manual_skeleton(method, s['session_id'], s['work_dir'] or str(Path.cwd()))
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
