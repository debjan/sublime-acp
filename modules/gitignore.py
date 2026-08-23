"""Gitignore parsing and pattern matching."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_regex_cache = {}


@dataclass(frozen=True)
class GitignoreRules:
    """Compiled ignore rules from a project root ``.gitignore`` file.

    Attributes:
        ignored_dirs: Exact directory names skipped quickly during walks.
        ignored_ext: File extensions skipped quickly.
        patterns: Ordered ``(pattern, is_dir_only, is_negate)`` tuples.
    """

    ignored_dirs: frozenset[str]
    ignored_ext: frozenset[str]
    patterns: tuple[tuple[str, bool, bool], ...]

    @property
    def has_negation(self) -> bool:
        """Return whether any pattern re-includes previously ignored paths."""
        return any(is_negate for _, _, is_negate in self.patterns)

    def is_path_ignored(self, relative_path: str, *, is_dir: bool = False) -> bool:
        """Check if a relative path is ignored.

        Walks each path segment so a pattern matching a leading directory
        also excludes its contents. Patterns are evaluated in order and the
        last match wins, so a negation (``!``) re-includes a path. The
        fast-skip sets are only consulted when no negation exists."""

        path = relative_path.replace('\\', '/')
        parts = path.split('/')
        use_fast = not self.has_negation
        ignored = False
        for index, part in enumerate(parts):
            segment_path = '/'.join(parts[:index + 1])
            segment_is_dir = index < len(parts) - 1 or is_dir
            if use_fast and segment_is_dir and part in self.ignored_dirs:
                return True
            if use_fast and not segment_is_dir and any(
                part.lower().endswith(ext) for ext in self.ignored_ext
            ):
                return True
            result = self._segment_ignored(segment_path, segment_is_dir)
            if result is not None:
                ignored = result
        return ignored

    def _segment_ignored(self, segment_path: str, segment_is_dir: bool) -> bool | None:
        """Return the ignore state for one path segment, or ``None`` if no pattern matches.

        The last matching pattern wins, so a negation (``!``) re-includes a
        segment (returns ``False``). A ``None`` result means the caller must
        keep its previous state, because an earlier segment may have set it
        and a deeper segment must be able to overwrite it.
        """
        result = None
        for pattern, dir_only, is_negate in self.patterns:
            if dir_only and not segment_is_dir:
                continue
            if _match_gitignore_pattern(pattern, segment_path):
                result = not is_negate
        return result


def read_gitignore_lines(gitignore_path):
    """Read ``.gitignore`` lines, returning an empty list on error."""
    try:
        with open(gitignore_path, 'r', encoding='utf-8') as f:
            return f.readlines()
    except Exception:
        return []


def classify_gitignore_line(raw_line):
    """Classify a single ``.gitignore`` line into a category.

    Skips blank lines and comments. A leading ``!`` marks a negation
    (re-include) pattern. Unescapes a leading ``\\#`` or ``\\!`` and strips
    a leading ``/``. Trailing ``/`` marks the pattern as directory-only.

    Args:
        raw_line: A single raw line from a ``.gitignore`` file.

    Returns:
        ``None`` to skip the line, or ``('pattern', pattern_str,
        is_dir_only)`` / ``('negate', pattern_str, is_dir_only)``.
    """
    line = raw_line.rstrip('\n\r')
    if not line:
        return None

    if is_negate := line.startswith('!'):
        line = line[1:]
    elif line.startswith('#'):
        return None

    if line.startswith(('\\#', '\\!')):
        line = line[1:]

    line = line.rstrip()
    if not line:
        return None

    if line.startswith('/'):
        line = line[1:]

    if is_dir_only := line.endswith('/'):
        line = line[:-1]

    return ('negate' if is_negate else 'pattern', line, is_dir_only)


def parse_gitignore(project_root):
    """Parse ``.gitignore`` in *project_root*.

    Splits patterns into fast-skip sets (exact directory names and file
    extensions) plus an ordered list of patterns for finer checks.

    Args:
        project_root: Path to the project directory containing ``.gitignore``.

    Returns:
        A :class:`GitignoreRules` instance, with empty rules when no
        ``.gitignore`` exists or it cannot be read.
    """
    gitignore_path = Path(project_root) / '.gitignore'
    if not gitignore_path.is_file():
        return GitignoreRules(frozenset(), frozenset(), ())

    lines = read_gitignore_lines(gitignore_path)
    if not lines:
        return GitignoreRules(frozenset(), frozenset(), ())

    ignored_dirs = set()
    ignored_ext = set()
    patterns = []

    for line in lines:
        result = classify_gitignore_line(line)
        if result is None:
            continue
        kind, pattern_str, is_dir_only = result
        patterns.append((pattern_str, is_dir_only, kind == 'negate'))

        if kind != 'negate':
            if is_dir_only and '/' not in pattern_str:
                ignored_dirs.add(pattern_str)
            if not is_dir_only and pattern_str.startswith('*.') and '/' not in pattern_str:
                ignored_ext.add(pattern_str[1:])

    return GitignoreRules(frozenset(ignored_dirs), frozenset(ignored_ext), tuple(patterns))


def norm_path(path):
    """Normalize a path to forward slashes for gitignore pattern matching."""
    return path.replace('\\', '/')


def _translate_segment(seg):
    """Translate one path segment (no ``/``) to a regex fragment.

    ``*`` matches any run of characters within the segment, ``?`` matches a
    single character, and ``[...]`` character classes are preserved.
    """
    out = []
    i = 0
    while i < len(seg):
        ch = seg[i]
        if ch == '*':
            out.append('[^/]*')
        elif ch == '?':
            out.append('[^/]')
        elif ch == '[':
            end = seg.find(']', i + 1)
            if end == -1:
                out.append('\\[')
            else:
                cls = seg[i + 1:end]
                if cls.startswith(('!', '^')):
                    cls = '^' + cls[1:]
                out.append('[' + cls + ']')
                i = end
        elif ch == '\\' and i + 1 < len(seg):
            out.append(re.escape(seg[i + 1]))
            i += 1
        else:
            out.append(re.escape(ch))
        i += 1
    return ''.join(out)


def _translate_pattern(pattern):
    """Translate a gitignore glob pattern into an unanchored regex string.

    ``*`` matches within a path segment, ``?`` matches one character, and
    ``**`` matches zero or more path segments (a leading ``**/`` matches in
    all directories, a trailing ``/**`` matches everything inside, and a
    middle ``**`` matches zero or more directories).
    """
    segments = pattern.split('/')
    regex = ''
    for i, seg in enumerate(segments):
        if i > 0 and segments[i - 1] != '**':
            regex += '/'
        if seg == '**':
            if len(segments) == 1 or i == len(segments) - 1:
                regex += '.*'
            else:
                regex += '(?:[^/]+/)*'
        else:
            regex += _translate_segment(seg)
    return regex


def _compiled_pattern(pattern):
    """Return a cached compiled regex for a gitignore pattern."""
    rx = _regex_cache.get(pattern)
    if rx is None:
        rx = re.compile(_translate_pattern(pattern))
        _regex_cache[pattern] = rx
    return rx


def _match_gitignore_pattern(pattern, path):
    """Check if a path matches a gitignore pattern.

    Patterns without ``/`` match the basename at any depth; patterns with
    ``/`` (or ``**``) match the full path."""
    rx = _compiled_pattern(pattern)
    if '/' not in pattern:
        return rx.fullmatch(path.split('/')[-1]) is not None
    return rx.fullmatch(path) is not None
