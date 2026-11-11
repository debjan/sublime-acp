"""Status-bar broadcasting - multi-view status, spinner, daemon status text."""

from __future__ import annotations

import sys
from pathlib import Path

import sublime

from ..protocol import acp_log
from . import cache, notify
from .config import SPINNER_INTERVAL_MS, STATUS_KEY_DAEMON, STATUS_KEY_NOTIFY, settings

#: Status-bar prefix that toasts drop, since a toast already names the app.
_STATUS_PREFIX = 'ACP: '


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
    """Signal an interactive prompt: status flash plus gated desktop toast.

    The status flash always fires; the toast (never a focus steal) honors the
    ``prompt_flash`` setting (default on) and uses the prompt sound. Prompts
    block the agent turn until answered, so background windows need the nudge.
    Main-thread safe from any thread.
    """
    flash_notify(text, window_id)
    if settings().get('prompt_flash', True):
        demand_attention(text, window_id, kind='prompt')


def demand_attention(text, window_id=None, kind: str = 'turn_end') -> None:
    """Signal a finished turn or a blocking prompt for the owning window."""

    def _signal():
        window = None
        if isinstance(window_id, int):
            window = next((w for w in sublime.windows() if w.id() == window_id), None)
        if window is None:
            return
        if not _owner_unfocused(window):
            return
        body = text[len(_STATUS_PREFIX):] if text.startswith(_STATUS_PREFIX) else text
        label = _window_label(window) if len(sublime.windows()) > 1 else ''
        message = f'{body} · {label}' if label else body
        notify.show(message, kind)
        if sys.platform == 'win32' and not _flash_taskbar_window(window):
            acp_log('notify', f'taskbar flash failed for window {window.id()}', window.id())

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


def _window_label(window) -> str:
    """Return a short label for *window*: its first folder or project name."""
    try:
        folders = window.folders()
    except Exception:
        folders = []
    if folders:
        if name := Path(folders[0]).name:
            return name
    try:
        project = window.project_file_name()
    except Exception:
        project = None
    return Path(project).stem if project else ''


def _owner_not_active(window) -> bool:
    """Return whether *window* is not the window the user is looking at."""
    try:
        active = sublime.active_window()
    except Exception:
        return False
    if active is None:
        return False
    try:
        return active.id() != window.id()
    except Exception:
        return False


def _window_has_os_focus(window) -> bool | None:
    """Return whether *window* owns OS focus; ``None`` when that is unknown."""

    if sys.platform != 'win32':
        return None
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        foreground = user32.GetForegroundWindow()
        hwnd = window.hwnd()
    except Exception:
        return None
    if not foreground or not hwnd:
        return None
    return foreground == hwnd


def _owner_unfocused(window) -> bool:
    """Return whether the user looks at something other than *window*."""

    if _owner_not_active(window):
        return True
    return _window_has_os_focus(window) is False


def _pretty_model_name(model: str | None) -> str:
    """Render a cached model value for the status bar."""

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
