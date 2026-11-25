"""Git-based turn summaries and optional daemon-turn checkpoints.

Snapshots ``git status`` + ``HEAD`` + per-file content hashes before each
prompt and renders a markdown summary afterwards, so only the current
turn's changes are reported even when the tree was already dirty. Optional
checkpoint refs preserve worktree and index state, including new files.
Non-git workdirs are a silent no-op.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from itertools import islice
from typing import Any

MAX_FILES = 10
MAX_LINES_PER_FILE = 100
MAX_TOTAL_CHARS = 12000
MAX_SNAPSHOT_FILES = 100
MAX_SNAPSHOT_BYTES = 200_000

GIT_SUMMARY_OFF = 'off'
GIT_SUMMARY_COUNTS = 'counts'
GIT_SUMMARY_DIFF = 'diff'
GIT_CHECKPOINT_RETENTION = 100


def resolve_mode(value: Any) -> str:
    """Map the ``git_turn_summary`` setting to ``'off'``/``'counts'``/``'diff'``.

    ``True`` means ``'counts'`` (the default), ``False``/``None``/``0`` means
    ``'off'``; unknown values fall back to ``'counts'``.
    """
    if not value:
        return GIT_SUMMARY_OFF
    if value is True:
        return GIT_SUMMARY_COUNTS
    if value == GIT_SUMMARY_COUNTS:
        return GIT_SUMMARY_COUNTS
    if value == GIT_SUMMARY_DIFF:
        return GIT_SUMMARY_DIFF
    return GIT_SUMMARY_COUNTS


GIT_TIMEOUT_QUICK = 10
GIT_TIMEOUT_DIFF = 30


def _run_git(
    work_dir: str,
    *args: str,
    timeout: int = GIT_TIMEOUT_QUICK,
    raise_on_error: bool = False,
    env: dict[str, str] | None = None,
) -> str | None:
    """Run Git in *work_dir*, returning stdout or ``None`` on best-effort failure.

    ``raise_on_error`` is used for checkpoint writes, which must not fail
    silently. Best-effort calls disable optional locks and discard stderr.
    """
    try:
        hide_kwargs: dict[str, Any] = {}
        if sys.platform == 'win32':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            hide_kwargs['startupinfo'] = startupinfo
        command = ['git', '-C', work_dir]
        if not raise_on_error:
            command.append('--no-optional-locks')
        command.extend(args)
        proc = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if raise_on_error else subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            env=env,
            **hide_kwargs,
        )
    except Exception as exc:
        if raise_on_error:
            raise RuntimeError(f'git {args[0]} failed: {exc}') from exc
        return None
    if proc.returncode != 0:
        if raise_on_error:
            detail = proc.stderr.decode('utf-8', errors='replace').strip()
            raise RuntimeError(f'git {args[0]} failed: {detail or proc.returncode}')
        return None
    try:
        return proc.stdout.decode('utf-8', errors='replace')
    except Exception as exc:
        if raise_on_error:
            raise RuntimeError(f'git {args[0]} failed: {exc}') from exc
        return None


def _run_git_required(
    work_dir: str,
    *args: str,
    timeout: int = GIT_TIMEOUT_QUICK,
    env: dict[str, str] | None = None,
) -> str:
    """Run Git and require successful textual output."""
    output = _run_git(
        work_dir, *args, timeout=timeout, raise_on_error=True, env=env,
    )
    if output is None:
        raise RuntimeError(f'git {args[0]} returned no output')
    return output


def _sanitize_ref_part(value: str | None, fallback: str = 'unknown') -> str:
    """Normalize *value* into a safe Git refname component fragment."""
    cleaned = re.sub(r'[^A-Za-z0-9._-]+', '-', (value or '').strip()).strip('.-')
    cleaned = re.sub(r'\.{2,}', '.', cleaned).lower()
    if cleaned.endswith('.lock'):
        cleaned = f'{cleaned[:-5]}-lock'.strip('.-')
    return cleaned or fallback


def _new_checkpoint_paths(status: str) -> list[str]:
    """Extract untracked (``??``) and intent-to-add (`` A``) paths from ``status -z``."""
    paths: list[str] = []
    tokens = iter(status.split('\0'))
    for entry in tokens:
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        if xy[0] in ('R', 'C') or xy[1] in ('R', 'C'):
            next(tokens, None)
        if xy in ('??', ' A'):
            paths.append(path)
    return paths


def create_checkpoint(work_dir: str, session_id: str | None, agent_name: str) -> str | None:
    """Save worktree and index changes under a retained ACP checkpoint ref.

    Git's stash object keeps staged and unstaged state without changing
    ``HEAD``, the real index, or the worktree. New non-ignored files are
    included using a temporary index.

    Args:
        work_dir: Working directory inside or at the Git repository.
        session_id: The ACP session ID for checkpoint naming.
        agent_name: The agent name for checkpoint naming.

    Returns:
        The created ref name, or ``None`` when the workdir is not a Git repo
        or has no tracked or new non-ignored changes.
    """
    top_level = _run_git(work_dir, 'rev-parse', '--show-toplevel')
    if top_level is None:
        return None
    if not (repo_root := top_level.strip()):
        return None
    if _run_git(repo_root, 'rev-parse', '--verify', 'HEAD') is None:
        return None

    index_path = _run_git_required(repo_root, 'rev-parse', '--git-path', 'index').strip()
    if not os.path.isabs(index_path):
        index_path = os.path.abspath(os.path.join(repo_root, index_path))

    fd, temp_index = tempfile.mkstemp(
        prefix='acp-checkpoint-', suffix='.index', dir=os.path.dirname(index_path),
    )
    os.close(fd)
    try:
        if os.path.exists(index_path):
            shutil.copyfile(index_path, temp_index)
        else:
            # Remove the 0-byte placeholder so Git initializes a fresh index rather
            # than rejecting it with "index file smaller than expected".
            os.unlink(temp_index)
        env = os.environ.copy()
        env['GIT_INDEX_FILE'] = temp_index

        status = _run_git_required(repo_root, 'status', '--porcelain=v1', '-z', '--untracked-files=all')
        if new_paths := _new_checkpoint_paths(status):
            _run_git_required(repo_root, 'add', '-f', '-A', '--', *new_paths, env=env)

        checkpoint = _run_git_required(repo_root, 'stash', 'create', env=env).strip()
        if not checkpoint:
            return None

        safe_agent = _sanitize_ref_part(agent_name, 'agent')
        safe_session = _sanitize_ref_part(session_id, 'session')
        ref = f'refs/acp/checkpoints/{time.time_ns()}-{safe_agent}-{safe_session}'
        _run_git_required(repo_root, 'update-ref', ref, checkpoint, '0' * len(checkpoint))

        refs_out = _run_git_required(
            repo_root, 'for-each-ref', '--format=%(refname)', '--sort=-refname', 'refs/acp/checkpoints',
        )
        old_refs = refs_out.splitlines()[GIT_CHECKPOINT_RETENTION:]
        for old_ref in old_refs:
            _run_git_required(repo_root, 'update-ref', '-d', old_ref)
        return ref
    finally:
        if os.path.exists(temp_index):
            os.unlink(temp_index)


def _parse_status(output: str) -> dict[str, str]:
    """Parse ``git status --porcelain`` output into ``{path: XY}``."""
    entries: dict[str, str] = {}
    for line in output.splitlines():
        if len(line) < 4:
            continue
        xy, path = line[:2], line[3:]
        if ' -> ' in path:
            path = path.rsplit(' -> ', 1)[-1]
        if path := path.strip().strip('"'):
            entries[path] = xy
    return entries


def _read_worktree(
    work_dir: str,
    path: str,
    read_content: bool = True,
) -> tuple[str | None, str | None, bool]:
    """Fingerprint the worktree file at *path*.

    Returns ``(digest, text, binary)``; *digest* is ``None`` when the file
    is missing, *text* is ``None`` for binary, oversized, or hash-only
    reads (change still detectable via *digest*).
    """
    try:
        full = os.path.join(work_dir, *path.split('/'))
        digest_obj = hashlib.sha1()
        prefix = bytearray()
        oversized = False
        with open(full, 'rb') as f:
            for block in iter(lambda: f.read(65536), b''):
                digest_obj.update(block)
                if not read_content:
                    continue
                if len(prefix) < MAX_SNAPSHOT_BYTES:
                    take = min(len(block), MAX_SNAPSHOT_BYTES - len(prefix))
                    prefix += block[:take]
                else:
                    oversized = True
        digest = digest_obj.hexdigest()
    except Exception:
        return None, None, False
    if not read_content:
        return digest, None, False
    raw = bytes(prefix)
    if b'\0' in raw[:8192]:
        return digest, None, True
    if oversized:
        return digest, None, False
    try:
        return digest, raw.decode('utf-8', errors='replace'), False
    except Exception:
        return digest, None, False


def snapshot(work_dir: str, include_content: bool = True) -> dict[str, Any] | None:
    """Capture git ``HEAD`` + status + file fingerprints; ``None`` when not a repo.

    Content blobs let :func:`summarize` isolate the turn's own delta on
    files that were already dirty, and skip files the turn didn't touch.
    Pass ``include_content=False`` when only change detection is needed.
    """
    head = _run_git(work_dir, 'rev-parse', 'HEAD')
    status = _run_git(work_dir, 'status', '--porcelain=v1', '--untracked-files=all')
    if head is None or status is None:
        return None
    entries = _parse_status(status)
    blobs: dict[str, dict[str, Any]] = {}
    for path in list(entries)[:MAX_SNAPSHOT_FILES]:
        digest, text, binary = _read_worktree(work_dir, path, read_content=include_content)
        blobs[path] = {'hash': digest, 'content': text, 'binary': binary}
    return {'head': head.strip(), 'status': entries, 'blobs': blobs}


def _split_diff(diff_out: str) -> dict[str, str]:
    """Split full ``git diff`` output into ``{path: patch}`` per file."""
    chunks: dict[str, list[str]] = {}
    current: str | None = None
    for line in diff_out.splitlines():
        if line.startswith('diff --git '):
            parts = line.split(' b/', 1)
            current = parts[1] if len(parts) == 2 else None
            if current is not None:
                chunks.setdefault(current, [])
        if current is not None:
            chunks[current].append(line)
    return {path: '\n'.join(lines) for path, lines in chunks.items()}


def _truncate(text: str, max_lines: int) -> tuple[str, bool]:
    """Cap *text* to *max_lines*; return ``(text, truncated)``."""
    lines = text.splitlines()
    if len(lines) > max_lines:
        return '\n'.join(lines[:max_lines]), True
    return text, False


def _turn_patch(old: str, new: str, path: str) -> str:
    """Unified diff of turn-start content vs current content for *path*."""
    return '\n'.join(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=f'a/{path}', tofile=f'b/{path}',
    ))


def _stat_of(patch: str) -> str:
    """Count ``+/-`` lines in *patch* (excluding ``+++/---`` headers)."""
    add = sum(1 for ln in patch.splitlines() if ln.startswith('+') and not ln.startswith('+++'))
    delete = sum(1 for ln in patch.splitlines() if ln.startswith('-') and not ln.startswith('---'))
    bits = []
    if add:
        bits.append(f'{add} insertion{"s" if add != 1 else ""}(+)')
    if delete:
        bits.append(f'{delete} deletion{"s" if delete != 1 else ""}(-)')
    return ', '.join(bits)


def _new_file_patch(work_dir: str, path: str) -> str:
    """Render a file created during the turn as an all-additions patch."""
    try:
        full = os.path.join(work_dir, *path.split('/'))
        with open(full, 'r', encoding='utf-8', errors='replace') as f:
            content = ''.join(islice(f, MAX_LINES_PER_FILE + 1))
    except Exception:
        return ''
    body, cut = _truncate(content, MAX_LINES_PER_FILE)
    lines = ['--- /dev/null', f'+++ b/{path}']
    lines.extend('+' + ln for ln in body.splitlines())
    if cut:
        lines.append('... (truncated)')
    return '\n'.join(lines)


def _line_count(work_dir: str, path: str) -> int | None:
    """Count lines of the worktree file at *path*; ``None`` when unreadable."""
    try:
        full = os.path.join(work_dir, *path.split('/'))
        with open(full, 'r', encoding='utf-8', errors='replace') as f:
            return sum(1 for _ in f)
    except Exception:
        return None


def _new_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Paths created during the turn (untracked ``??`` or staged-new ``A``).

    Porcelain status omits clean files, but that needs no tracked-files set
    to resolve: a clean-tracked file dirtied mid-turn surfaces as ``M``,
    never ``??``/``A`` - and unlike ``ls-files`` output, status codes are
    immune to quotepath key mismatches on non-ASCII paths.
    """
    return sorted(
        p for p in after
        if p not in before and ('?' in after[p] or 'A' in after[p])
    )


