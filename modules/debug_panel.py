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


def _configure_panel(panel: sublime.View) -> None:
    panel.assign_syntax(DEBUG_SYNTAX)
    vs = panel.settings()
    vs.set('word_wrap', False)
    vs.set('line_numbers', False)
    vs.set('gutter', True)
    vs.set('scroll_past_end', False)


def ensure_debug_panel(window: sublime.Window) -> sublime.View:
    """Return the read-only debug panel for *window* (no auto-show).

    An already existing panel is reconfigured in place rather than recreated,
    so preserved log content survives (e.g. after a plugin reload).
    """
    panel = window.find_output_panel(DEBUG_PANEL_NAME)
    if panel is None:
        panel = window.create_output_panel(DEBUG_PANEL_NAME)
        _configure_panel(panel)
        # Re-create so Sublime picks it up as a result buffer (cf. LSP panels).
        panel = window.create_output_panel(DEBUG_PANEL_NAME)
    elif window.id() not in _configured_windows:
        _configure_panel(panel)
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


def destroy_window_log(window_id: int) -> None:
    """Remove the debug panel of *window_id* so it leaves the Output menu.

    Runs on the main thread. Unlike clearing, this unregisters the panel via
    ``destroy_output_panel`` so ``View > Output`` no longer offers it. A
    currently visible panel is hidden first so destruction always runs.
    """

    def _destroy() -> None:
        try:
            window = next((w for w in sublime.windows() if w.id() == window_id), None)
            if window is None:
                return
            if window.active_panel() == f'output.{DEBUG_PANEL_NAME}':
                window.run_command('hide_panel', {'cancel': True})
            window.destroy_output_panel(DEBUG_PANEL_NAME)
            _configured_windows.discard(window_id)
        except Exception:
            pass

    sublime.set_timeout(_destroy, 0)


def clear_window_log(window_id: int) -> None:
    """Erase the debug panel content of *window_id* without destroying it.

    Drops already-queued entries for *window_id* under ``_lock`` before
    erasing, so stale lines emitted just before a reset cannot flush into
    the new session. Broadcast entries (``None``) are kept since they fan
    out to all windows. Main thread only; no-ops if the panel does
    not exist. Failures are logged instead of silently swallowed.
    """
    from ..protocol.log import acp_log

    with _lock:
        kept = [(line, wid) for line, wid in _pending if wid != window_id]
        _pending.clear()
        _pending.extend(kept)

    try:
        window = next((w for w in sublime.windows() if w.id() == window_id), None)
        if window is None:
            acp_log('debug_panel', f'clear_window_log: window {window_id} not found')
            return
        panel = window.find_output_panel(DEBUG_PANEL_NAME)
        if panel is None:
            return
        panel.run_command('acp_clear_log_panel')
        if panel.size() > 0:
            acp_log(
                'debug_panel',
                f'clear_window_log: panel still {panel.size()} chars - clear command did not run',
            )
    except Exception as exc:
        acp_log('debug_panel', f'clear_window_log failed: {exc}')


def clear_panel_cache(window_id: int | None = None) -> None:
    """Forget configured panels (window closed or plugin unload)."""
    if window_id is None:
        _configured_windows.clear()
    else:
        _configured_windows.discard(window_id)


def _write_panel(view: sublime.View, edit: sublime.Edit, fn: Callable[[sublime.Edit], None]) -> None:
    """Run *fn* with the panel writable, restoring read-only afterwards."""
    if was_read_only := view.is_read_only():
        view.set_read_only(False)
    try:
        fn(edit)
    finally:
        if was_read_only:
            view.set_read_only(True)
    # clear_undo_stack cannot run inside a TextCommand; defer it.
    sublime.set_timeout(_try_clear_undo_stack, 0)


class AcpUpdateLogPanelCommand(sublime_plugin.TextCommand):
    """Append *characters* to the debug log panel."""

    def run(self, edit: sublime.Edit, characters: str | None = '') -> None:
        view = self.view
        _write_panel(view, edit, lambda e: view.insert(e, view.size(), characters or ''))


class AcpClearLogPanelCommand(sublime_plugin.TextCommand):
    """Erase all content of the debug log panel."""

    def run(self, edit: sublime.Edit) -> None:
        view = self.view
        _write_panel(view, edit, lambda e: view.erase(e, sublime.Region(0, view.size())))


def _try_clear_undo_stack() -> None:
    for window in sublime.windows():
        try:
            panel = window.find_output_panel(DEBUG_PANEL_NAME)
            if panel is not None:
                panel.clear_undo_stack()
        except Exception:
            pass
