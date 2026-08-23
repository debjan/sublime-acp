"""Status-bar broadcasting - multi-view status, spinner, daemon status text."""

from __future__ import annotations

from pathlib import Path

import sublime

from . import cache
from .config import SPINNER_INTERVAL_MS, STATUS_KEY_DAEMON


def _broadcast_status(key, value, window=None):
    """Set or erase a status bar key on every view in the given window."""
    if window is None:
        window = sublime.active_window()
    if window:
        for v in window.views():
            if value is None:
                v.erase_status(key)
            else:
                v.set_status(key, value)


def set_broadcast_status(key, text, window=None):
    """Set a status bar key on every view in the given window."""
    _broadcast_status(key, text, window)


def erase_broadcast_status(key, window=None):
    """Erase a status bar key from every view in the given window."""
    _broadcast_status(key, None, window)


def daemon_status_text(agent_name: str, agent_cmd: list | None = None) -> str:
    """Return the daemon status text, appending the current model name."""
    model = cache.get_model_name(Path(sublime.cache_path()) / 'ACP', agent_cmd)
    return f'✓ ACP: {agent_name} [{model or "default"}]'


SPINNER_FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']


def show_spinner(view, is_done, message, on_done=None):
    """Display an animated spinner in the status bar while a condition holds."""
    def tick(index=0):
        win = view.window()
        if win is None:
            if on_done:
                on_done()
            return

        if not is_done():
            frame = SPINNER_FRAMES[index % len(SPINNER_FRAMES)]
            set_broadcast_status(STATUS_KEY_DAEMON, f'{frame} {message}...', win)
            sublime.set_timeout(lambda: tick(index + 1), SPINNER_INTERVAL_MS)
        else:
            erase_broadcast_status(STATUS_KEY_DAEMON, win)
            if on_done:
                on_done()

    tick(0)