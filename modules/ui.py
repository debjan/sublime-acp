"""UI helpers - output views, status bar, input panel wiring."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Callable

import sublime

from ..protocol import TIMEOUT_EXCEPTIONS, acp_log
from . import broadcast
from .config import SESSION_PROMPT, STATUS_KEY_DAEMON, TURN_DIVIDER, settings


def dividers_enabled() -> bool:
    """Return whether turn dividers are enabled (``turn_dividers`` setting)."""
    return bool(settings().get('turn_dividers', True))


# Alias avoids a literal `sublime.set_timeout` call inside an `async def` body,
# which `st_package_reviewer` flags as an unsafe pre-init API call on builds < 4171.
_set_timeout = sublime.set_timeout


def on_main(fn: Callable[[], Any], delay: int = 0) -> None:
    """Schedule *fn* to run on Sublime Text's main thread.

    All ``sublime`` API calls made from background threads must be dispatched
    to the main thread. This is a thin wrapper over ``sublime.set_timeout``.

    Args:
        fn: Zero-argument callable to run on the main thread.
        delay: Delay in milliseconds. Defaults to 0 (run as soon as possible).
    """
    _set_timeout(fn, delay)


async def call_on_main(fn: Callable[[], Any]) -> Any:
    """Run *fn* on Sublime's main thread and await its result.

    Must be awaited from a daemon event loop thread. The callable runs
    via ``sublime.set_timeout`` (main thread); the result or exception
    is marshalled back with ``call_soon_threadsafe`` so the loop is
    never blocked.

    Args:
        fn: Zero-argument callable to run on the main thread.

    Returns:
        Whatever *fn* returns.
    """
    running = asyncio.get_running_loop()
    fut = running.create_future()

    def _run() -> None:
        if fut.done():
            return
        try:
            result = fn()
        except Exception as exc:
            running.call_soon_threadsafe(fut.set_exception, exc)
        else:
            running.call_soon_threadsafe(fut.set_result, result)

    _set_timeout(_run, 0)
    return await fut


def _focus_snapshot_unsafe(window_id: int) -> tuple:
    """Capture window/focus state; main thread only (no marshaling)."""
    open_ids = [w.id() for w in sublime.windows()]
    if window_id not in open_ids:
        return (False, None, None)
    active = sublime.active_window()
    active_id = active.id() if active is not None else None
    return (True, active_id, owner_has_os_focus(window_id))


async def _focus_snapshot(window_id: int) -> tuple:
    """Capture window/focus state via the main thread.

    Raises:
        Exception: Whatever the main-thread snapshot raised, or a
            timeout when the main thread does not answer within 5s.
    """
    return await asyncio.wait_for(call_on_main(lambda: _focus_snapshot_unsafe(window_id)), 5)


def _find_reusable_empty_view(window: sublime.Window) -> sublime.View | None:
    """Return the single empty untitled view if it is the only one open."""
    views = window.views()
    if len(views) != 1:
        return None
    view = views[0]
    if view.file_name() is None and view.size() == 0 and not view.is_dirty():
        return view
    return None


def create_output_view(window: sublime.Window, agent_name: str = '',
                       role: str = 'prompt') -> sublime.View:
    """Create a scratch buffer output view for ACP responses.

    Reuses the window's single view in place when it is an empty untitled
    buffer, so a fresh window keeps a single group instead of gaining a
    split. Otherwise creates a new scratch view.
    The view is configured with Markdown syntax, word wrap enabled, and
    line numbers hidden. The ``font_size`` setting overrides the view's
    font size when set (``null``/unset uses the global font size).

    Args:
        window: The Sublime window to create the view in.
        agent_name: Optional agent name to include in the view title.
        role: ``'prompt'`` for one-shot prompts, ``'daemon'`` for persistent sessions.

    Returns:
        The reused or newly created ``sublime.View``.
    """
    suffix = f': {agent_name}' if agent_name else ''
    label = 'ACP Chat' if role == 'daemon' else 'ACP Prompt'
    view = _find_reusable_empty_view(window) or window.new_file()
    view.set_scratch(True)
    view.set_name(f'{label}{suffix}')
    view.assign_syntax('Packages/Markdown/Markdown.sublime-syntax')
    vs = view.settings()
    vs.set('word_wrap', True)
    vs.set('line_numbers', False)
    if font_size := settings().get('font_size'):
        vs.set('font_size', font_size)
    return view


_one_shot_views: dict[int, sublime.View] = {}


def get_one_shot_view(window: sublime.Window, agent_name: str = '',
                      role: str = 'prompt') -> sublime.View:
    """Return the last one-shot view for *window* if still open, else create one.

    Reused views keep their layout position and simply gain focus; only
    newly created views are moved into the output split.
    """
    view = _one_shot_views.get(window.id())
    owner = view.window() if view is not None and view.is_valid() else None
    if view is not None and owner is not None and owner.id() == window.id():
        window.focus_view(view)
        return view
    view = create_output_view(window, agent_name, role=role)
    _one_shot_views[window.id()] = view
    open_split_for_output(window, view)
    return view


def forget_one_shot_view(window_id: int) -> None:
    """Drop the remembered one-shot view for *window_id* (e.g. on close)."""
    _one_shot_views.pop(window_id, None)


def resolve_work_dir(window: sublime.Window, source_view: sublime.View) -> str:
    """Resolve the working directory for an ACP session.

    Prefers ``project_path``, falls back to ``folder``, then the source
    view's parent directory, then the first window folder, and finally the
    current process directory.

    Args:
        window: The Sublime window context.
        source_view: The view from which the command was invoked.

    Returns:
        An absolute directory path string.
    """
    vars_dict = window.extract_variables()
    work_dir: Path | None = None
    if candidate := vars_dict.get('project_path') or vars_dict.get('folder'):
        work_dir = Path(candidate)
    if work_dir is None and source_view:
        if fname := source_view.file_name():
            work_dir = Path(fname).parent
    if work_dir is None:
        if folders := window.folders():
            work_dir = Path(folders[0])
    return str(work_dir) if work_dir is not None else str(Path.cwd())


_MAX_ATTACH_SELECTIONS = 10


def attach_selection_to_prompt(prompt: str, source_view: sublime.View,
                               force: bool = False) -> str:
    """Append the current selections or cursor context to the prompt.

    No-op when ``attach_selection`` is disabled in settings unless *force*
    is set. With *force* (quick-action prompts), always appends the raw
    selected text in a code fence. Otherwise, if ``source_view`` has
    non-empty selections and a file name, appends ``@path:line-start-line-end``
    per selection; if no file name, appends the selected text in a code fence.
    At most ``_MAX_ATTACH_SELECTIONS`` selections are attached.

    Args:
        prompt: The original prompt text.
        source_view: The view whose selections to attach.
        force: Bypass the ``attach_selection`` setting and embed the raw
            selection text.

    Returns:
        The augmented prompt string.
    """
    if source_view is None:
        return prompt
    if not force and not settings().get('attach_selection', False):
        return prompt
    if not (regions := [r for r in source_view.sel() if not r.empty()]):
        return prompt
    if force or not source_view.file_name():
        blocks = '\n'.join(
            f'```\n{source_view.substr(r)}\n```'
            for r in regions[:_MAX_ATTACH_SELECTIONS]
        )
        return f'{prompt}\n{blocks}'
    path = source_view.file_name()

    def _ref(region) -> str:
        start = source_view.rowcol(region.begin())[0] + 1
        end = source_view.rowcol(region.end())[0] + 1
        return f'@{path}:{start}-{end}'

    refs = [_ref(r) for r in regions[:_MAX_ATTACH_SELECTIONS]]
    if len(regions) > _MAX_ATTACH_SELECTIONS:
        refs.append(f'(+{len(regions) - _MAX_ATTACH_SELECTIONS} more)')
    return f'{prompt} {" ".join(refs)}'


def open_split_for_output(window: sublime.Window, output_view: sublime.View) -> None:
    """Rearrange the window layout so the output view sits in a 2-column split.

    No-op when the output view is the window's only view (fresh window
    reusing its empty buffer) - there is nothing to split against.
    """
    if len(window.views()) <= 1:
        return
    num_groups = window.num_groups()
    active_group = window.active_group()

    if num_groups == 2:
        target_group = 1 - active_group
        window.set_view_index(output_view, target_group, 0)
        return

    if num_groups == 1:
        layout = window.layout()
        cols = list(layout.get('cols', [0, 1]))
        rows = list(layout.get('rows', [0, 1]))
        cells = list(layout.get('cells', [[0, 0, 1, 1]]))

        if len(cols) < 2 or len(rows) < 2:
            cols = [0.0, 1.0]
            rows = [0.0, 1.0]
            cells = [[0, 0, 1, 1]]

        mid = (cols[-2] + cols[-1]) / 2
        cols.insert(-1, mid)

        row_count = len(rows) - 1
        for r in range(row_count):
            cells.append([len(cols) - 2, r, len(cols) - 1, r + 1])

        window.set_layout({'cols': cols, 'rows': rows, 'cells': cells})

    target_group = min(window.active_group() + 1, window.num_groups() - 1)
    window.set_view_index(output_view, target_group, 0)


def make_stream_callback(
    output_view: sublime.View,
    state: Any | None = None,
) -> Callable[[str], None]:
    """Create a streaming callback that appends text to *output_view*.

    When *state* is provided (daemon mode), the live view is resolved from
    ``state`` on every chunk, so output follows a recreated tab after the
    user closes the old one. The callback also tracks ``has_replied`` and
    focuses the view on the first reply, but only when the owning window is
    already active - a background window (e.g. while typing in another
    Sublime window) gets no focus steal, just the turn-end flash. All view
    mutations run on the main thread via ``sublime.set_timeout``.

    Args:
        output_view: The view to append streamed chunks to (fallback when
            *state* holds no live view, and the target for one-shot mode).
        state: Optional ``DaemonState`` for daemon-mode extras.

    Returns:
        A callable ``(text: str) -> None`` suitable as ``rpc.acp``'s callback.
    """
    def _on_chunk(text: str) -> None:
        if state is not None:
            has_replied = state.get('has_replied')
            state.set(has_replied=True)
            if not has_replied:
                def _focus():
                    ov = state.get('output_view')
                    window = ov.window() if ov else None
                    if window is None:
                        return
                    active = sublime.active_window()
                    if active is not None and active.id() == window.id():
                        group, _ = window.get_view_index(ov)
                        window.focus_group(group)
                        window.focus_view(ov)
                    else:
                        acp_log('ui', 'owning window inactive - skipping output focus (no steal)', window.id())
                on_main(_focus)

        def _append_live(t=text):
            live = state.get('output_view') if state is not None else output_view
            target = live if live is not None else output_view
            if target is not None:
                _append_text(target, t)
        on_main(_append_live)
    return _on_chunk


def _append_text(view: sublime.View, text: str) -> None:
    """Append *text* to *view* at EOF with smart auto-scroll (main thread only)."""
    if not view.window():
        return  # view was closed
    try:
        was_at_end = view.size() - view.visible_region().b < 50
        view.run_command('append', {'characters': text, 'scroll_to_end': was_at_end})
    except TypeError:
        pass  # view was destroyed during shutdown


def append_to_output_view(view: sublime.View, text: str) -> None:
    """Append text to the daemon output view with smart auto-scroll."""
    on_main(lambda: _append_text(view, text))


def replace_output_view(view: sublime.View, text: str) -> None:
    """Clear *view* and replace its content with *text* (main thread only)."""

    def _replace() -> None:
        try:
            view.run_command('select_all')
            view.run_command('left_delete')
            _append_text(view, text)
        except Exception as exc:
            acp_log('ui', f'replace output view failed: {exc!r}')

    on_main(_replace)


def append_turn_divider(view: sublime.View, enabled: bool | None = None) -> None:
    """Append a markdown horizontal rule marking the end of an agent turn."""
    if enabled is None:
        enabled = dividers_enabled()
    if enabled:
        append_to_output_view(view, TURN_DIVIDER)


def append_prompt_turn(view: sublime.View, prompt: str,
                       source_view: sublime.View | None = None,
                       force_selection: bool = False) -> None:
    """Append the user's prompt followed by a divider before the response."""
    divider = TURN_DIVIDER if dividers_enabled() else '\n'
    block = f'\n> **User**: {prompt}{divider}'
    append_to_output_view(view, block)
    if source_view is not None:
        if sel_display := attach_selection_to_prompt('', source_view, force=force_selection):
            sel_display = sel_display.lstrip()
            append_to_output_view(view, f'{sel_display}\n{divider}')


