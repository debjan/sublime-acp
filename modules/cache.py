"""Agent configuration cache."""

from __future__ import annotations

import copy
import json
import os
import shlex
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import CACHE_TTL_DEFAULT

cache_lock = threading.RLock()

_memo: dict[Path, tuple[float, int | None, dict]] = {}

_MEMO_MAX_ENTRIES = 8


def _memo_store(data_file: Path, value: tuple[float, int | None, dict]) -> None:
    """Store a memo entry, evicting the stalest first when over the cap."""
    _memo[data_file] = value
    while len(_memo) > _MEMO_MAX_ENTRIES:
        oldest = min(_memo.items(), key=lambda item: item[1][0])[0]
        del _memo[oldest]


def _agents_path(cache_dir: Path) -> Path:
    return cache_dir / 'agents.json'


def agent_key(cmd: list | None) -> str | None:
    """Return the stable cache key for an agent command list."""
    if not cmd:
        return None
    return shlex.join(str(part) for part in cmd)


def _legacy_json_key(cmd: list) -> str:
    """Return the pre-simplify cache key for *cmd* (for migration)."""
    return json.dumps([str(part) for part in cmd])


def get_entry(agents: dict, cmd: list | None) -> dict:
    """Return the cache entry for *cmd*, migrating legacy keys in memory."""
    if not cmd:
        return {}
    key = agent_key(cmd)
    if key in agents:
        return agents.get(key) or {}
    legacy_json = _legacy_json_key(cmd)
    if legacy_json in agents and key is not None:
        agents[key] = agents.pop(legacy_json)
        return agents.get(key) or {}
    legacy = cmd[0]
    if (
        len(cmd) == 1 and isinstance(legacy, str)
        and legacy in agents and key is not None
    ):
        agents[key] = agents.pop(legacy)
        return agents.get(key) or {}
    return {}


def _parse_agents(data_file: Path) -> tuple[dict, int | None]:
    """Parse ``data_file``, returning its contents and ``st_mtime_ns``."""
    try:
        mtime_ns = data_file.stat().st_mtime_ns
    except OSError:
        return {}, None
    try:
        with open(data_file, encoding='utf-8') as f:
            return json.load(f), mtime_ns
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}, mtime_ns


def load_agents(cache_dir: Path, ttl: float = CACHE_TTL_DEFAULT) -> dict:
    """Load the agents dict from ``cache_dir/agents.json``.

    *ttl* bounds memo reuse and is a plain value on purpose: this function
    must never touch the Sublime API, since it runs on background threads
    and is routinely called while holding ``cache_lock`` (a Sublime call
    here can deadlock against the main thread waiting for that lock).
    Callers on the main thread that want the configured ``cache_ttl``
    must read it themselves and pass it in.
    """
    data_file = _agents_path(cache_dir)
    with cache_lock:
        now = time.monotonic()
        entry = _memo.get(data_file)
        if entry is not None and now - entry[0] <= ttl:
            return copy.deepcopy(entry[2])
        if entry is not None:
            try:
                unchanged = data_file.stat().st_mtime_ns == entry[1]
            except OSError:
                unchanged = False
            if unchanged:
                _memo_store(data_file, (now, entry[1], entry[2]))
                return copy.deepcopy(entry[2])
        data, mtime_ns = _parse_agents(data_file)
        if data:
            _memo_store(data_file, (now, mtime_ns, copy.deepcopy(data)))
        else:
            _memo.pop(data_file, None)
        return data