def _detect_turn_entries(
    base: dict[str, Any] | None,
    work_dir: str,
    read_content: bool = True,
) -> tuple[list[tuple[str, str]], dict[str, tuple[str | None, str | None, bool]]] | None:
    """Return ``(entries, current_blobs)`` changed since *base*, or ``None``."""
    if base is None:
        return None
    status_out = _run_git(work_dir, 'status', '--porcelain=v1', '--untracked-files=all')
    if status_out is None:
        return None
    before: dict[str, str] = base['status']
    blobs: dict[str, dict[str, Any]] = base.get('blobs') or {}
    after = _parse_status(status_out)
    # Fingerprint only files dirty at turn start (clean-start paths use git diff)
    current: dict[str, tuple[str | None, str | None, bool]] = {
        p: _read_worktree(work_dir, p, read_content=read_content) for p in before
    }

    def _changed(path: str) -> bool:
        """True when *path*'s content differs from the turn-start snapshot."""
        snap = blobs.get(path, {}).get('hash')
        cur = current.get(path, (None, None, False))[0]
        if snap is None or cur is None:
            if snap == cur:
                return after.get(path) != before.get(path)
            return True
        return cur != snap

    new_paths = _new_paths(before, after)
    new_set = set(new_paths)
    # Entries: (path, kind) where kind selects patch source.
    # 'created' = appeared during turn; 'clean' = clean at start;
    # 'dirty' = dirty at start and changed; 'removed'/'reverted' = gone now.
    entries: list[tuple[str, str]] = [(p, 'created') for p in new_paths]
    for p in after:
        if p in new_set:
            continue
        if p not in before:
            entries.append((p, 'clean'))  # clean at start, dirtied by turn
        elif _changed(p):
            entries.append((p, 'dirty'))
    for p in before:
        if p not in after and _changed(p):
            digest = current[p][0]
            entries.append((p, 'reverted' if digest is not None else 'removed'))

    if not entries:
        return None
    return entries, current