def window_by_id(window_id):
    """Return the open window matching *window_id*, or ``None``.

    Main thread only; callers on daemon threads must marshal via
    :func:`call_on_main` or :func:`on_main`.
    """
    if not isinstance(window_id, int):
        return None
    for w in sublime.windows():
        if w.id() == window_id:
            return w
    return None


async def wait_for_window_active(window_id: int, deadline: float | None = None,
                                 poll: float = 0.25, cancelled: Any = None,
                                 notify_text: str | None = None) -> bool:
    """Wait until *window_id* can host a quick panel.

    Quick panels are overlays bound to the active window; showing one on a
    background window fires ``on_done(-1)`` immediately. ``sublime`` only
    tracks the active window *within* the app, so on Windows the OS
    foreground window must additionally belong to *window_id* (Alt+Tab away
    and back otherwise slips through the gate). Callers gate panel display
    on this helper so background prompts defer instead of cancelling.

    Args:
        window_id: Owning window to wait for.
        deadline: Running-loop timestamp the wait must not pass, or ``None``
            to wait forever.
        poll: Seconds between activation checks.
        cancelled: Optional zero-argument callable polled alongside
            activation; a truthy return aborts the wait.
        notify_text: Optional prompt text flashed (status bar plus OS
            taskbar/banner) once deferral begins, so an away user is nudged
            while the panel itself stays unshown until focus returns.

    Returns:
        ``True`` when *window_id* is active, else ``False`` on timeout,
        cancellation, or a closed window.

    Sublime API state is snapshotted on the main thread via
    :func:`call_on_main`; a snapshot failure is treated as transient
    (keep polling) rather than a cancel.
    """
    running = asyncio.get_running_loop()
    logged = False
    while True:
        if callable(cancelled):
            try:
                if cancelled():
                    return False
            except Exception as exc:
                acp_log('ui', f'cancel check failed: {exc!r}', window_id)
        try:
            is_open, active_id, os_focus = await _focus_snapshot(window_id)
        except TIMEOUT_EXCEPTIONS as exc:
            acp_log('ui', f'focus snapshot timed out - retrying: {exc!r}', window_id)
            is_open, active_id, os_focus = True, None, None
        except Exception as exc:
            acp_log('ui', f'focus snapshot failed - retrying: {exc!r}', window_id)
            is_open, active_id, os_focus = True, None, None
        if not is_open:
            return False
        if active_id == window_id and os_focus is not False:
            return True
        if deadline is not None and running.time() >= deadline:
            return False
        if not logged:
            acp_log('ui', 'owning window inactive - deferring quick panel until focused', window_id)
            if notify_text:
                broadcast.flash_prompt(notify_text, window_id)
            logged = True
        delay = poll
        if deadline is not None:
            delay = min(poll, max(0.05, deadline - running.time()))
        await asyncio.sleep(delay)


