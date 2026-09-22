"""Permission prompt handling for daemon sessions."""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
from typing import Any, Callable

import sublime

from ..protocol import acp_log
from . import ui
from .config import DEFAULT_PERMISSIONS, PERMISSION_PROMPT_TIMEOUT

# Per-window serialization of interactive prompts: Sublime allows only one
# quick panel per window; concurrent show_quick_panel calls displace each
# other and the displaced panel's on_done never fires.
_prompt_locks: dict[int, asyncio.Lock] = {}
# window_id -> callable resolving the currently displayed prompt (supersede
# fail-safe so a displaced/abandoned waiter can never hang forever).
_active_prompts: dict[int, Callable[[str | None], None]] = {}


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


_TRUNCATE_MAX_CHARS = 160


def _truncate_middle(text: str, max_chars: int = _TRUNCATE_MAX_CHARS) -> str:
    """Shorten long text to fit the quick panel, keeping head and tail."""
    text = ' '.join(str(text).split())
    if len(text) <= max_chars:
        return text
    keep = max_chars - 1
    head = keep * 2 // 3
    return text[:head] + '…' + text[len(text) - (keep - head):]


def _show_permission_prompt(params: dict, on_done: Callable, window_id: int) -> None:
    """Show a quick panel with permission options."""
    if window_id not in [w.id() for w in sublime.windows()]:
        on_done(None)
        return
    window = sublime.Window(window_id)

    tool_call = params.get('toolCall', {})
    title = tool_call.get('title', 'Unknown operation')
    kind = tool_call.get('kind', '')
    options = params.get('options', [])

    if not options:
        on_done(None)
        return

    short_title = _truncate_middle(title)
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

    with contextlib.suppress(Exception):
        window.bring_to_front()

    quick_panel_item = getattr(sublime, 'QuickPanelItem', None)
    if quick_panel_item is not None:
        items = [
            quick_panel_item(name, details=short_title, annotation=str(kind))
            for name in names
        ]
    else:  # pragma: no cover - older Sublime without QuickPanelItem
        items = [f'{name} - {short_title}' for name in names]

    window.show_quick_panel(
        items, _on_done,
        placeholder=f'Agent wants to: {short_title}',
    )


def dismiss_permission_prompt(window_id: int) -> None:
    """Dismiss any open permission quick panel for a window."""
    def _dismiss():
        window = sublime.Window(window_id)
        if window is not None:
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
    lock = _prompt_locks.setdefault(window_id, asyncio.Lock())
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

        try:
            ui.on_main(
                lambda: _show_permission_prompt(params, _on_done, window_id),
            )
            try:
                if timeout and timeout > 0:
                    await asyncio.wait_for(event.wait(), timeout)
                else:
                    await event.wait()
            except asyncio.TimeoutError:
                acp_log(
                    'permissions',
                    f'permission prompt timed out after {timeout}s - denying',
                    window_id,
                )
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
