"""ACP session management - create, resume, load sessions."""

from __future__ import annotations

import os
from typing import Any

from .connection import ACPError, Connection
from .log import acp_log

STATUS_NEW = 'new'
STATUS_RESUMED = 'resumed'
STATUS_LOADED = 'loaded'
STATUS_ERROR = 'error'


async def new_session(
    conn: Connection,
    base_params: dict[str, Any],
) -> tuple[str | None, str, list | None, str | None]:
    """Create a new ACP session via ``session/new``.

    Args:
        conn: The active connection to the agent.
        base_params: Base parameters including ``cwd`` and ``mcpServers``.

    Returns:
        ``(session_id, STATUS_NEW, config_options, None)`` or
        ``(None, STATUS_NEW, None, None)`` if the agent did not return a
        ``sessionId``.
    """
    result = await conn.send_request('session/new', base_params)
    if not result or not result.get('sessionId'):
        acp_log('session', 'Session setup did not return a sessionId')
        return None, STATUS_NEW, None, None
    return result['sessionId'], STATUS_NEW, result.get('configOptions'), None


async def _fallback_new(
    conn: Connection,
    base_params: dict[str, Any],
    error_msg: str,
) -> tuple[str | None, str, list | None, str | None]:
    """Create a new session as fallback, attaching *error_msg* to the result.

    Wraps :func:`new_session` for the three fallback paths in
    :func:`resolve_session`: the new session is created with *base_params*
    and its (always-``None``) error slot is replaced with *error_msg* so the
    caller knows why the original resume/load attempt was abandoned.
    """
    sid, status, opts, _ = await new_session(conn, base_params)
    return sid, status, opts, error_msg


async def resolve_session(
    conn: Connection,
    agent_caps: dict[str, Any],
    session_id: str | None,
    cwd: str | None,
) -> tuple[str | None, str, list | None, str | None]:
    """Resolve an ACP session, trying resume/load before falling back to new.

    Attempts ``session/resume`` then ``session/load`` (based on agent
    capabilities), falling back to ``session/new`` on failure or when no
    ``session_id`` is provided.

    Args:
        conn: The active connection to the agent.
        agent_caps: Agent capabilities dict from the initialize response.
        session_id: Optional session ID to resume or load.
        cwd: Working directory for the session.

    Returns:
        ``(session_id, status, config_options, error_msg)`` where *status*
        is one of ``STATUS_NEW``, ``STATUS_RESUMED``, or ``STATUS_LOADED``.
        *error_msg* is ``None`` on success, or a human-readable string when
        resume/load failed and a new session was created as fallback.
    """
    base_params: dict[str, Any] = {
        'cwd': cwd or os.getcwd(),
        'mcpServers': [],
    }

    if session_id is not None:
        sess_caps = agent_caps.get('sessionCapabilities') or {}
        if isinstance(sess_caps, dict) and isinstance(sess_caps.get('resume'), dict):
            try:
                await conn.send_request('session/resume', {
                    'sessionId': session_id, **base_params,
                })
                return session_id, STATUS_RESUMED, None, None
            except ACPError as exc:
                return await _fallback_new(
                    conn, base_params, f'Could not resume session: {exc}',
                )
        elif agent_caps.get('loadSession'):
            try:
                await conn.send_request('session/load', {
                    'sessionId': session_id, **base_params,
                })
                return session_id, STATUS_LOADED, None, None
            except ACPError as exc:
                return await _fallback_new(
                    conn, base_params, f'Could not load previous session: {exc}',
                )
        else:
            return await _fallback_new(
                conn, base_params,
                'Agent does not support session resume or load',
            )

    return await new_session(conn, base_params)