def has_turn_changes(base: dict[str, Any] | None, work_dir: str) -> bool:
    """Return ``True`` when the worktree changed relative to *base*."""
    return _detect_turn_entries(base, work_dir, read_content=False) is not None


def summarize(
    base: dict[str, Any] | None,
    work_dir: str,
    include_diff: bool = True,
) -> str | None:
    """Report only the turn's own changes vs *base*; ``None`` when no change.

    Args:
        base: Snapshot from :func:`snapshot` taken before the turn.
        work_dir: Working directory of the git repo.
        include_diff: When ``False``, only the "Changed files" filename list
            is rendered and per-file ``diff`` fences are omitted.
    """
    if base is None:
        return None
    detected = _detect_turn_entries(base, work_dir, read_content=True)
    if detected is None:
        return None
    entries, current = detected
    blobs: dict[str, dict[str, Any]] = base.get('blobs') or {}
    head_out = _run_git(work_dir, 'rev-parse', 'HEAD')
    head = head_out.strip() if head_out else base['head']
    head_moved = head != base['head']

    # One batched git diff covers files that were clean at turn start
    # (worktree-vs-HEAD there equals the turn's own delta). Scoped to
    # those paths so repo-wide dirt is never generated just to be discarded.
    clean_paths = sorted(p for p, kind in entries if kind == 'clean')
    git_patches: dict[str, str] = {}
    if clean_paths:
        diff_out = _run_git(
            work_dir, 'diff', '--no-color', '--no-ext-diff', base['head'],
            '--', *clean_paths, timeout=GIT_TIMEOUT_DIFF,
        )
        if diff_out:
            git_patches = _split_diff(diff_out)

    # Resolve (label, patch) per entry; patch None means label-only.
    resolved: list[tuple[str, str, str | None]] = []
    for path, kind in entries:
        if kind == 'created':
            count = _line_count(work_dir, path)
            label = f'[{path}]({path}) (new{(f", {count} lines" if count is not None else "")})'
            resolved.append((path, label, _new_file_patch(work_dir, path) or None))
        elif kind == 'clean':
            patch = git_patches.get(path)
            stat = _stat_of(patch) if patch else ''
            resolved.append((path, f'[{path}]({path}){(" - " + stat) if stat else ""}', patch))
        elif kind == 'removed':
            snap_text = blobs.get(path, {}).get('content')
            patch = _turn_patch(snap_text, '', path) if snap_text else None
            resolved.append((path, f'[{path}]({path}) (deleted)', patch))
        elif kind == 'reverted':
            snap_text = blobs.get(path, {}).get('content')
            cur_text = current[path][1]
            patch = _turn_patch(snap_text, cur_text, path) if snap_text and cur_text else None
            resolved.append((path, f'[{path}]({path}) (reverted)', patch))
        else:  # 'dirty': dirty at start, changed during turn -> turn-only patch
            snap = blobs.get(path, {})
            cur_text = current[path][1]
            if current[path][2] or snap.get('binary'):
                resolved.append((path, f'[{path}]({path}) (binary changed)', None))
            elif snap.get('content') is None or cur_text is None:
                resolved.append((path, f'[{path}]({path}) (modified)', None))
            else:
                patch = _turn_patch(snap['content'], cur_text, path)
                stat = _stat_of(patch)
                resolved.append((path, f'[{path}]({path}){(" - " + stat) if stat else ""}', patch))

    lines = ['\n\n**Changed files:**\n']
    for _, label, _ in resolved:
        lines.append(f'- {label}')
    if head_moved:
        lines.append('\n*(HEAD moved mid-turn, worktree diffs may be approximate)*')

    if include_diff:
        shown = 0
        total = 0
        for _, _, patch in resolved:
            if not patch or not patch.strip():
                continue
            if shown >= MAX_FILES or total >= MAX_TOTAL_CHARS:
                lines.append(f'\n*... and {len(resolved) - shown} more file(s), diff omitted*')
                break
            patch, cut = _truncate(patch, MAX_LINES_PER_FILE)
            if cut:
                patch += '\n... (truncated)'
            if total + len(patch) > MAX_TOTAL_CHARS:
                patch = patch[: max(0, MAX_TOTAL_CHARS - total)] + '\n... (truncated)'
            lines.append(f'\n```diff\n{patch}\n```')
            total += len(patch)
            shown += 1
    lines.append('')
    return '\n'.join(lines).rstrip('\n')
