"""asyncio version-compatibility helpers for Python 3.8 through 3.14.

Centralizes the primitives whose semantics changed across the supported
range so callers do not branch on ``sys.version_info`` inline.
"""

from __future__ import annotations

import asyncio
import contextlib

TIMEOUT_EXCEPTIONS = (asyncio.TimeoutError, TimeoutError)
"""Timeout exception tuple safe on both ends of the supported range.

On 3.8 ``asyncio.TimeoutError`` and builtin ``TimeoutError`` are distinct
classes, so both must be caught. On 3.11+ they are the same object and the
tuple simply contains a duplicate, which is harmless.
"""


def loop_time() -> float:
    """Return the current running loop's monotonic clock.

    Returns:
        Seconds from the running event loop's internal clock.
    """
    return asyncio.get_running_loop().time()


def new_daemon_loop() -> asyncio.AbstractEventLoop:
    """Create an event loop for a background daemon/worker thread.

    Calls ``asyncio.set_event_loop`` so code using the thread-local loop
    (e.g. ``asyncio.Queue()`` created before the loop runs) binds to it on
    3.8, while remaining harmless on 3.10+ where primitives bind lazily.

    Returns:
        The newly created event loop, installed as the thread-local loop.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop


def clear_thread_loop() -> None:
    """Clear the current thread's event loop reference (best effort)."""
    with contextlib.suppress(Exception):
        asyncio.set_event_loop(None)
