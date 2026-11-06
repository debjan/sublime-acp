"""Project file enumeration with gitignore rules."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..protocol import acp_log
from . import gitignore, ui
from .config import CACHE_TTL_DEFAULT


def walk_project_files(folders, all_ignored_dirs, all_ignored_ext, allow_rules):
    """Walk project folders and return relative file paths.

    Pure function with no UI or thread dependencies; safe to run on a
    background thread. For each folder the per-project ``.gitignore`` rules
    are applied together with the global ignore sets, and directories/files
    are pruned accordingly. Paths matching *allow_rules* are kept even when
    they would otherwise be ignored.

    Args:
        folders: Iterable of absolute folder paths to walk.
        all_ignored_dirs: Set of directory names to always skip.
        all_ignored_ext: Set of file extensions to always skip.
        allow_rules: :class:`gitignore.AllowRules` re-including ignored paths.

    Returns:
        List of file paths relative to their containing project folder.
    """
    project_files = []
    for folder in folders:
        rules = gitignore.parse_gitignore(folder)

        for root, dirs, files in os.walk(folder):
            rel_root = gitignore.norm_path(os.path.relpath(root, folder))
            if rel_root == '.':
                rel_root = ''

            dirs[:] = [
                d for d in dirs
                if allow_rules.matches(
                    norm_dir := gitignore.norm_path(os.path.join(rel_root, d)),
                    is_dir=True,
                )
                or (
                    not d.startswith('.')
                    and d not in all_ignored_dirs
                    and not rules.is_path_ignored(norm_dir, is_dir=True)
                )
            ]

            for f in files:
                full_path = os.path.join(root, f)
                rel_path = os.path.relpath(full_path, folder)
                norm_rel = gitignore.norm_path(rel_path)
                if not is_file_ignored(f, norm_rel, all_ignored_ext, rules, allow_rules):
                    project_files.append(norm_rel)
    return project_files


def is_file_ignored(f, rel_path, settings_ext, rules, allow_rules):
    """Check if a single file should be skipped from the project file list.

    Args:
        f: The file name (basename).
        rel_path: Normalized relative path for gitignore pattern matching.
        settings_ext: Set of file extensions to always ignore.
        rules: :class:`gitignore.GitignoreRules` for the project.
        allow_rules: :class:`gitignore.AllowRules` re-including ignored paths.

    Returns:
        ``True`` if the file should be excluded.
    """
    if allow_rules.matches(rel_path, is_dir=False):
        return False
    if f.startswith('.'):
        return True
    ext = Path(f).suffix
    if ext.lower() in settings_ext:
        return True
    return rules.is_path_ignored(rel_path, is_dir=False)


def load_filter_config(settings):
    """Build ignore and allow rules from settings for the file walker.

    Returns:
        Tuple of ``(ignored_dirs, ignored_ext, allow_rules)`` where the first
        two are sets and the last is a :class:`gitignore.AllowRules`.
    """
    ignored_dirs = {'.git', '.svn', '.hg', '.venv', 'node_modules'}
    ignored_ext = {'.pyc', '.pyo', '.pyd', '.so', '.dll', '.exe', '.o', '.a', '.lib', '.class'}

    if ignore := settings.get('ignore', None):
        folders = ignore.get('folders')
        if folders is not None:
            ignored_dirs = set(folders)
        extensions = ignore.get('extensions')
        if extensions is not None:
            ignored_ext = set(extensions)

    return ignored_dirs, ignored_ext, gitignore.parse_allow_config(settings.get('allow'))


def get_window_folders(window):
    """Get project folders, falling back to the active view's directory.

    Args:
        window: A Sublime ``Window`` instance.

    Returns:
        List of folder paths to scan for project files.
    """
    if not (folders := window.folders()):
        view = window.active_view()
        if view and view.file_name():
            folders = [os.path.dirname(view.file_name())]
    return folders


@dataclass
class CacheEntry:
    """A cached project file listing with staleness tracking.

    Parameters:
        files: List of relative file paths.
        folders: Tuple of project folder paths used to generate the listing.
        ts: Monotonic timestamp of when this entry was created.
    """

    files: list
    folders: tuple
    ts: float

    def is_valid(self, folders_key: tuple, ttl: int) -> bool:
        """Check whether this cache entry is still valid.

        Args:
            folders_key: The current project folder paths to compare against.
            ttl: Maximum age in seconds. ``0`` or negative means no TTL check.

        Returns:
            ``True`` if the entry matches the given folders and is within TTL.
        """
        return self.folders == folders_key and (ttl <= 0 or time.monotonic() - self.ts <= ttl)


class ProjectFileCache:
    """Project file cache with thread-safe access.

    Manages cached file listings keyed by window ID, background refresh
    tracking, and deferred callback registration.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._cache: dict = {}
        self._refresh_in_progress: set = set()
        self._ready_callbacks: dict = {}

    def _do_refresh(self, window_id, folders, all_ignored_dirs, all_ignored_ext, allow_rules):
        """Background thread: walk project dirs and write result back to cache."""
        try:
            project_files = walk_project_files(
                folders, all_ignored_dirs, all_ignored_ext, allow_rules
            )

            def update_cache():
                with self._lock:
                    self._cache[window_id] = CacheEntry(project_files, tuple(folders), time.monotonic())
                    self._refresh_in_progress.discard(window_id)
                    callbacks = self._ready_callbacks.pop(window_id, [])
                for cb in callbacks:
                    cb(project_files)

            ui.on_main(update_cache)
        except Exception as exc:
            acp_log('file_walker', f'background walk failed: {exc!r}')

            def clear_flag():
                with self._lock:
                    self._refresh_in_progress.discard(window_id)
                    self._ready_callbacks.pop(window_id, None)
            ui.on_main(clear_flag)

    def load_project_files_for_window(self, window, settings, cache_ttl=CACHE_TTL_DEFAULT):
        all_ignored_dirs, all_ignored_ext, allow_rules = load_filter_config(settings)

        window_id = window.id()
        folders = get_window_folders(window)
        folders_key = tuple(folders)
        ttl = settings.get('cache_ttl', cache_ttl)

        with self._lock:
            entry = self._cache.get(window_id)
            if entry is not None and entry.is_valid(folders_key, ttl):
                return entry.files

            stale_files = entry.files if entry else []

            if window_id not in self._refresh_in_progress:
                self._refresh_in_progress.add(window_id)
                t = threading.Thread(
                    target=self._do_refresh,
                    args=(window_id, folders, all_ignored_dirs, all_ignored_ext, allow_rules),
                    daemon=True,
                )
                t.start()

        return stale_files

    def expire_cache_for_window(self, window_id):
        with self._lock:
            if entry := self._cache.get(window_id):
                entry.ts = 0

    def on_cache_ready(self, window_id, callback):
        with self._lock:
            if window_id not in self._ready_callbacks:
                self._ready_callbacks[window_id] = []
            self._ready_callbacks[window_id].append(callback)

    def is_refresh_pending(self, window_id):
        with self._lock:
            return window_id in self._refresh_in_progress

    def clear_all(self):
        with self._lock:
            self._cache.clear()
            self._refresh_in_progress.clear()
            self._ready_callbacks.clear()


_project_file_cache = ProjectFileCache()

load_project_files_for_window = _project_file_cache.load_project_files_for_window
expire_cache_for_window = _project_file_cache.expire_cache_for_window
on_cache_ready = _project_file_cache.on_cache_ready
is_refresh_pending = _project_file_cache.is_refresh_pending
clear_all_caches = _project_file_cache.clear_all