def owner_has_os_focus(window_id: int) -> bool | None:
    """Return whether the OS foreground window belongs to *window_id*.

    Main thread only; daemon threads must go through :func:`_focus_snapshot`.

    Windows-only (via ``GetForegroundWindow`` vs ``Window.hwnd()``);
    ``None`` on other platforms or when the handle is unavailable, in which
    case callers fall back to the ``sublime``-level activation check.
    """
    if sys.platform != 'win32':
        return None
    try:
        import ctypes
        window = sublime.Window(window_id)
        hwnd = window.hwnd() if window is not None else None
    except Exception:
        return None
    if not hwnd:
        return None
    try:
        return ctypes.windll.user32.GetForegroundWindow() == hwnd
    except Exception:
        return None


#: A quick-panel cancel faster than this is unlikely to be a deliberate Esc.
FAST_CANCEL_GRACE = 1.0


async def panel_cancel_is_background(window_id: int, elapsed: float) -> bool:
    """Return whether a quick-panel cancel looks like an auto-dismiss.

    Distinguishes Sublime auto-dismissing a panel shown while unfocused
    (``on_done(-1)`` within ~200ms, no user involved) from a genuine user
    Esc. Decided by OS focus where available, else by Sublime-level
    activation; the cancel latency grace only applies when the active
    window is unknown, never to override a known-active owner.

    Window/focus state is snapshotted on the main thread; a snapshot
    failure returns ``False`` (treat as a genuine cancel).
    """
    try:
        is_open, active_id, focus = await _focus_snapshot(window_id)
    except Exception as exc:
        acp_log('ui', f'cancel focus snapshot failed: {exc!r}', window_id)
        return False
    if not is_open:
        return False
    if focus is False:
        return True
    if focus is True:
        return False
    if active_id is not None and active_id != window_id:
        return True
    if active_id == window_id:
        return False
    return elapsed < FAST_CANCEL_GRACE


