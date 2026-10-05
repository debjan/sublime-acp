"""Elicitation prompt handling for agent questions."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Callable

import sublime

from ..protocol import TIMEOUT_EXCEPTIONS, acp_log
from . import ui
from .config import PERMISSION_PROMPT_TIMEOUT

_CANCELLED: Any = object()

_elicit_locks: dict[int, asyncio.Lock] = {}
_elicit_lock_loops: dict[int, asyncio.AbstractEventLoop] = {}
_active_elicitations: dict[int, Callable[[Any], None]] = {}


def _lock_for_window(window_id: int) -> asyncio.Lock:
    running = asyncio.get_running_loop()
    lock = _elicit_locks.get(window_id)
    if lock is None or _elicit_lock_loops.get(window_id) is not running:
        lock = asyncio.Lock()
        _elicit_locks[window_id] = lock
        _elicit_lock_loops[window_id] = running
    return lock


def invalidate_elicit_lock(window_id: int) -> None:
    """Drop the cached elicitation lock for *window_id*."""
    _elicit_locks.pop(window_id, None)
    _elicit_lock_loops.pop(window_id, None)


def _truncate_middle(text: str, max_chars: int = 160) -> str:
    """Shorten long text to fit the quick panel, keeping head and tail."""
    text = ' '.join(str(text).split())
    if len(text) <= max_chars:
        return text
    keep = max_chars - 1
    head = keep * 2 // 3
    return text[:head] + '…' + text[len(text) - (keep - head):]


def dismiss_elicitation_prompt(window_id: int) -> None:
    """Dismiss any open elicitation panel for a window."""
    def _dismiss():
        window = sublime.Window(window_id)
        if window is not None:
            window.run_command('hide_panel', {'cancel': True})
        resolver = _active_elicitations.pop(window_id, None)
        if resolver is not None:
            resolver(_CANCELLED)

    ui.on_main(_dismiss)


def _enum_options(spec: dict) -> list[tuple[Any, str, str]] | None:
    """Return ``(value, label, description)`` options, or ``None`` for free text."""
    if isinstance(spec.get('enum'), list):
        return [(v, str(v), '') for v in spec['enum']]
    for key in ('oneOf', 'anyOf'):
        variants = spec.get(key)
        if isinstance(variants, list) and all(
            isinstance(v, dict) and 'const' in v for v in variants
        ):
            return [
                (v['const'], str(v.get('title') or v.get('name') or v['const']),
                 str(v.get('description') or ''))
                for v in variants
            ]
    if spec.get('type') == 'boolean':
        return [(True, 'True', ''), (False, 'False', '')]
    return None


def _show_choice(window_id: int, caption: str, labels: list, selected: int,
                 on_done: Callable) -> None:
    """Show a quick panel with *labels* on the main thread."""
    if window_id not in [w.id() for w in sublime.windows()]:
        on_done(-1)
        return
    window = sublime.Window(window_id)
    with contextlib.suppress(Exception):
        window.bring_to_front()
    window.show_quick_panel(labels, on_done, selected_index=selected, placeholder=caption)


def _show_input(window_id: int, caption: str, initial: str,
                on_done: Callable, on_cancel: Callable) -> None:
    """Show an input panel on the main thread."""
    if window_id not in [w.id() for w in sublime.windows()]:
        on_cancel()
        return
    window = sublime.Window(window_id)
    with contextlib.suppress(Exception):
        window.bring_to_front()
    window.show_input_panel(caption, initial, on_done, None, on_cancel)


async def _ask_choice(window_id: int, caption: str, options: list[tuple[Any, str, str]],
                      default: Any, loop: Any, remaining: float | None) -> Any:
    """Prompt a single enum/boolean field via quick panel."""
    lock = _lock_for_window(window_id)
    async with lock:
        event = asyncio.Event()
        result: list = []
        labels: list = []

        def _on_done(index: int) -> None:
            def _set():
                result.append(index)
                event.set()

            if loop is not None and not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(_set)
                    return
                except RuntimeError:
                    pass
                return
            _set()

        def _resolve(value: Any) -> None:
            _active_elicitations.pop(window_id, None)
            _on_done(value)

        prior = _active_elicitations.pop(window_id, None)
        if prior is not None:
            acp_log('elicitation', 'previous elicitation superseded - cancelling', window_id)
            prior(_CANCELLED)
        _active_elicitations[window_id] = _resolve

        selected = 0
        quick_items: list = []
        quick_panel_item = getattr(sublime, 'QuickPanelItem', None)
        for i, (_, label, desc) in enumerate(options):
            if default is not None and options[i][0] == default:
                selected = i
            short = _truncate_middle(desc) if desc else _truncate_middle(caption)
            if quick_panel_item is not None:
                quick_items.append(quick_panel_item(label, details=short))
            else:
                quick_items.append(label)
        labels = quick_items

        try:
            ui.on_main(
                lambda: _show_choice(window_id, _truncate_middle(caption), labels, selected, _on_done),
            )
            if remaining is not None:
                await asyncio.wait_for(event.wait(), remaining)
            else:
                await event.wait()
        except TIMEOUT_EXCEPTIONS:
            acp_log('elicitation', 'choice timed out - cancelling', window_id)
            return _CANCELLED
        finally:
            if _active_elicitations.get(window_id) is _resolve:
                del _active_elicitations[window_id]

        if not result or result[0] == -1:
            return _CANCELLED
        return options[result[0]][0]


async def _ask_text(window_id: int, caption: str, initial: str,
                    loop: Any, remaining: float | None) -> Any:
    """Prompt a single free-text field via input panel."""
    lock = _lock_for_window(window_id)
    async with lock:
        event = asyncio.Event()
        result: list = []

        def _finish(value: Any) -> None:
            def _set():
                result.append(value)
                event.set()

            if loop is not None and not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(_set)
                    return
                except RuntimeError:
                    pass
                return
            _set()

        def _resolve(value: Any) -> None:
            _active_elicitations.pop(window_id, None)
            _finish(value)

        prior = _active_elicitations.pop(window_id, None)
        if prior is not None:
            acp_log('elicitation', 'previous elicitation superseded - cancelling', window_id)
            prior(_CANCELLED)
        _active_elicitations[window_id] = _resolve

        try:
            ui.on_main(
                lambda: _show_input(
                    window_id, _truncate_middle(caption), initial,
                    lambda text: _finish(text), lambda: _finish(_CANCELLED),
                ),
            )
            if remaining is not None:
                await asyncio.wait_for(event.wait(), remaining)
            else:
                await event.wait()
        except TIMEOUT_EXCEPTIONS:
            acp_log('elicitation', 'input timed out - cancelling', window_id)
            return _CANCELLED
        finally:
            if _active_elicitations.get(window_id) is _resolve:
                del _active_elicitations[window_id]

        if not result or result[0] is _CANCELLED:
            return _CANCELLED
        return result[0]


def _coerce_value(text: str, spec: dict) -> Any:
    """Coerce free-text input to the schema type, or ``_CANCELLED``."""
    kind = spec.get('type', 'string')
    if kind == 'integer':
        try:
            return int(text.strip())
        except ValueError:
            return _CANCELLED
    if kind == 'number':
        try:
            return float(text.strip())
        except ValueError:
            return _CANCELLED
    if kind == 'boolean':
        lowered = text.strip().lower()
        if lowered in ('true', '1', 'yes', 'y'):
            return True
        if lowered in ('false', '0', 'no', 'n'):
            return False
        return _CANCELLED
    return text


async def resolve_elicitation(
    params: dict,
    window_id: int | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
    timeout: float = PERMISSION_PROMPT_TIMEOUT,
) -> dict:
    """Resolve an ``elicitation/create`` form request to a result dict.

    Collects each property in *requestedSchema* via sequential native
    panels sharing one whole-form deadline. Any dismiss, validation
    failure, or timeout cancels the whole form.

    Args:
        params: The ``elicitation/create`` params dict.
        window_id: Owning window for panels; ``None`` auto-cancels
            (one-shot mode has no interactive window).
        loop: Running event loop for thread-safe wakeups.
        timeout: Whole-form budget in seconds (0 = wait forever).

    Returns:
        ``{"action": "accept", "content": {...}}`` on success, else
        ``{"action": "cancel"}``.
    """
    if window_id is None:
        return {'action': 'cancel'}
    if not isinstance(params, dict) or params.get('mode', 'form') != 'form':
        acp_log('elicitation', 'unsupported elicitation mode - cancelling', window_id)
        return {'action': 'cancel'}
    schema = params.get('requestedSchema')
    if not isinstance(schema, dict):
        return {'action': 'cancel'}
    props = schema.get('properties')
    if not isinstance(props, dict) or not props:
        return {'action': 'cancel'}

    message = params.get('message') or 'Agent question'
    required = schema.get('required') if isinstance(schema.get('required'), list) else []
    ordered = [k for k in required if k in props] + [k for k in props if k not in required]

    running = loop
    if running is None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
    start = running.time() if running is not None else None
    deadline = start + timeout if start is not None and timeout and timeout > 0 else None

    content: dict[str, Any] = {}
    for key in ordered:
        spec = props[key]
        if not isinstance(spec, dict):
            acp_log('elicitation', f'unsupported schema for {key!r} - cancelling', window_id)
            return {'action': 'cancel'}
        if (
            spec.get('type') not in (None, 'string', 'number', 'integer', 'boolean')
            and _enum_options(spec) is None
        ):
            acp_log('elicitation', f'unsupported schema for {key!r} - cancelling', window_id)
            return {'action': 'cancel'}
        title = spec.get('title') or key
        caption = f'{message} - {title}'
        remaining = deadline - running.time() if deadline is not None and running is not None else None
        if remaining is not None and remaining <= 0:
            acp_log('elicitation', 'whole-form timeout exceeded - cancelling', window_id)
            return {'action': 'cancel'}

        options = _enum_options(spec)
        if options is not None:
            acp_log('elicitation', f'prompting choice: {key!r} ({len(options)} options)', window_id)
            value = await _ask_choice(window_id, caption, options, spec.get('default'), loop, remaining)
        else:
            initial = str(spec.get('default') or '')
            if spec.get('description'):
                caption = f'{caption}: {spec["description"]}'
            acp_log('elicitation', f'prompting text: {key!r}', window_id)
            value = await _ask_text(window_id, caption, initial, loop, remaining)
            if value is _CANCELLED:
                pass
            elif isinstance(value, str) and value == '' and spec.get('default') is not None:
                value = spec['default']
            elif isinstance(value, str) and value == '' and key not in required:
                continue
            elif isinstance(value, str):
                value = _coerce_value(value, spec)
            else:
                value = _CANCELLED
        if value is _CANCELLED:
            acp_log('elicitation', f'field {key!r} cancelled - cancelling form', window_id)
            return {'action': 'cancel'}
        content[key] = value

    return {'action': 'accept', 'content': content}
