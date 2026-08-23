"""Permission prompt handling for daemon sessions."""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
from typing import Any, Callable

import sublime

from ..protocol import acp_log
from . import ui
from .config import DEFAULT_PERMISSIONS


def _resolve_auto_permission(
    params: dict[str, Any],
    permissions_config: dict | None = None,
) -> tuple[str, str | None]:
    if permissions_config is None:
        permissions_config = DEFAULT_PERMISSIONS

    options = params.get('options', [])
    tool_call = params.get('toolCall', {})
    tool_kind = tool_call.get('kind', 'other')

    auto_reject = permissions_config.get('auto_reject', DEFAULT_PERMISSIONS['auto_reject'])
    auto_allow = permissions_config.get('auto_allow', DEFAULT_PERMISSIONS['auto_allow'])

    for pattern in auto_reject:
        if fnmatch.fnmatch(tool_kind, pattern):
            return ('cancelled', None)

    for pattern in auto_allow:
        if fnmatch.fnmatch(tool_kind, pattern):
            if options:
                return ('selected', options[0].get('optionId'))
            return ('selected', None)

    return ('prompt', None)


def _show_permission_prompt(params: dict, on_done: Callable, window_id: int) -> None:
    """Show a quick panel with permission options."""
    window = sublime.Window(window_id)
    if not window:
        on_done(None)
        return

    tool_call = params.get('toolCall', {})
    title = tool_call.get('title', 'Unknown operation')
    options = params.get('options', [])

    if not options:
        on_done(None)
        return

    labels = [
        o.get('name', o.get('optionId'))
        for o in options
    ]
    option_ids = [o.get('optionId') for o in options]

    acp_log('permissions', f'showing quick panel: title={title!r}, options={labels}')

    def _on_done(index: int) -> None:
        if index == -1:
            acp_log('permissions', 'user cancelled quick panel (index=-1)')
            on_done(None)
        else:
            acp_log('permissions', f'user selected: index={index}, optionId={option_ids[index]!r}')
            on_done(option_ids[index])

    with contextlib.suppress(Exception):
        window.bring_to_front()

    window.show_quick_panel(
        labels, _on_done,
        placeholder=f'Agent wants to: {title}',
    )


def _daemon_permission_prompt(
    event: asyncio.Event,
    params: dict,
    result: list,
    window_id: int,
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """Show the permission quick panel and signal the asyncio ``event``
    when the user responds (or cancels).

    Args:
        event: Event signaled when the user responds.
        params: A ``session/request_permission``-shaped params dict.
        result: List receiving the chosen ``optionId`` (or ``None``).
        window_id: Window ID whose quick panel shows the prompt.
        loop: Event loop awaiting the response; the user's selection is
            marshaled onto it via ``call_soon_threadsafe`` so a loop
            blocked on I/O wakes up immediately. Responses arriving
            after the loop has closed are dropped: the awaiting
            coroutine is gone and cannot consume them.
    """
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

    ui.on_main(
        lambda: _show_permission_prompt(params, _on_done, window_id),
    )


def dismiss_permission_prompt(window_id: int) -> None:
    """Dismiss any open permission quick panel for a window."""
    def _dismiss():
        window = sublime.Window(window_id)
        if window is not None:
            window.run_command('hide_panel', {'cancel': True})

    ui.on_main(_dismiss)


async def _prompt_user(
    params: dict,
    window_id: int,
    loop: asyncio.AbstractEventLoop | None = None,
) -> str | None:
    """Show the permission prompt on the UI thread and await the user's
    choice without blocking a thread-pool worker."""
    event = asyncio.Event()
    result: list = []

    _daemon_permission_prompt(event, params, result, window_id, loop)
    await event.wait()

    if result:
        return result[0]
    return None


async def resolve_permission(
    params: dict,
    permissions_config: dict | None = None,
    window_id: int | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
) -> str | None:
    outcome, option_id = _resolve_auto_permission(params, permissions_config)
    if outcome == 'selected':
        return option_id
    if outcome == 'cancelled':
        return None
    if window_id is not None:
        return await _prompt_user(params, window_id, loop)
    return None
