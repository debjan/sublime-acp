"""Permission prompt handling for daemon sessions."""

from __future__ import annotations

import asyncio
import fnmatch
from typing import Any, Callable

import sublime

from ..protocol import TIMEOUT_EXCEPTIONS, acp_log
from . import ui
from .config import (
    DEFAULT_PERMISSIONS,
    DEFAULT_SENSITIVE_GLOBS,
    PERMISSION_PROMPT_TIMEOUT,
)

# Per-window serialization of interactive prompts: Sublime allows only one
# quick panel per window; concurrent show_quick_panel calls displace each
# other and the displaced panel's on_done never fires.
_prompt_locks: dict[int, asyncio.Lock] = {}
# window_id -> running loop the lock above was created on. Daemon restarts
# create a new event loop, and an asyncio.Lock bound to the old (closed)
# loop raises on 3.10+; tracking the loop lets us recreate stale locks.
_prompt_lock_loops: dict[int, asyncio.AbstractEventLoop] = {}
# window_id -> callable resolving the currently displayed prompt (supersede
# fail-safe so a displaced/abandoned waiter can never hang forever).
_active_prompts: dict[int, Callable[[str | None], None]] = {}


def _lock_for_window(window_id: int) -> asyncio.Lock:
    running = asyncio.get_running_loop()
    lock = _prompt_locks.get(window_id)
    if lock is None or _prompt_lock_loops.get(window_id) is not running:
        lock = asyncio.Lock()
        _prompt_locks[window_id] = lock
        _prompt_lock_loops[window_id] = running
    return lock


def invalidate_prompt_lock(window_id: int) -> None:
    """Drop the cached prompt lock for *window_id*.

    Called when a daemon stops so a later daemon (with a new event loop)
    never reuses a lock bound to the closed loop.
    """
    _prompt_locks.pop(window_id, None)
    _prompt_lock_loops.pop(window_id, None)


def sensitive_globs(permissions_config: dict | None = None) -> list:
    """Return the sensitive-file globs from *permissions_config* or defaults."""
    if permissions_config is None:
        permissions_config = DEFAULT_PERMISSIONS
    globs = permissions_config.get('sensitive_globs', DEFAULT_SENSITIVE_GLOBS)
    if isinstance(globs, (list, tuple)):
        return list(globs)
    return list(DEFAULT_SENSITIVE_GLOBS)


def is_sensitive_path(path: str | None, globs: list | None = None) -> bool:
    """Return whether *path* matches any sensitive-file glob.

    Patterns match the basename (``.env``) as well as the full path, so
    ``*key*`` also catches sensitive directories on the way in.
    """
    if not isinstance(path, str) or not path:
        return False
    if globs is None:
        globs = DEFAULT_SENSITIVE_GLOBS
    candidates = {path, path.replace('\\', '/')}
    candidates.add(path.replace('\\', '/').rsplit('/', 1)[-1])
    return any(
        fnmatch.fnmatch(candidate, glob)
        for candidate in candidates
        for glob in globs
    )


def _resolve_auto_permission(
    params: dict[str, Any],
    permissions_config: dict | None = None,
    window_id: int | None = None,
) -> tuple[str, str | None]:
    if permissions_config is None:
        permissions_config = DEFAULT_PERMISSIONS

    options = params.get('options', [])
    tool_call = params.get('toolCall', {})
    tool_kind = tool_call.get('kind', 'other')

    auto_reject = permissions_config.get('auto_reject', DEFAULT_PERMISSIONS['auto_reject'])
    auto_allow = permissions_config.get('auto_allow', DEFAULT_PERMISSIONS['auto_allow'])

    outcome: tuple[str, str | None] = ('prompt', None)
    for pattern in auto_reject:
        if fnmatch.fnmatch(tool_kind, pattern):
            outcome = ('cancelled', None)
            break
    else:
        if tool_call.get('sensitive'):
            acp_log('permissions', f'sensitive {tool_kind!r} operation - forcing prompt', window_id)
        else:
            for pattern in auto_allow:
                if fnmatch.fnmatch(tool_kind, pattern):
                    outcome = ('selected', options[0].get('optionId')) if options else ('selected', None)
                    break
    acp_log(
        'permissions',
        f'auto-{outcome[0]} for kind {tool_kind!r} (optionId={outcome[1]!r})',
        window_id,
    )
    return outcome


def _show_permission_prompt(params: dict, on_done: Callable, window_id: int,
                            shown: list | None = None) -> None:
    """Show a quick panel with permission options."""
    if window_id not in [w.id() for w in sublime.windows()]:
        on_done(None)
        return
    window = sublime.Window(window_id)

    tool_call = params.get('toolCall', {})
    title = tool_call.get('title', 'Unknown operation')
    kind = tool_call.get('kind', '')

    if not (options := params.get('options', [])):
        on_done(None)
        return

    from . import broadcast
    short_title = ui.truncate_middle(title)
    names = [
        o.get('name', o.get('optionId'))
        for o in options
    ]
    option_ids = [o.get('optionId') for o in options]

    acp_log('permissions', f'showing quick panel: title={title!r}, options={names}', window_id)

    def _on_done(index: int) -> None:
        if index == -1:
            acp_log('permissions', 'user cancelled quick panel (index=-1)', window_id)
            on_done(None)
        else:
            acp_log('permissions', f'user selected: index={index}, optionId={option_ids[index]!r}', window_id)
            on_done(option_ids[index])

    broadcast.flash_prompt(f'ACP: permission needed - {short_title}', window_id)

    items = [
        sublime.QuickPanelItem(name, details=short_title, annotation=str(kind))
        for name in names
    ]

    if shown is not None:
        shown.append(True)
    window.show_quick_panel(
        items, _on_done,
        placeholder=f'Agent wants to: {short_title}',
    )