def truncate_middle(text: str, max_chars: int = 160) -> str:
    """Shorten long text to fit the quick panel, keeping head and tail."""
    text = ' '.join(str(text).split())
    if len(text) <= max_chars:
        return text
    keep = max_chars - 1
    head = keep * 2 // 3
    return text[:head] + '…' + text[len(text) - (keep - head):]


def format_local_time(value: Any) -> str | None:
    """Convert an ACP UTC timestamp to local time for display, or ``None``."""
    if value is None or value == '':
        return None
    try:
        from datetime import datetime, timezone
        if isinstance(value, (int, float)):
            ts = value / 1000.0 if value > 1e11 else float(value)
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M')
        if not (text := str(value).strip().replace('Z', '+00:00')):
            return None
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime('%Y-%m-%d %H:%M')
    except (ValueError, OverflowError, OSError):
        pass
    return str(value)


def display_session_title(session: dict) -> str:
    """Return a session title, falling back to the full session ID.

    A local override set via ``ACP: Rename Session`` is explicit user intent,
    so it wins. Otherwise the agent's ``title`` is used, unless it is a
    verbatim echo of :data:`~modules.config.SESSION_PROMPT`. The session ID is
    the final fallback.
    """
    if override := (session.get('title_override') or '').strip():
        return override
    if title := (session.get('title') or '').strip():
        normalized_title = ' '.join(title.split())
        prompt_head = ' '.join(SESSION_PROMPT.split())
        is_echo = (
            normalized_title.startswith(prompt_head[:40])
            or prompt_head[:60] in normalized_title
        )
        if not is_echo:
            return title
    return session.get('sessionId', 'untitled') or 'untitled'


