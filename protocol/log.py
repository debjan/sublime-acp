"""Shared debug logging helper for ACP packages."""

from __future__ import annotations

import contextlib
import os
import sys
from collections import deque
from contextvars import ContextVar
from datetime import datetime
from typing import Callable

_TRUTHY = ('1', 'true', 'yes')

# Window that "owns" the current thread/task; attached to each log line so
# the debug panel can route lines per window. Untagged (None) lines fan out
# to every window. Each daemon/worker thread sets this once at startup.
_window_ctx: ContextVar[int | None] = ContextVar('acp_log_window', default=None)


def set_log_window(window_id: int | None) -> None:
    """Tag subsequent :func:`acp_log` lines on this thread/task with *window_id*."""
    _window_ctx.set(window_id)


# Lines logged before a sink is registered (early init); replayed on registration.
_BACKLOG_LIMIT = 500
_backlog: deque[tuple[str, int | None]] = deque(maxlen=_BACKLOG_LIMIT)

_sink: Callable[[str, int | None], None] | None = None


def set_log_sink(sink: Callable[[str, int | None], None] | None) -> None:
    """Route formatted debug lines to *sink* (panel-only); ``None`` restores stderr."""
    global _sink
    _sink = sink
    if sink is not None:
        while _backlog:
            line, wid = _backlog.popleft()
            with contextlib.suppress(Exception):
                sink(line, wid)


def acp_log(tag: str, msg: str, window_id: int | None = None) -> None:
    """Emit a tagged debug line to the log sink (panel) or stderr fallback.

    *window_id* overrides the thread's tag when the caller knows the owning
    window (e.g. state cleanup running on the main thread); otherwise the
    thread-local tag applies, and untagged lines fan out to every window.
    """
    if os.environ.get('ACP_DEBUG', '').lower() not in _TRUTHY:
        return
    line = f'[{datetime.now().strftime("%H:%M:%S")}] [ACP:{tag}] {msg}'
    wid = window_id if window_id is not None else _window_ctx.get()
    if _sink is not None:
        with contextlib.suppress(Exception):
            _sink(line, wid)
        return
    _backlog.append((line, wid))
    print(line, file=sys.stderr)