def dismiss_permission_prompt(window_id: int) -> None:
    """Dismiss any open permission quick panel for a window."""
    def _dismiss():
        window = sublime.Window(window_id)
        if window is not None:
            window.run_command('hide_overlay')
            window.run_command('hide_panel', {'cancel': True})
        resolver = _active_prompts.pop(window_id, None)
        if resolver is not None:
            resolver(None)

    ui.on_main(_dismiss)


async def _prompt_user(
    params: dict,
    window_id: int,
    loop: asyncio.AbstractEventLoop | None = None,
    timeout: float = PERMISSION_PROMPT_TIMEOUT,
) -> str | None:
    """Show the permission prompt on the UI thread and await the user's
    choice without blocking a thread-pool worker.

    Prompts for the same window are serialized behind an ``asyncio.Lock``
    and the wait is capped at *timeout* seconds so an unanswered or
    superseded panel can never hang the agent forever. On timeout (or if
    this prompt is superseded by another) ``None`` is returned, which the
    caller treats as a cancel/deny.
    """
    lock = _lock_for_window(window_id)
    async with lock:
        event = asyncio.Event()
        result: list = []

        def _on_done(option_id: str | None) -> None:
            def _set():
                result.append(option_id)
                event.set()

            if loop is not None:
                if loop.is_closed():
                    return
                try:
                    loop.call_soon_threadsafe(_set)
                    return
                except RuntimeError:
                    pass
                return
            _set()

        # Supersede any stale waiter still registered for this window.
        prior = _active_prompts.pop(window_id, None)
        if prior is not None:
            acp_log('permissions', 'previous permission prompt superseded - denying', window_id)
            prior(None)
        _active_prompts[window_id] = _on_done

        panel_shown: list = []
        running_loop = asyncio.get_running_loop()
        deadline = running_loop.time() + timeout if timeout and timeout > 0 else None
        tool_call = params.get('toolCall', {}) if isinstance(params, dict) else {}
        notify_text = f'ACP: permission needed - {ui.truncate_middle(tool_call.get("title", "Unknown operation"))}'
        try:
            if not await ui.wait_for_window_active(
                window_id, deadline, cancelled=event.is_set, notify_text=notify_text,
            ):
                if not event.is_set():
                    acp_log(
                        'permissions',
                        'permission prompt timed out waiting for focus - denying',
                        window_id,
                    )
                return None
            if event.is_set():
                return None

            while True:
                if deadline is not None:
                    timeout = deadline - running_loop.time()
                    if timeout <= 0:
                        acp_log(
                            'permissions',
                            'permission prompt timed out waiting for focus - denying',
                            window_id,
                        )
                        return None
                def _show() -> None:
                    _show_permission_prompt(params, _on_done, window_id, shown=panel_shown)

                ui.on_main(_show)
                shown_at = running_loop.time()
                try:
                    if timeout and timeout > 0:
                        await asyncio.wait_for(event.wait(), timeout)
                    else:
                        await event.wait()
                except TIMEOUT_EXCEPTIONS:
                    acp_log(
                        'permissions',
                        f'permission prompt timed out after {timeout}s - denying',
                        window_id,
                    )
                    return None
                if (
                    panel_shown and result and result[0] is None
                    and await ui.panel_cancel_is_background(window_id, running_loop.time() - shown_at)
                ):
                    acp_log('permissions', 'panel auto-dismissed while unfocused - waiting for focus', window_id)
                    event.clear()
                    result.clear()
                    del panel_shown[:]
                    if not await ui.wait_for_window_active(
                        window_id, deadline, cancelled=event.is_set, notify_text=notify_text,
                    ):
                        if not event.is_set():
                            acp_log(
                                'permissions',
                                'permission prompt timed out waiting for focus - denying',
                                window_id,
                            )
                        return None
                    if event.is_set():
                        return None
                    continue
                break
        finally:
            if _active_prompts.get(window_id) is _on_done:
                del _active_prompts[window_id]

        if result:
            return result[0]
        return None


async def resolve_permission(
    params: dict,
    permissions_config: dict | None = None,
    window_id: int | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
    timeout: float = PERMISSION_PROMPT_TIMEOUT,
) -> str | None:
    outcome, option_id = _resolve_auto_permission(params, permissions_config, window_id)
    if outcome == 'selected':
        return option_id
    if outcome == 'cancelled':
        return None
    if window_id is not None:
        return await _prompt_user(params, window_id, loop, timeout=timeout)
    return None
