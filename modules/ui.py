"""UI helpers - output views, status bar, input panel wiring."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import sublime

from . import broadcast
from .config import STATUS_KEY_DAEMON, settings


def on_main(fn: Callable[[], Any], delay: int = 0) -> None:
    """Schedule *fn* to run on Sublime Text's main thread.

    All ``sublime`` API calls made from background threads must be dispatched
    to the main thread. This is a thin wrapper over ``sublime.set_timeout``.

    Args:
        fn: Zero-argument callable to run on the main thread.
        delay: Delay in milliseconds. Defaults to 0 (run as soon as possible).
    """
    sublime.set_timeout(fn, delay)


def create_output_view(window: sublime.Window, agent_name: str = '',
                       role: str = 'prompt') -> sublime.View:
    """Create a scratch buffer output view for ACP responses.

    The view is configured with Markdown syntax, word wrap enabled, and
    line numbers hidden.

    Args:
        window: The Sublime window to create the view in.
        agent_name: Optional agent name to include in the view title.
        role: ``'prompt'`` for one-shot prompts, ``'daemon'`` for persistent sessions.

    Returns:
        The newly created ``sublime.View``.
    """
    suffix = f': {agent_name}' if agent_name else ''
    label = 'ACP Chat' if role == 'daemon' else 'ACP Prompt'
    view = window.new_file()
    view.set_scratch(True)
    view.set_name(f'{label}{suffix}')
    view.assign_syntax('Packages/Markdown/Markdown.sublime-syntax')
    vs = view.settings()
    vs.set('word_wrap', True)
    vs.set('line_numbers', False)
    return view


def resolve_work_dir(window: sublime.Window, source_view: sublime.View) -> str:
    """Resolve the working directory for an ACP session.

    Prefers ``project_path``, falls back to ``folder``, then the source
    view's parent directory, then the first window folder, and finally ``'.'``.

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
    return str(work_dir) if work_dir is not None else '.'


def attach_selection_to_prompt(prompt: str, source_view: sublime.View,
                               force: bool = False) -> str:
    """Append the current selection or cursor context to the prompt.

    No-op when ``attach_selection`` is disabled in settings unless *force*
    is set. With *force* (quick-action prompts), always appends the raw
    selected text in a code fence. Otherwise, if ``source_view`` has a
    non-empty selection and a file name, appends ``@path:line-start-line-end``;
    if no file name, appends the selected text in a code fence.

    Args:
        prompt: The original prompt text.
        source_view: The view whose selection to attach.
        force: Bypass the ``attach_selection`` setting and embed the raw
            selection text.

    Returns:
        The augmented prompt string.
    """
    if source_view is None:
        return prompt
    if not force and not settings().get('attach_selection', False):
        return prompt
    region = source_view.sel()[0]
    if region.empty():
        return prompt
    if force:
        selection = source_view.substr(region)
        return f'{prompt}\n```\n{selection}\n```'
    if path := source_view.file_name():
        start = source_view.rowcol(region.begin())[0] + 1
        end = source_view.rowcol(region.end())[0] + 1
        return f'{prompt} @{path}:{start}-{end}'
    else:
        selection = source_view.substr(region)
        return f'{prompt}\n```\n{selection}\n```'


def open_split_for_output(window: sublime.Window, output_view: sublime.View) -> None:
    """Rearrange the window layout so the output view sits in a 2-column split."""
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

    When *state* is provided (daemon mode), the callback also tracks
    ``has_replied`` and focuses the view on the first reply. All view
    mutations run on the main thread via ``sublime.set_timeout``.

    Args:
        output_view: The view to append streamed chunks to.
        state: Optional ``DaemonState`` for daemon-mode extras.

    Returns:
        A callable ``(text: str) -> None`` suitable as ``rpc.acp``'s callback.
    """
    from .daemon import DaemonState

    def _on_chunk(text: str) -> None:
        if state is not None:
            has_replied = state.get('has_replied')
            state.set(has_replied=True)
            if not has_replied:
                def _focus():
                    ov = state.get('output_view')
                    if ov and ov.window():
                        group, _ = ov.window().get_view_index(ov)
                        ov.window().focus_group(group)
                        ov.window().focus_view(ov)
                on_main(_focus)
        on_main(lambda t=text: _append_text(output_view, t))
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


def append_prompt_turn(view: sublime.View, prompt: str,
                       source_view: sublime.View | None = None,
                       force_selection: bool = False) -> None:
    """Append the user's prompt as a blockquote separator before the response."""
    block = f'\n\n> **User**: {prompt}\n\n'
    append_to_output_view(view, block)
    if source_view is not None:
        sel_display = attach_selection_to_prompt('', source_view,
                                                 force=force_selection)
        if sel_display:
            sel_display = sel_display.lstrip()
            append_to_output_view(view, f'{sel_display}\n\n---\n\n')


def reopen_daemon_input_panel(cmd, model, timeout, system_prompt, session_id, agent_name, daemon_window=None, env=None, auth=None):
    """Re-open the prompt input panel so the user can continue chatting."""
    if window := daemon_window or sublime.active_window():
        window.run_command('acp_input', {
            'cmd': cmd,
            'model': model,
            'env': env or {},
            'timeout': timeout,
            'system_prompt': system_prompt,
            'session_id': session_id,
            'use_daemon': True,
            'auth': auth,
        })
        broadcast.set_broadcast_status(
            STATUS_KEY_DAEMON, broadcast.daemon_status_text(agent_name, cmd), window
        )
