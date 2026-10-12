"""Status-bar broadcasting - multi-view status, spinner, daemon status text."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import sublime

from . import cache
from .config import SPINNER_INTERVAL_MS, STATUS_KEY_DAEMON, STATUS_KEY_NOTIFY, settings


def _broadcast_status(key, value, window=None):
    """Set or erase a status bar key on every view in the given window.

    Writes are skipped when the view already shows *value*, so view
    activation and spinner ticks do not churn the status bar.
    """
    if window is None:
        window = sublime.active_window()
    if window:
        for v in window.views():
            if value is None:
                if v.get_status(key):
                    v.erase_status(key)
            elif v.get_status(key) != value:
                v.set_status(key, value)


def set_broadcast_status(key, text, window=None):
    """Set a status bar key on every view in the given window."""
    _broadcast_status(key, text, window)


def erase_broadcast_status(key, window=None):
    """Erase a status bar key from every view in the given window."""
    _broadcast_status(key, None, window)


def flash_notify(text, window_id=None, timeout_ms=5000):
    """Flash transient *text* in the status bar, auto-clearing after *timeout_ms*.

    Main-thread safe from any thread (via ``sublime.set_timeout``); the
    owning window is resolved late so background callers pass an id, never
    a view. Used for permission/elicitation prompts instead of stealing
    focus with ``window.bring_to_front()``. Unknown owners stay silent
    instead of flashing the active window.
    """
    def _show():
        window = None
        if isinstance(window_id, int):
            window = next((w for w in sublime.windows() if w.id() == window_id), None)
            if window is None:
                return
        set_broadcast_status(STATUS_KEY_NOTIFY, text, window)
        sublime.set_timeout(lambda: erase_broadcast_status(STATUS_KEY_NOTIFY, window), timeout_ms)

    sublime.set_timeout(_show, 0)


def flash_prompt(text, window_id=None) -> None:
    """Signal an interactive prompt: status flash plus gated OS attention.

    The status flash always fires; the OS leg (taskbar/banner, never
    focus steal) honors the ``prompt_flash`` setting (default on).
    Prompts block the agent turn until answered, so background windows
    need the nudge. Main-thread safe from any thread.
    """
    flash_notify(text, window_id)
    if settings().get('prompt_flash', True):
        demand_attention(text, window_id)


def demand_attention(text, window_id=None):
    """Flash the OS taskbar/dock entry for the owning window (never steals focus).

    Main-thread safe from any thread. No-op when the window is gone.
    Windows flashes the taskbar button until foregrounded (``FlashWindowEx``);
    macOS posts a Notification Center banner; Linux tries ``notify-send``.
    The macOS/Linux subprocess calls run off the UI thread so a slow
    notification service cannot freeze the editor.
    """
    def _signal():
        window = None
        if isinstance(window_id, int):
            window = next((w for w in sublime.windows() if w.id() == window_id), None)
        if window is None:
            return
        if sys.platform == 'win32':
            _flash_taskbar_window(window)
        elif sys.platform == 'darwin':
            threading.Thread(target=_notify_center, args=('ACP', text), daemon=True).start()
        else:
            threading.Thread(target=_notify_send, args=('ACP', text), daemon=True).start()

    sublime.set_timeout(_signal, 0)


def _flash_taskbar_window(window) -> bool:
    """Request a taskbar flash via ``FlashWindowEx``; True when requested."""
    try:
        import ctypes
        hwnd = window.hwnd()
    except Exception:
        return False
    if not hwnd:
        return False

    class _FlashInfo(ctypes.Structure):
        _fields_ = [
            ('cbSize', ctypes.c_uint),
            ('hwnd', ctypes.c_void_p),
            ('dwFlags', ctypes.c_uint),
            ('uCount', ctypes.c_uint),
            ('dwTimeout', ctypes.c_uint),
        ]

    info = _FlashInfo(ctypes.sizeof(_FlashInfo), hwnd, 0x3 | 0xC, 0, 0)
    try:
        return bool(ctypes.windll.user32.FlashWindowEx(ctypes.byref(info)))
    except Exception:
        return False


def _notify_center(title, text) -> bool:
    """Post a Notification Center banner via osascript; True when posted."""
    import subprocess

    def _quote(value):
        return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'

    script = f'display notification {_quote(text)} with title {_quote(title)}'
    try:
        subprocess.run(
            ['osascript', '-e', script],
            capture_output=True, check=False, timeout=5,
        )
    except Exception:
        return False
    return True


def _notify_send(title, text) -> bool:
    """Post a desktop notification via ``notify-send`` when available."""
    import shutil
    import subprocess

    if shutil.which('notify-send') is None:
        return False
    try:
        subprocess.run(
            ['notify-send', str(title), str(text)],
            capture_output=True, check=False, timeout=5,
        )
    except Exception:
        return False
    return True


def _pretty_model_name(model: str | None) -> str:
    """Render a cached model value for the status bar.

    Some agents (e.g. ``dsh acp``) encode the model as a JSON pair
    like ``["provider", "model"]``; show that as ``provider/model``.
    """
    if not model:
        return 'default'
    text = str(model).strip()
    if text.startswith('['):
        try:
            import json

            parsed = json.loads(text)
            if isinstance(parsed, list) and len(parsed) == 2:
                return f'{parsed[0]}/{parsed[1]}'
        except ValueError:
            pass
    return text


def daemon_status_text(agent_name: str, agent_cmd: list | None = None) -> str:
    """Return the daemon status text, appending the current model name."""
    model = cache.get_model_name(Path(sublime.cache_path()) / 'ACP', agent_cmd)
    return f'✓ ACP: {agent_name} [{_pretty_model_name(model)}]'


def _compact_tokens(value: int) -> str:
    """Format a token count compactly, e.g. 53000 -> ``53k``, 1500000 -> ``1.5M``."""
    if value < 1000:
        return str(value)
    if value >= 1_000_000:
        val = round(value / 1_000_000, 1)
        return f'{int(val)}M' if val == int(val) else f'{val}M'
    rounded = round(value / 1000)
    return f'{rounded}k'


def usage_status_text(used: int, size: int) -> str:
    """Return the context usage status text, e.g. ``ctx 27% (53k/200k)``."""
    pct = min(100, int(used / size * 100 + 0.5)) if size > 0 else 0
    return f'ctx {pct}% ({_compact_tokens(used)}/{_compact_tokens(size)})'


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