def save_agents(cache_dir: Path, data: dict) -> None:
    """Save the agents dict to ``cache_dir/agents.json`` atomically.

    When the serialized content is unchanged, the file is left untouched
    (keeping its mtime) so editors with the file open do not reload.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    data_file = _agents_path(cache_dir)
    payload = json.dumps(data, indent=2, ensure_ascii=False).encode('utf-8')
    try:
        existing = data_file.read_bytes()
    except OSError:
        existing = None
    if existing == payload:
        with cache_lock:
            try:
                mtime_ns = data_file.stat().st_mtime_ns
            except OSError:
                _memo.pop(data_file, None)
            else:
                _memo_store(data_file, (
                    time.monotonic(), mtime_ns, copy.deepcopy(data),
                ))
        return
    temp_file = cache_dir / 'agents.json.tmp'
    try:
        with open(temp_file, 'wb') as f:
            f.write(payload)
        os.replace(temp_file, data_file)
        with cache_lock:
            try:
                mtime_ns = data_file.stat().st_mtime_ns
            except OSError:
                _memo.pop(data_file, None)
            else:
                _memo_store(data_file, (
                    time.monotonic(), mtime_ns, copy.deepcopy(data),
                ))
    finally:
        if temp_file.exists():
            temp_file.unlink()


def update_session_id(cache_dir: Path, cmd: list, session_id: str) -> None:
    """Update the ``last_session_id`` for an agent in the cache atomically.

    A no-op when the cached ID already matches, so the cache file is not
    rewritten (and editors do not reload) on repeated syncs.
    """
    with cache_lock:
        agents = load_agents(cache_dir)
        entry = get_entry(agents, cmd)
        key = agent_key(cmd)
        if key is None:
            return
        if entry.get('last_session_id') == session_id:
            return
        entry['last_session_id'] = session_id
        entry['last_sync'] = datetime.now().isoformat()
        agents[key] = entry
        save_agents(cache_dir, agents)


def _get_agent_entry(agents: dict, cmd: list | None) -> tuple[dict, str | None]:
    entry = get_entry(agents, cmd)
    return entry, agent_key(cmd)


def set_session_title(cache_dir: Path, cmd: list, session_id: str, title: str | None) -> None:
    """Persist a local title override for *session_id* for the given agent.

    A ``None`` or blank *title* clears any existing override, so callers can
    pass user input straight through without pre-normalizing it.
    """
    label = ' '.join(title.split()) if isinstance(title, str) else ''
    with cache_lock:
        agents = load_agents(cache_dir)
        entry, agent_key = _get_agent_entry(agents, cmd)
        if agent_key is None:
            return
        titles = entry.get('session_titles') or {}
        if not label:
            if session_id not in titles:
                return
            titles.pop(session_id, None)
        else:
            titles[session_id] = label
        if titles:
            entry['session_titles'] = titles
        else:
            entry.pop('session_titles', None)
        agents[agent_key] = entry
        save_agents(cache_dir, agents)


def get_session_title_override(cache_dir: Path, cmd: list | None, session_id: str | None) -> str | None:
    """Return a local title override for *session_id*, if any."""
    if not cmd or not session_id:
        return None
    with cache_lock:
        agents = load_agents(cache_dir)
        entry, _ = _get_agent_entry(agents, cmd)
        titles = entry.get('session_titles') or {}
        t = titles.get(session_id)
        if isinstance(t, str) and t.strip():
            return t
        return None


def get_model_name(cache_dir: Path, cmd: list | None) -> str | None:
    """Return the agent's configured model from the cache, or ``None``."""
    if not cmd:
        return None
    for opt in get_entry(load_agents(cache_dir), cmd).get('config_options') or []:
        if opt.get('id') == 'model':
            return opt.get('currentValue')
    return None


def set_config_option_value(cache_dir: Path, cmd: list | None, config_id: str, value: str) -> None:
    """Set ``currentValue`` for one cached config option.

    A no-op when the agent entry or option is missing, or the value is
    unchanged, so redundant syncs do not rewrite the cache file.
    """
    if not cmd or not config_id:
        return
    with cache_lock:
        agents = load_agents(cache_dir)
        entry, key = _get_agent_entry(agents, cmd)
        if key is None or not entry:
            return
        for opt in entry.get('config_options') or []:
            if isinstance(opt, dict) and opt.get('id') == config_id:
                if opt.get('currentValue') == value:
                    return
                opt['currentValue'] = value
                break
        else:
            return
        agents[key] = entry
        save_agents(cache_dir, agents)


def clear_session_id(cache_dir: Path, cmd: list) -> None:
    """Clear the cached ``last_session_id`` for an agent."""
    with cache_lock:
        agents = load_agents(cache_dir)
        entry, key = _get_agent_entry(agents, cmd)
        if key is None or not entry:
            return
        entry.pop('last_session_id', None)
        agents[key] = entry
        legacy = cmd[0] if cmd else None
        if legacy != key:
            agents.pop(legacy, None)
            agents.pop(_legacy_json_key(cmd), None)
        if not entry:
            agents.pop(key, None)
        save_agents(cache_dir, agents)


def allowed_keys_for_commands(commands: list | None) -> set[str]:
    """Return current plus legacy cache keys for configured commands."""
    keys: set[str] = set()
    for item in commands or []:
        if not isinstance(item, dict):
            continue
        cmd_str = item.get('cmd')
        if not cmd_str:
            continue
        cmd = [cmd_str] + list(item.get('args') or [])
        key = agent_key(cmd)
        if key is not None:
            keys.add(key)
        keys.add(_legacy_json_key(cmd))
        if len(cmd) == 1 and isinstance(cmd[0], str):
            keys.add(cmd[0])
    return keys


def prune_agents(data: dict, allowed: set[str]) -> bool:
    """Drop entries not in *allowed*; return ``True`` when anything was removed."""
    stale = [k for k in data if k not in allowed]
    for k in stale:
        del data[k]
    return bool(stale)
