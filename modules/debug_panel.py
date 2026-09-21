from __future__ import annotations

import threading
from collections import deque
from typing import Callable

import sublime
import sublime_plugin

DEBUG_PANEL_NAME = 'ACP Log Panel'
DEBUG_SYNTAX = 'Packages/ACP/syntaxes/ACPLog.sublime-syntax'

_pending: deque[tuple[str, int | None]] = deque()
_lock = threading.Lock()
_scheduled = False
_configured_windows: set[int] = set()


def ensure_debug_panel(window: sublime.Window) -> sublime.View:
    """Return the read-only debug panel for *window* (no auto-show)."""
    panel = window.find_output_panel(DEBUG_PANEL_NAME)
    if panel is not None and window.id() in _configured_windows:
        return panel
    panel = window.create_output_panel(DEBUG_PANEL_NAME)
    panel.assign_syntax(DEBUG_SYNTAX)
    vs = panel.settings()
    vs.set('word_wrap', False)
    vs.set('line_numbers', False)
    vs.set('gutter', True)
    vs.set('scroll_past_end', False)
    # Re-create so Sublime picks it up as a result buffer (cf. LSP panels).
    panel = window.create_output_panel(DEBUG_PANEL_NAME)
    panel.set_read_only(True)
    _configured_windows.add(window.id())
    return panel


def _flush_to_windows() -> None:
    """Append pending lines to their owning window's panel (untagged fans out)."""
    global _scheduled
    with _lock:
        if not _pending:
            _scheduled = False
            return
        items = list(_pending)
        _pending.clear()
        _scheduled = False
    per_window: dict[int, list[str]] = {}
    broadcast: list[str] = []
    for line, wid in items:
        if wid is None:
            broadcast.append(line)
        else:
            per_window.setdefault(wid, []).append(line)
    windows = sublime.windows()
    if broadcast:
        text = ''.join(line + '\n' for line in broadcast)
        for window in windows:
            _append_to_panel(window, text)
    for wid, lines in per_window.items():
        window = next((w for w in windows if w.id() == wid), None)
        if window is not None:
            _append_to_panel(window, ''.join(line + '\n' for line in lines))


def _append_to_panel(window: sublime.Window, text: str) -> None:
    try:
        panel = ensure_debug_panel(window)
        panel.run_command('acp_update_log_panel', {'characters': text})
    except Exception:
        pass


def enqueue_debug_line(line: str, window_id: int | None = None) -> None:
    """Sink for :func:`protocol.log.set_log_sink`; batches writes on main thread."""
    global _scheduled
    with _lock:
        _pending.append((line, window_id))
        if _scheduled:
            return
        _scheduled = True
    sublime.set_timeout(_flush_to_windows, 0)


def init_debug_panel() -> Callable[[str, int | None], None]:
    """Register the panel sink; return it for :func:`set_log_sink`."""
    from ..protocol.log import set_log_sink

    set_log_sink(enqueue_debug_line)
    return enqueue_debug_line


def clear_window_log(window_id: int) -> None:
    """Erase the debug panel of *window_id* on the main thread."""

    def _clear() -> None:
        try:
            window = next((w for w in sublime.windows() if w.id() == window_id), None)
            if window is None:
                return
            panel = window.find_output_panel(DEBUG_PANEL_NAME)
            if panel is None:
                return
            if was_read_only := panel.is_read_only():
                panel.set_read_only(False)
            try:
                panel.run_command('select_all')
                panel.run_command('left_delete')
            finally:
                if was_read_only:
                    panel.set_read_only(True)
        except Exception:
            pass

    sublime.set_timeout(_clear, 0)


def clear_panel_cache(window_id: int | None = None) -> None:
    """Forget configured panels (window closed or plugin unload)."""
    if window_id is None:
        _configured_windows.clear()
    else:
        _configured_windows.discard(window_id)


class AcpUpdateLogPanelCommand(sublime_plugin.TextCommand):
    """Append *characters* to the debug log panel."""

    def run(self, edit: sublime.Edit, characters: str | None = '') -> None:
        view = self.view
        if was_read_only := view.is_read_only():
            view.set_read_only(False)
        try:
            view.insert(edit, view.size(), characters or '')
        finally:
            if was_read_only:
                view.set_read_only(True)
        # clear_undo_stack cannot run inside a TextCommand; defer it.
        sublime.set_timeout(_try_clear_undo_stack, 0)


def _try_clear_undo_stack() -> None:
    for window in sublime.windows():
        try:
            panel = window.find_output_panel(DEBUG_PANEL_NAME)
            if panel is not None:
                panel.clear_undo_stack()
        except Exception:
            pass