def session_row(session: dict, current: str | None) -> tuple[str, str, bool]:
    """Return ``(title, detail, is_current)`` for a session picker row."""
    title = display_session_title(session)
    sid = session.get('sessionId', '')
    updated = format_local_time(session.get('updatedAt'))
    detail = sid if len(sid) <= 32 else f'…{sid[-16:]}'
    if updated:
        detail = f'{updated} · {detail}'
    return title, detail, bool(sid) and sid == current


def reopen_daemon_input_panel(cmd, model, timeout, session_prompt, session_id, agent_name, daemon_window=None, env=None, auth=None, daemon_window_id=None):
    """Re-open the prompt input panel so the user can continue chatting.

    The panel always opens in the daemon-owning window resolved from
    *daemon_window_id* (preferred), *daemon_window* (a window or id), or
    the active window only when no owner was given. Never falls back to
    another window when the owner is known but closed.
    """
    owner_id = None
    if isinstance(daemon_window_id, int):
        owner_id = daemon_window_id
    elif isinstance(daemon_window, int):
        owner_id = daemon_window
    elif daemon_window is None and daemon_window_id is None:
        active = sublime.active_window()
        if active is None:
            return
        try:
            owner_id = active.id()
        except Exception:
            return
    elif daemon_window is not None and daemon_window is not False:
        try:
            owner_id = daemon_window.id()
        except Exception:
            return
    else:
        return  # owning view/window gone - never fall back to another window
    if owner_id is None:
        return
    window = window_by_id(owner_id)
    if window is None:
        return  # owning window closed - never fall back to another window
    window.run_command('acp_input', {
        'cmd': cmd,
        'model': model,
        'env': env or {},
        'timeout': timeout,
        'session_prompt': session_prompt,
        'session_id': session_id,
        'use_daemon': True,
        'auth': auth,
        'daemon_window_id': owner_id,
    })
    broadcast.set_broadcast_status(
        STATUS_KEY_DAEMON, broadcast.daemon_status_text(agent_name, cmd), window
    )
