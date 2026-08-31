"""File and slash-command auto-completions for the ACP input panel."""

import sublime
import sublime_plugin

from . import broadcast, file_walker
from .config import INPUT_VIEW_NAME, MAX_HINT_LENGTH, STATUS_KEY_DAEMON, settings
from .daemon import _stop_daemon_async
from .daemon import get_state as _get_state

_INHIBIT = sublime.INHIBIT_WORD_COMPLETIONS | sublime.INHIBIT_EXPLICIT_COMPLETIONS


def _find_trigger_and_prefix(view, pt, prefix):
    """Scan backwards from the prefix start to find a ``/`` or ``@`` trigger.

    Sublime Text's ``on_query_completions`` breaks the ``prefix`` at word
    separators (``:``, ``/``, etc.), so we must scan the raw buffer to find
    the trigger character and reconstruct the full prefix text.

    Returns:
        ``(trigger_char, full_prefix)`` where *full_prefix* is the text from
        the trigger (exclusive) to the cursor, or ``(None, None)`` when no
        trigger is found.
    """
    start = pt - len(prefix)
    if start <= 0:
        return (None, None)

    pos = start
    while pos > 0:
        ch = view.substr(sublime.Region(pos - 1, pos))
        if ch == '@':
            return ('@', view.substr(sublime.Region(pos, pt)))
        if ch in ('/', '\\'):
            pos -= 1
            continue
        if ch in (' ', '\t', '\n', '\r'):
            break
        pos -= 1

    # Re-scan for '/' at word boundary
    pos = start
    while pos > 0:
        ch = view.substr(sublime.Region(pos - 1, pos))
        if ch == '/':
            return ('/', view.substr(sublime.Region(pos, pt)))
        if ch in (' ', '\t', '\n', '\r', '@', '\\'):
            break
        pos -= 1

    return (None, None)


def _build_completions(project_files, prefix):
    """Filter *project_files* by *prefix* and return ``CompletionItem`` list."""
    if prefix:
        partial_lower = prefix.replace('\\', '/').lower()
        files = [f for f in project_files if partial_lower in f.replace('\\', '/').lower()]
    else:
        files = project_files
    items = []
    for f in files:
        try:
            kind = sublime.KIND_NAVIGATION
        except AttributeError:
            kind = (0, '', '')
        items.append(sublime.CompletionItem(
            trigger=f,
            annotation='file',
            completion=f,
            kind=kind,
        ))
    return items


def complete_slash_commands(view, prefix):
    """Build slash-command completions for the input panel.

    Reads the ``acp_slash_commands`` view setting (populated from the agent's
    cached commands) and filters by *prefix*, returning Sublime completion
    tuples with appropriate ``INHIBIT`` flags.

    Args:
        view: The input panel view requesting completions.
        prefix: The current word prefix being completed.

    Returns:
        ``(completions, flags)`` with ``INHIBIT`` flags, or
        ``([], INHIBIT_WORD_COMPLETIONS | INHIBIT_EXPLICIT_COMPLETIONS)``
        when no slash commands match or none are configured.
    """
    slash_commands = view.settings().get('acp_slash_commands') or []
    if not slash_commands:
        return ([], _INHIBIT)

    completions = []
    for c in slash_commands:
        name = c.get('name')
        if not name:
            continue
        if prefix and not name.lower().startswith(prefix.lower()):
            continue
        hint = c.get('description') or 'command'
        hint = ' '.join(hint.split())
        if len(hint) > MAX_HINT_LENGTH:
            hint = hint[:MAX_HINT_LENGTH - 3] + '...'
        completions.append((f'/{name}\t{hint}', f'/{name}'))

    if not completions:
        return ([], _INHIBIT)
    return (completions, _INHIBIT)


def complete_at_files(window, prefix, settings):
    """Build ``@``-file completions for the input panel.

    Loads cached project files for *window* (kicking off a background refresh on
    a miss) and filters them by *prefix*. When the cache is empty and a refresh
    is in flight, returns a ``CompletionList`` populated asynchronously.

    Args:
        window: The Sublime window whose project files should be offered.
        prefix: The current word prefix being completed.
        settings: Settings object controlling ignore rules and cache TTL.

    Returns:
        ``(completions, flags)`` with ``INHIBIT`` flags, a ``CompletionList``
        populated asynchronously when a refresh is pending, or ``None`` when no
        files are available to defer to default completion behavior.
    """
    if not window:
        return None

    if project_files := file_walker.load_project_files_for_window(window, settings):
        completions = _build_completions(project_files, prefix)
        if not completions:
            return None
        return (completions, _INHIBIT)

    # No cached files yet - if a background refresh is in flight, return a
    # CompletionList that will be populated when the walk finishes.
    if file_walker.is_refresh_pending(window.id()):
        completion_list = sublime.CompletionList(flags=_INHIBIT)

        def on_ready(files):
            if not files:
                completion_list.set_completions([])
                return
            items = _build_completions(files, prefix)
            completion_list.set_completions(items)

        file_walker.on_cache_ready(window.id(), on_ready)
        return completion_list

    return None


class AcpFileCompletionListener(sublime_plugin.EventListener):
    """Provides file path completions when typing @ in the ACP input panel."""

    def on_post_save_async(self, view):
        """Expire the project file cache when a file is saved."""
        if win := view.window():
            file_walker.expire_cache_for_window(win.id())

    def on_activated(self, view):
        """Re-apply daemon status when a view in a daemon's window is activated."""
        if view.settings().get('is_widget'):
            return
        win = view.window()
        if win is None:
            return
        state = _get_state(win.id())
        if state is None or not state.is_running():
            return
        agent_name = state.get('agent_name') or 'agent'
        if state.get('is_busy'):
            view.set_status(STATUS_KEY_DAEMON, 'ACP: processing...')
        else:
            view.set_status(STATUS_KEY_DAEMON, broadcast.daemon_status_text(agent_name, state.get('agent_cmd')))

    def on_pre_close_window(self, window):
        """Auto-stop the daemon when the window that started it is closed."""
        window_id = window.id()
        state = _get_state(window_id)
        if state is None or not state.is_running():
            return

        agent_name = state.get('agent_name') or 'unknown'
        _stop_daemon_async(
            window_id,
            on_done=lambda: sublime.status_message(
                f'Agent "{agent_name}" stopped (window closed)'
            ),
        )

    def on_query_completions(self, view, prefix, locations):
        """Provide ``@`` file and ``/`` slash-command completions in the ACP input panel.

        Args:
            view: The view requesting completions.
            prefix: The current word prefix being completed.
            locations: List of buffer positions for the completion context.

        Returns:
            A completions tuple, a ``CompletionList``, or ``None`` to defer
            to default Sublime Text completion behavior.
        """
        if view.name() != INPUT_VIEW_NAME:
            return None

        if not locations:
            return None

        pt = locations[0]
        trigger_char, full_prefix = _find_trigger_and_prefix(view, pt, prefix)
        if trigger_char == '/':
            return complete_slash_commands(view, full_prefix)
        if trigger_char == '@':
            return complete_at_files(view.window(), full_prefix, settings())

        return ([], _INHIBIT)
