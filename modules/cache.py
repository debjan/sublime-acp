"""Agent configuration cache."""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import CACHE_TTL_DEFAULT, settings

cache_lock = threading.RLock()

_memo: dict[Path, tuple[float, int | None, dict]] = {}


def _agents_path(cache_dir: Path) -> Path:
    return cache_dir / 'agents.json'


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


def load_agents(cache_dir: Path, ttl: float | None = None) -> dict:
    """Load the agents dict from ``cache_dir/agents.json``."""
    if ttl is None:
        ttl = settings().get('cache_ttl', CACHE_TTL_DEFAULT)
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
                _memo[data_file] = (now, entry[1], entry[2])
                return copy.deepcopy(entry[2])
        data, mtime_ns = _parse_agents(data_file)
        if data:
            _memo[data_file] = (now, mtime_ns, copy.deepcopy(data))
        else:
            _memo.pop(data_file, None)
        return data


def save_agents(cache_dir: Path, data: dict) -> None:
    """Save the agents dict to ``cache_dir/agents.json`` atomically."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    data_file = _agents_path(cache_dir)
    temp_file = cache_dir / 'agents.json.tmp'
    try:
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(temp_file, data_file)
        with cache_lock:
            try:
                mtime_ns = data_file.stat().st_mtime_ns
            except OSError:
                _memo.pop(data_file, None)
            else:
                _memo[data_file] = (
                    time.monotonic(), mtime_ns, copy.deepcopy(data),
                )
    finally:
        if temp_file.exists():
            temp_file.unlink()


def update_session_id(cache_dir: Path, cmd: list, session_id: str) -> None:
    """Update the ``last_session_id`` for an agent in the cache atomically."""
    with cache_lock:
        agents = load_agents(cache_dir)
        agent_key = cmd[0]
        if agent_key not in agents:
            agents[agent_key] = {}
        agents[agent_key]['last_session_id'] = session_id
        agents[agent_key]['last_sync'] = datetime.now().isoformat()
        save_agents(cache_dir, agents)


def get_model_name(cache_dir: Path, cmd: list | None) -> str | None:
    """Return the agent's configured model from the cache, or ``None``."""
    if not cmd:
        return None
    for opt in load_agents(cache_dir).get(cmd[0], {}).get('config_options') or []:
        if opt.get('id') == 'model':
            return opt.get('currentValue')
    return None


def clear_session_id(cache_dir: Path, cmd: list) -> None:
    """Clear the cached ``last_session_id`` for an agent."""
    with cache_lock:
        agents = load_agents(cache_dir)
        entry = agents.get(cmd[0])
        if entry is None:
            return
        entry.pop('last_session_id', None)
        if not entry:
            agents.pop(cmd[0], None)
        save_agents(cache_dir, agents)
