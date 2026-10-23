"""Elicitation prompt handling for agent questions."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import sublime

from ..protocol import TIMEOUT_EXCEPTIONS, acp_log
from . import ui
from .config import PERMISSION_PROMPT_TIMEOUT

_CANCELLED: Any = object()
_OMIT: Any = object()

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


def dismiss_elicitation_prompt(window_id: int) -> None:
    """Dismiss any open elicitation panel for a window."""
    def _dismiss():
        window = sublime.Window(window_id)
        if window is not None:
            window.run_command('hide_overlay')
            window.run_command('hide_panel', {'cancel': True})
        resolver = _active_elicitations.pop(window_id, None)
        if resolver is not None:
            resolver(_CANCELLED)

    ui.on_main(_dismiss)


def _const_options(variants: Any) -> list[tuple[Any, str, str]] | None:
    """Return options for an ``oneOf``/``anyOf`` list of const variants, or ``None``.

    Every variant must be a dict carrying ``const``; ``title``/``name`` labels
    the option and ``description`` annotates it.
    """
    if not isinstance(variants, list) or not all(
        isinstance(v, dict) and 'const' in v for v in variants
    ):
        return None
    return [
        (v['const'], str(v.get('title') or v.get('name') or v['const']),
         str(v.get('description') or ''))
        for v in variants
    ]


def _enum_options(spec: dict) -> list[tuple[Any, str, str]] | None:
    """Return ``(value, label, description)`` options, or ``None`` for free text."""
    if isinstance(spec.get('enum'), list):
        return [(v, str(v), '') for v in spec['enum']]
    for key in ('oneOf', 'anyOf'):
        options = _const_options(spec.get(key))
        if options is not None:
            return options
    if spec.get('type') == 'boolean':
        return [(True, 'True', ''), (False, 'False', '')]
    return None


def _multi_options(spec: dict) -> list[tuple[Any, str, str]] | None:
    """Return ``(value, label, description)`` options for an array property.

    Multi-select properties carry their choices under ``items``, either as a
    plain ``enum`` or as titled ``oneOf``/``anyOf`` variants; anything else -
    including non-array types - returns ``None``.
    """
    if spec.get('type') != 'array':
        return None
    items = spec.get('items')
    if not isinstance(items, dict):
        return None
    if isinstance(items.get('enum'), list):
        return [(v, str(v), '') for v in items['enum']]
    for key in ('oneOf', 'anyOf'):
        options = _const_options(items.get(key))
        if options is not None:
            return options
    return None


def _skip_custom(key: str, required: bool, content: dict) -> bool:
    """Return whether an optional ``*_custom`` field is moot."""
    if required or not key.endswith('_custom'):
        return False
    value = content.get(key[:-len('_custom')])
    return isinstance(value, str) and value.strip().lower() != 'custom'


def _prune_stale(content: dict[str, Any], required: list, window_id: int) -> None:
    """Drop collected ``*_custom`` values whose sibling is no longer ``'custom'``"""
    for stale in [k for k in content if _skip_custom(k, k in required, content)]:
        acp_log('elicitation', f'dropping stale {stale!r} - sibling is not custom', window_id)
        content.pop(stale)


def _row_kind(checked: bool | None, ready: bool = True) -> tuple:
    """Return the quick-panel gutter ``kind`` for a checklist row."""
    ambiguous = getattr(sublime, 'KIND_ID_AMBIGUOUS', 0)
    if checked is None:
        if ready:
            return (getattr(sublime, 'KIND_ID_COLOR_YELLOWISH', ambiguous), '→', '')
        return (getattr(sublime, 'KIND_ID_COLOR_REDISH', ambiguous), '→', '')
    if checked:
        return (getattr(sublime, 'KIND_ID_COLOR_GREENISH', ambiguous), '✓', '')
    return (ambiguous, '', '')


def _gutter(kind_attr: str, char: str) -> tuple:
    """Return a quick-panel gutter ``kind`` tuple showing *char*."""
    ambiguous = getattr(sublime, 'KIND_ID_AMBIGUOUS', 0)
    return (getattr(sublime, kind_attr, ambiguous), char, '')


def _show_choice(window_id: int, caption: str, labels: list, selected: int,
                 on_done: Callable, shown: list | None = None,
                 flash: bool = True) -> None:
    """Show a quick panel with *labels* on the main thread."""
    if window_id not in [w.id() for w in sublime.windows()]:
        on_done(-1)
        return
    window = sublime.Window(window_id)
    if flash:
        from . import broadcast
        broadcast.flash_prompt(f'ACP: question - {caption}', window_id)
    if shown is not None:
        shown.append(True)
    window.show_quick_panel(labels, on_done, selected_index=selected, placeholder=caption)


def _show_input(window_id: int, caption: str, initial: str,
                on_done: Callable, on_cancel: Callable) -> None:
    """Show an input panel on the main thread."""
    if window_id not in [w.id() for w in sublime.windows()]:
        on_cancel()
        return
    window = sublime.Window(window_id)
    from . import broadcast
    broadcast.flash_prompt(f'ACP: input needed - {caption}', window_id)
    window.show_input_panel(caption, initial, on_done, None, on_cancel)


async def _ask_choice(window_id: int, caption: str, options: list[tuple[Any, str, str]],
                      default: Any, loop: Any, remaining: float | None,
                      multi: bool = False, min_items: int = 0,
                      max_items: int | None = None) -> Any:
    """Prompt an enum, boolean, or array field via quick panel."""
    lock = _lock_for_window(window_id)
    async with lock:
        event = asyncio.Event()
        result: list = []
        chosen: set = set()
        if multi and isinstance(default, list):
            chosen = {i for i, (value, _, _) in enumerate(options) if value in default}
            if max_items is not None and len(chosen) > max_items:
                acp_log(
                    'elicitation',
                    f'default preselects {len(chosen)} rows - keeping the first {max_items}',
                    window_id,
                )
                chosen = set(sorted(chosen)[:max_items])

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

        panel_shown: list = []
        running_loop = asyncio.get_running_loop()
        deadline = running_loop.time() + remaining if remaining is not None else None
        notify_text = f'ACP: question - {ui.truncate_middle(caption)}'
        try:
            if not await ui.wait_for_window_active(
                window_id, deadline, cancelled=event.is_set, notify_text=notify_text,
            ):
                if not event.is_set():
                    acp_log('elicitation', 'choice timed out waiting for focus - cancelling', window_id)
                return _CANCELLED
            if event.is_set():
                return _CANCELLED

            selected = 0
            for i, (value, _, _) in enumerate(options):
                if not multi and default is not None and value == default:
                    selected = i
            def _acceptable() -> bool:
                """Return ``True`` when the picks satisfy the ``minItems``/``maxItems`` bounds."""
                if len(chosen) < min_items:
                    return False
                return max_items is None or len(chosen) <= max_items

            def _labels() -> list:
                """Build the panel rows, gutter marks and ``Done`` row included."""
                rows: list = []
                for i, (_, label, desc) in enumerate(options):
                    short = ui.truncate_middle(desc) if desc else ui.truncate_middle(caption)
                    if multi:
                        rows.append(sublime.QuickPanelItem(
                            label, details=short, kind=_row_kind(i in chosen)))
                    else:
                        rows.append(sublime.QuickPanelItem(label, details=short))
                if not multi:
                    return rows
                done = f'Done ({len(chosen)} selected)'
                if min_items - len(chosen) > 0:
                    done = f'Done (choose {min_items - len(chosen)} more)'
                rows.append(sublime.QuickPanelItem(
                    done, details=ui.truncate_middle(caption),
                    kind=_row_kind(None, ready=_acceptable()),
                ))
                return rows

            while True:
                if deadline is not None:
                    remaining = deadline - running_loop.time()
                    if remaining <= 0:
                        acp_log('elicitation', 'whole-form timeout exceeded - cancelling', window_id)
                        return _CANCELLED
                labels = _labels()

                def _show(labels=labels, selected=selected) -> None:
                    _show_choice(
                        window_id, ui.truncate_middle(caption), labels, selected,
                        _on_done, shown=panel_shown, flash=not panel_shown,
                    )

                ui.on_main(_show)
                shown_at = running_loop.time()
                if remaining is not None:
                    await asyncio.wait_for(event.wait(), remaining)
                else:
                    await event.wait()
                if (
                    panel_shown and result and result[0] == -1
                    and await ui.panel_cancel_is_background(window_id, running_loop.time() - shown_at)
                ):
                    acp_log('elicitation', 'panel auto-dismissed while unfocused - waiting for focus', window_id)
                    event.clear()
                    result.clear()
                    del panel_shown[:]
                    if not await ui.wait_for_window_active(
                        window_id, deadline, cancelled=event.is_set, notify_text=notify_text,
                    ):
                        if not event.is_set():
                            acp_log('elicitation', 'choice timed out waiting for focus - cancelling', window_id)
                        return _CANCELLED
                    if event.is_set():
                        return _CANCELLED
                    continue
                if not multi or not result:
                    break
                index = result[0]
                if index == len(options) and _acceptable():
                    break
                event.clear()
                result.clear()
                if index is _CANCELLED or index == -1:
                    return _CANCELLED
                if index == len(options):
                    acp_log('elicitation', f'{min_items} selections required - keeping form open', window_id)
                    continue
                if index in chosen:
                    chosen.discard(index)
                elif max_items is None or len(chosen) < max_items:
                    chosen.add(index)
                else:
                    acp_log('elicitation', f'at most {max_items} selections - ignoring toggle', window_id)
                selected = index
        except TIMEOUT_EXCEPTIONS:
            acp_log('elicitation', 'choice timed out - cancelling', window_id)
            return _CANCELLED
        finally:
            if _active_elicitations.get(window_id) is _resolve:
                del _active_elicitations[window_id]

        if multi:
            if result and result[0] == len(options):
                return [options[i][0] for i in sorted(chosen)]
            return _CANCELLED
        if not result or not isinstance(result[0], int) or result[0] == -1:
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
                    window_id, ui.truncate_middle(caption), initial,
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


async def _prompt_field(
    window_id: int,
    message: str,
    key: str,
    spec: dict,
    required: bool,
    loop: asyncio.AbstractEventLoop | None,
    deadline: float | None,
    current: Any = _OMIT,
    agent_name: str = '',
) -> Any:
    """Prompt one form field.

    Enum/boolean properties resolve on a quick-panel row, ``array``
    properties toggle rows and confirm via ``Done``, and remaining
    primitives take free text. *current* replaces the schema ``default`` so
    a review edit starts from the collected value.

    Args:
        window_id: Owning window for the panels.
        message: Form message used as the caption prefix.
        key: Property name.
        spec: The property's schema dict.
        required: Whether the property is listed in ``required``.
        loop: Running event loop for thread-safe wakeups.
        deadline: Absolute running-loop time the form must not pass, or
            ``None`` for no limit.
        current: Previously collected value to pre-fill, or ``_OMIT``.
        agent_name: Agent asking the question, prefixed to the caption when known.

    Returns:
        The coerced value, ``_OMIT`` for a blank optional field, or
        ``_CANCELLED`` when dismissed, timed out, or unsupported.
    """
    multi_options = _multi_options(spec)
    options = multi_options if multi_options is not None else _enum_options(spec)
    if (
        options is None
        and spec.get('type') not in (None, 'string', 'number', 'integer', 'boolean')
    ):
        acp_log('elicitation', f'unsupported schema for {key!r} - cancelling', window_id)
        return _CANCELLED
    title = spec.get('title') or key
    speaker = f'{agent_name}: ' if agent_name else ''
    caption = f'{speaker}{message} - {title}'
    remaining = deadline - loop.time() if deadline is not None and loop is not None else None
    if remaining is not None and remaining <= 0:
        acp_log('elicitation', 'whole-form timeout exceeded - cancelling', window_id)
        return _CANCELLED
    default = spec.get('default') if current is _OMIT else current

    if multi_options is not None:
        raw_min = spec.get('minItems')
        min_items = raw_min if isinstance(raw_min, int) else (1 if required else 0)
        raw_max = spec.get('maxItems')
        max_items = raw_max if isinstance(raw_max, int) else None
        acp_log('elicitation', f'prompting multi-choice: {key!r} ({len(multi_options)} options)', window_id)
        return await _ask_choice(
            window_id, caption, multi_options, default, loop, remaining,
            multi=True, min_items=min_items, max_items=max_items,
        )
    if options is not None:
        acp_log('elicitation', f'prompting choice: {key!r} ({len(options)} options)', window_id)
        return await _ask_choice(window_id, caption, options, default, loop, remaining)

    initial = '' if default is None else str(default)
    if spec.get('description'):
        caption = f'{caption}: {spec["description"]}'
    acp_log('elicitation', f'prompting text: {key!r}', window_id)
    value = await _ask_text(window_id, caption, initial, loop, remaining)
    if value is _CANCELLED:
        return _CANCELLED
    if isinstance(value, str) and value == '' and not required:
        return _OMIT
    if isinstance(value, str):
        return _coerce_value(value, spec)
    return _CANCELLED


def _review_keys(content: dict[str, Any], props: dict[str, Any],
                 required: list) -> list:
    """Return the property names that get an edit row in the review panel.

    Skips optional ``*_custom`` fields that are moot because their sibling
    already resolved to something other than ``'custom'``.

    Args:
        content: Values collected so far, keyed by property name.
        props: The ``requestedSchema`` properties map.
        required: Names listed in the schema ``required`` array.

    Returns:
        Property names in the same order as *props*.
    """
    return [
        key for key in props
        if not _skip_custom(key, key in required, content)
    ]


def _review_rows(content: dict[str, Any], props: dict[str, Any],
                 keys: list) -> list:
    """Build the review panel rows: send, decline, then one edit row per field.

    Kept out of the async body so no ``sublime`` API call sits directly in an
    ``async def`` (the package reviewer treats those as module scope).

    Args:
        content: Values collected so far, keyed by property name.
        props: The ``requestedSchema`` properties map.
        keys: Property names to render, as returned by :func:`_review_keys`.

    Returns:
        ``sublime.QuickPanelItem`` rows in display order.
    """
    rows = [
        sublime.QuickPanelItem(
            'Send answers', details='Submit the collected values',
            kind=_gutter('KIND_ID_COLOR_GREENISH', '✓'),
        ),
        sublime.QuickPanelItem(
            'Decline', details='Do not answer this request',
            kind=_gutter('KIND_ID_COLOR_REDISH', '✗'),
        ),
    ]
    for key in keys:
        spec = props[key]
        title = (spec.get('title') if isinstance(spec, dict) else None) or key
        value = content.get(key, _OMIT)
        detail = 'not set' if value is _OMIT else ui.truncate_middle(value)
        rows.append(sublime.QuickPanelItem(
            f'Edit: {title}', details=detail,
            kind=_gutter('KIND_ID_NAVIGATION', '→'),
        ))
    return rows


async def _review_form(
    window_id: int,
    message: str,
    content: dict[str, Any],
    props: dict[str, Any],
    required: list,
    loop: asyncio.AbstractEventLoop | None,
    deadline: float | None,
    agent_name: str = '',
) -> Any:
    """Show the collected answers and return the user's next step.

    Rows are ``Send answers``, ``Decline``, then one ``Edit`` row per
    property, with unset optional fields marked ``not set``. Optional
    ``*_custom`` fields whose sibling is not ``'custom'`` get no row. Esc
    dismisses the panel, which cancels the form.

    Args:
        window_id: Owning window for the panel.
        message: Form message used as the panel placeholder.
        content: Values collected so far, keyed by property name.
        props: The ``requestedSchema`` properties map.
        required: Names listed in the schema ``required`` array.
        loop: Running event loop for thread-safe wakeups.
        deadline: Absolute running-loop time the form must not pass, or
            ``None`` for no limit.
        agent_name: Agent asking the question, prefixed to the caption when known.

    Returns:
        ``'accept'`` to submit, ``'decline'`` to refuse the request,
        ``('edit', key)`` to re-prompt one property, or ``_CANCELLED`` on
        dismissal, timeout, or a closed window.
    """
    keys = _review_keys(content, props, required)
    rows = _review_rows(content, props, keys)

    running_loop = asyncio.get_running_loop()
    remaining = deadline - running_loop.time() if deadline is not None else None
    if remaining is not None and remaining <= 0:
        acp_log('elicitation', 'whole-form timeout exceeded - cancelling', window_id)
        return _CANCELLED
    panel_deadline = running_loop.time() + remaining if remaining is not None else None
    short_message = ui.truncate_middle(message)
    panel_caption = f'{agent_name}: {short_message}' if agent_name else short_message
    notify_text = (
        f'ACP: question from {agent_name} - {short_message}'
        if agent_name else f'ACP: question - {short_message}'
    )

    lock = _lock_for_window(window_id)
    async with lock:
        event = asyncio.Event()
        result: list = []

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

        panel_shown: list = []
        try:
            if not await ui.wait_for_window_active(
                window_id, panel_deadline, cancelled=event.is_set, notify_text=notify_text,
            ):
                if not event.is_set():
                    acp_log('elicitation', 'review timed out waiting for focus - cancelling', window_id)
                return _CANCELLED
            if event.is_set():
                return _CANCELLED
            while True:
                if panel_deadline is not None:
                    remaining = panel_deadline - running_loop.time()
                    if remaining <= 0:
                        acp_log('elicitation', 'whole-form timeout exceeded - cancelling', window_id)
                        return _CANCELLED

                def _show() -> None:
                    _show_choice(
                        window_id, panel_caption, rows, 0,
                        _on_done, shown=panel_shown, flash=not panel_shown,
                    )

                ui.on_main(_show)
                shown_at = running_loop.time()
                if remaining is not None:
                    await asyncio.wait_for(event.wait(), remaining)
                else:
                    await event.wait()
                if (
                    panel_shown and result and result[0] == -1
                    and await ui.panel_cancel_is_background(window_id, running_loop.time() - shown_at)
                ):
                    acp_log('elicitation', 'review auto-dismissed while unfocused - waiting for focus', window_id)
                    event.clear()
                    result.clear()
                    del panel_shown[:]
                    if not await ui.wait_for_window_active(
                        window_id, panel_deadline, cancelled=event.is_set, notify_text=notify_text,
                    ):
                        if not event.is_set():
                            acp_log('elicitation', 'review timed out waiting for focus - cancelling', window_id)
                        return _CANCELLED
                    if event.is_set():
                        return _CANCELLED
                    continue
                break
        except TIMEOUT_EXCEPTIONS:
            acp_log('elicitation', 'review timed out - cancelling', window_id)
            return _CANCELLED
        finally:
            if _active_elicitations.get(window_id) is _resolve:
                del _active_elicitations[window_id]

        if not result or not isinstance(result[0], int) or result[0] == -1:
            return _CANCELLED
        index = result[0]
        if index == 0:
            return 'accept'
        if index == 1:
            return 'decline'
        offset = index - 2
        if 0 <= offset < len(keys):
            return ('edit', keys[offset])
        return _CANCELLED


async def resolve_elicitation(
    params: dict,
    window_id: int | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
    timeout: float = PERMISSION_PROMPT_TIMEOUT,
    agent_name: str = '',
) -> dict:
    """Resolve an ``elicitation/create`` form request to a result dict.

    Collects each property in *requestedSchema* via sequential native
    panels sharing one whole-form deadline: enum/boolean fields resolve on a
    quick-panel row, ``array`` fields toggle rows and confirm via the panel's
    ``Done`` row, and remaining primitives take free text.

    A required array field needs at least one selection unless its schema
    states ``minItems`` explicitly; ``minItems``/``maxItems`` are enforced
    either way.

    A form left with at most one visible field is accepted as soon as that
    field is answered - the review panel would only repeat it - while forms
    with two or more visible fields still go through review.

    Args:
        params: The ``elicitation/create`` params dict.
        window_id: Owning window for panels; ``None`` auto-cancels
            (one-shot mode has no interactive window).
        loop: Running event loop for thread-safe wakeups.
        timeout: Whole-form budget in seconds (0 = wait forever).
        agent_name: Agent asking the question, prefixed to panel captions
            when known so users can tell agents apart.

    Returns:
        ``{"action": "accept", "content": {...}}`` when submitted,
        ``{"action": "decline"}`` when the user declines, else
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
    if props is None:
        props = {}
    if not isinstance(props, dict):
        return {'action': 'cancel'}
    if not props:
        return {'action': 'accept', 'content': {}}

    message = (
        params.get('message')
        or schema.get('title')
        or schema.get('description')
        or 'Agent question'
    )
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
        is_required = key in required
        spec = props[key]
        if not isinstance(spec, dict):
            acp_log('elicitation', f'unsupported schema for {key!r} - cancelling', window_id)
            return {'action': 'cancel'}
        if _skip_custom(key, is_required, content):
            acp_log('elicitation', f'skipping {key!r} - sibling is not custom', window_id)
            continue
        value = await _prompt_field(
            window_id, message, key, spec, is_required, running, deadline,
            agent_name=agent_name,
        )
        if value is _CANCELLED:
            acp_log('elicitation', f'field {key!r} cancelled - cancelling form', window_id)
            return {'action': 'cancel'}
        if value is not _OMIT:
            content[key] = value

    _prune_stale(content, required, window_id)
    visible = _review_keys(content, props, required)
    if len(visible) <= 1:
        acp_log('elicitation', 'at most one visible field - accepting without review', window_id)
        return {'action': 'accept', 'content': content}

    while True:
        decision = await _review_form(
            window_id, message, content, props, required, running, deadline,
            agent_name=agent_name,
        )
        if decision is _CANCELLED:
            return {'action': 'cancel'}
        if decision == 'decline':
            acp_log('elicitation', 'form declined', window_id)
            return {'action': 'decline'}
        if decision == 'accept':
            _prune_stale(content, required, window_id)
            return {'action': 'accept', 'content': content}
        _, key = decision
        value = await _prompt_field(
            window_id, message, key, props[key], key in required, running, deadline,
            current=content.get(key, _OMIT), agent_name=agent_name,
        )
        if value is _CANCELLED:
            acp_log('elicitation', f'field {key!r} cancelled - cancelling form', window_id)
            return {'action': 'cancel'}
        if value is _OMIT:
            content.pop(key, None)
        else:
            content[key] = value
