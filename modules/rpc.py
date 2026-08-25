"""ACP JSON-RPC transport library."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import os
import sys
from pathlib import Path
from typing import Any

from ..protocol import (
    PROTOCOL_VERSION,
    STATUS_ERROR,
    STATUS_LOADED,
    STATUS_NEW,
    STATUS_RESUMED,
    ACPError,
    Connection,
    acp_log,
    cleanup_process,
    resolve_session,
    spawn_subprocess,
)
from .permissions import resolve_permission

_INITIALIZE_PARAMS = {
    'protocolVersion': PROTOCOL_VERSION,
    'clientCapabilities': {
        'fs': {
            'readTextFile': True,
            'writeTextFile': True,
        },
    },
    'clientInfo': {'name': 'sublime-acp', 'version': '1.0.0'},
}

_FS_ALLOW_OPTION_ID = 'allow'
_FS_PERMISSION_OPTIONS = [
    {'optionId': _FS_ALLOW_OPTION_ID, 'name': 'Allow'},
    {'optionId': 'reject', 'name': 'Reject'},
]


def _log_task_failure(task: asyncio.Task) -> None:
    """Log any unhandled exception from a completed asyncio task."""
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc:
        acp_log('rpc', f'Task failed: {exc}')


def _make_request_callback(handler: Any) -> Any:
    """Wrap an async request handler into a ``request_callback``.

    The returned callable spawns the async *handler* as an ``asyncio.Task``
    and logs any unhandled exception via ``_log_task_failure``.
    """
    return lambda mid, method, params: asyncio.ensure_future(
        handler(mid, method, params),
    ).add_done_callback(_log_task_failure)


def _validate_workspace_path(workspace_root: str, path_str: str) -> Path | None:
    """Resolve a path relative to the workspace root and confine it there."""
    try:
        ws_root = Path(workspace_root).resolve()
        file_path = (ws_root / path_str.lstrip('/')).resolve()
    except Exception:
        return None

    try:
        file_path.relative_to(ws_root)
    except ValueError:
        return None

    return file_path


def _is_positive_int(val: Any) -> bool:
    """Return ``True`` only if *val* is a non-bool integer >= 1."""
    return isinstance(val, int) and not isinstance(val, bool) and val >= 1


def _read_file_range(file_path: Path, line: int | None, limit: int | None) -> str:
    """Synchronous helper: read a range of lines from a file (runs in executor)."""
    with open(file_path, 'r', encoding='utf-8') as f:
        start = max(0, (line - 1) if line else 0)
        for _ in itertools.islice(f, start):
            pass
        selected = itertools.islice(f, limit) if limit else f
        content = ''.join(selected)
    return content


def _write_file_sync(file_path: Path, content: str) -> None:
    """Synchronous helper: atomically write a file (runs in executor)."""
    temp_path = file_path.with_suffix(file_path.suffix + '.tmp')
    committed = False
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(temp_path, 'w', encoding='utf-8') as f:
            f.write(content)
        os.replace(temp_path, file_path)
        committed = True
    finally:
        if not committed and temp_path.exists():
            temp_path.unlink(missing_ok=True)


def _extract_available_commands(method: str, params: dict) -> tuple[bool, Any]:
    """Return ``(matched, availableCommands)`` for an update notification.

    *matched* is ``True`` when the notification carries an
    ``available_commands_update``; the commands value may be ``None``.
    """
    if method != 'session/update':
        return False, None
    update = params.get('update', {})
    if update.get('sessionUpdate') != 'available_commands_update':
        return False, None
    return True, update.get('availableCommands')


def _handle_prompt_result(msg: dict) -> list[str]:
    """Extract text content from a ``session/prompt`` result message."""
    if 'error' in msg:
        err = msg['error']
        message = err.get('message', err)
        acp_log('rpc', f'session/prompt failed: {message}')
        return [message]
    result = msg.get('result', {})
    if not isinstance(result, dict):
        return []
    content_blocks = result.get('content', [])
    if isinstance(content_blocks, dict):
        content_blocks = [content_blocks]
    texts = []
    for block in content_blocks:
        if isinstance(block, dict) and block.get('type') == 'text':
            texts.append(block.get('text', ''))
    return texts


def _format_blockquote(text: str) -> str:
    """Prefix each line of *text* with a ``> `` blockquote marker."""
    if not text or not text.strip():
        return ''
    lines = text.split('\n')
    quoted = [f'> {line}' for line in lines]
    return '\n'.join(quoted)


def _flush_thought_buffer(thought_buf: list[str]) -> str:
    if not thought_buf:
        return ''
    result = _format_blockquote(''.join(thought_buf))
    thought_buf.clear()
    return result


def _flush_thoughts(
    thought_buf: list[str],
    thoughts_mode: str,
    callback: Any | None,
) -> bool:
    """Emit buffered thoughts for the given mode; return True if any were emitted.

    ``'enabled'`` buffers thoughts into a blockquote delivered through
    *callback* (or ``stdout``); ``'console'`` writes the raw buffer to
    ``stderr``; ``'disabled'`` drops them entirely.
    """
    if thoughts_mode == 'enabled':
        if flushed := _flush_thought_buffer(thought_buf):
            if callback:
                callback(flushed)
            else:
                sys.stdout.write(flushed)
        return bool(flushed)
    if thoughts_mode == 'console':
        flushed = ''.join(thought_buf)
        thought_buf.clear()
        if flushed:
            sys.stderr.write(flushed + '\n')
            sys.stderr.flush()
        return bool(flushed)
    thought_buf.clear()
    return False


def _handle_session_update(update: dict) -> list | None:
    """Extract content chunks from a ``session/update`` notification.

    Handles ``agent_message_chunk`` and ``agent_thought_chunk`` updates,
    returning a list of text strings or ``('__thought__', text)`` tuples.
    """
    kind = update.get('sessionUpdate')
    if not kind:
        return None
    content = update.get('content', {})
    if not isinstance(content, dict):
        return None
    if content.get('type') != 'text':
        return None
    if kind == 'agent_message_chunk':
        return [content.get('text', '')]
    elif kind == 'agent_thought_chunk':
        text = content.get('text', '')
        return [('__thought__', text)] if text.strip() else []
    else:
        return None


def _handle_session_update_nested(update: dict) -> list | None:
    """Extract content chunks from a nested ``message_chunk`` update.

    Handles nested update structures where the chunk type is embedded
    within a ``chunk`` sub-dict. Returns text strings or
    ``('__thought__', text)`` tuples.
    """
    if update.get('type') != 'message_chunk':
        return None
    chunk = update.get('chunk', {})
    if not isinstance(chunk, dict):
        return []
    chunk_type = chunk.get('type')
    chunk_text = chunk.get('text', '')
    if chunk_type == 'thought':
        return [('__thought__', chunk_text)] if chunk_text.strip() else []
    return [chunk_text]


def _handle_session_update_notification(msg: dict) -> list:
    """Extract content chunks from a ``session/update`` notification message.

    Dispatches to either ``_handle_session_update`` or
    ``_handle_session_update_nested`` based on the update structure.
    """
    if msg.get('method') != 'session/update':
        return []
    update = msg.get('params', {}).get('update', {})
    if not isinstance(update, dict):
        return []
    result = _handle_session_update(update)
    if result is not None:
        return result
    result = _handle_session_update_nested(update)
    if result is not None:
        return result
    return []


def _make_fs_permission_params(
    tool_kind: str,
    title: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Build a ``session/request_permission``-shaped params dict for an fs op.

    The synthesized ``toolCall.kind`` routes the operation through the same
    auto-allow / auto-reject rules as agent permission requests. Reads use
    ``'read_file'`` (matches the default ``read*`` auto-allow glob); writes
    use ``'write_file'`` (not auto-allowed by default).

    Args:
        tool_kind: Synthesized ``toolCall.kind`` for the operation.
        title: Human-readable prompt title, e.g. ``'Write file'``.
        params: The original fs request params dict (for ``path``).

    Returns:
        A ``session/request_permission`` params dict with ``toolCall``
        and ``options``.
    """
    path = params.get('path', '')
    return {
        'toolCall': {
            'kind': tool_kind,
            'title': f'{title} {path}' if path else f'{title} (no path given)',
        },
        'options': _FS_PERMISSION_OPTIONS,
    }


async def _handle_init_phase(
    conn: Connection,
    model: str | None = None,
    session_id: str | None = None,
    cwd: str | None = None,
    workspace_root: str | None = None,
    permissions_config: dict | None = None,
    auth: bool | None = None,
) -> dict[str, Any] | None:
    """Run the ACP initialization phase with the agent.

    Sends ``initialize``, optionally authenticates, resolves a session (resume,
    load, or new), and optionally sets the model. Installs request and
    notification callbacks to handle agent requests during init.
    """
    ws_root = workspace_root or cwd or os.getcwd()

    conn.request_callback = _make_request_callback(
        lambda mid, method, params: _handle_agent_request(
            conn, mid, method, params,
            ws_root, permissions_config, 'one-shot',
        ),
    )

    available_commands: Any = None

    def on_notification(method: str, params: dict[str, Any]) -> None:
        nonlocal available_commands
        matched, commands = _extract_available_commands(method, params)
        if matched:
            available_commands = commands

    conn.notification_callback = on_notification
    try:
        acp_log('rpc', '_handle_init_phase: sending initialize')
        result = await conn.send_request('initialize', _INITIALIZE_PARAMS)
        agent_caps = result.get('agentCapabilities', {})
        acp_log('rpc', '_handle_init_phase: initialize succeeded')

        if auth_methods := result.get('authMethods', []):
            if auth is False:
                acp_log('rpc', '_handle_init_phase: auth=False, skipping authentication')
            else:
                acp_log('rpc', '_handle_init_phase: authenticating')
                await conn.send_request('authenticate', {
                    'methodId': auth_methods[0]['id'],
                })
                acp_log('rpc', '_handle_init_phase: authentication done')

        acp_log('rpc', f'_handle_init_phase: resolving session (session_id={session_id})')
        resolved_session_id, opened_via, config_options, session_error = await resolve_session(
            conn, agent_caps, session_id, cwd,
        )
        if resolved_session_id is None:
            acp_log('rpc', '_handle_init_phase: session resolution returned None')
            return None
        acp_log('rpc', f'_handle_init_phase: session resolved: sid={resolved_session_id}, opened_via={opened_via}')

        confirmed_model: str | None = None
        if model and resolved_session_id:
            try:
                acp_log('rpc', f'_handle_init_phase: setting model to {model}')
                response = await conn.send_request('session/set_config_option', {
                    'sessionId': resolved_session_id,
                    'configId': 'model',
                    'value': model,
                })
                if isinstance(response, dict):
                    confirmed_model = (
                        response.get('currentValue')
                        or response.get('value')
                        or model
                    )
                else:
                    confirmed_model = model
            except (ACPError, asyncio.TimeoutError) as exc:
                acp_log(
                    'rpc',
                    f'_handle_init_phase: failed to set model {model}: {type(exc).__name__}: {exc}',
                )

        if confirmed_model:
            for opt in config_options or []:
                if isinstance(opt, dict) and opt.get('id') == 'model':
                    opt['currentValue'] = confirmed_model
                    break

        return {
            'session_id': resolved_session_id,
            'config_options': config_options,
            'model': confirmed_model,
            'opened_via': opened_via,
            'session_error': session_error,
            'available_commands': available_commands,
            'initialize_result': result,
        }

    except (ACPError, asyncio.TimeoutError, ConnectionError) as exc:
        acp_log('rpc', f'_handle_init_phase: init failed: {exc}')
        return None


async def _handle_agent_request(
    conn: Connection,
    msg_id: int,
    method: str,
    params: dict[str, Any],
    workspace_root: str,
    permissions_config: dict | None = None,
    mode: str = 'one-shot',
    on_permission_prompt: Any | None = None,
) -> bool:
    """Handle an incoming agent request and send a response.

    Dispatches to appropriate handlers for ``session/request_permission``,
    ``fs/read_text_file``, and ``fs/write_text_file`` methods. All three
    resolve through the permission system: reads use kind ``read_file``
    (auto-allowed by the default ``read*`` rule), writes use kind
    ``write_file`` (prompted in daemon mode, denied elsewhere unless
    auto-allowed). Denied fs operations receive a JSON-RPC error.
    Unhandled methods return ``False``.
    """
    try:
        if method == 'session/request_permission':
            opt_id = await _resolve_permission_id(
                params, permissions_config, on_permission_prompt,
            )
            await conn.respond_to_request(
                msg_id,
                {'outcome': {'outcome': 'selected' if opt_id else 'cancelled', 'optionId': opt_id}},
            )
            return True
        elif method == 'fs/read_text_file':
            if await _check_fs_permission(
                conn, msg_id, 'read_file', 'Read file',
                params, permissions_config, on_permission_prompt,
            ):
                result = await _handle_fs_read_text_file_sync(workspace_root, params)
                await conn.respond_to_request(msg_id, result)
            return True
        elif method == 'fs/write_text_file':
            if await _check_fs_permission(
                conn, msg_id, 'write_file', 'Write file',
                params, permissions_config, on_permission_prompt,
            ):
                result = await _handle_fs_write_text_file_sync(workspace_root, params)
                await conn.respond_to_request(msg_id, result)
            return True
        return False
    except Exception as exc:
        await conn.respond_with_error(msg_id, -32603, str(exc))
        return True


async def _handle_fs_read_text_file_sync(workspace_root: str, params: dict[str, Any]) -> dict[str, Any]:
    """Handle a ``fs/readTextFile`` request without blocking the event loop.

    Validation runs on the loop; the actual file read is offloaded to a
    thread-pool executor so it does not block the event loop.
    """
    path_str = params.get('path', '')
    line = params.get('line')
    limit = params.get('limit')

    if line is not None and not _is_positive_int(line):
        return {'error': {'code': -32602, 'message': "'line' must be a positive integer"}}
    if limit is not None and not _is_positive_int(limit):
        return {'error': {'code': -32602, 'message': "'limit' must be a positive integer"}}

    file_path = _validate_workspace_path(workspace_root, path_str)
    if file_path is None:
        return {'error': {'code': -32602, 'message': 'Path outside workspace'}}
    if not file_path.is_file():
        return {'error': {'code': -32602, 'message': 'File not found'}}

    try:
        loop = asyncio.get_running_loop()
        content = await loop.run_in_executor(None, _read_file_range, file_path, line, limit)
    except Exception as exc:
        return {'error': {'code': -32602, 'message': str(exc)}}

    return {'content': content}


async def _handle_fs_write_text_file_sync(workspace_root: str, params: dict[str, Any]) -> dict[str, Any]:
    """Handle a ``fs/writeTextFile`` request without blocking the event loop.

    Validation runs on the loop; the actual file write is offloaded to a
    thread-pool executor so it does not block the event loop.
    """
    path_str = params.get('path', '')
    content = params.get('content', '')

    file_path = _validate_workspace_path(workspace_root, path_str)
    if file_path is None:
        return {'error': {'code': -32602, 'message': 'Path outside workspace'}}

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _write_file_sync, file_path, content)
        return {}
    except Exception as exc:
        return {'error': {'code': -32602, 'message': str(exc)}}


async def _resolve_permission_id(
    params: dict[str, Any],
    permissions_config: dict | None,
    on_permission_prompt: Any | None,
) -> str | None:
    """Resolve a permission request to an ``optionId`` (or ``None`` to cancel).

    ``on_permission_prompt`` (wired by the daemon) handles interactive
    prompts; otherwise :func:`resolve_permission` is used, which cancels
    any kind that is not auto-allowed when no interactive window is
    available (one-shot, init phase).

    Args:
        params: A ``session/request_permission``-shaped params dict.
        permissions_config: Optional permission rules for auto-allow /
            auto-reject handling.
        on_permission_prompt: Optional callable returning the chosen
            ``optionId`` (or ``None`` to cancel) for interactive prompts.

    Returns:
        The chosen ``optionId``, or ``None`` if cancelled or denied.
    """
    if on_permission_prompt:
        return await on_permission_prompt(params)
    return await resolve_permission(params, permissions_config)


async def _check_fs_permission(
    conn: Connection,
    msg_id: int,
    tool_kind: str,
    title: str,
    params: dict[str, Any],
    permissions_config: dict | None,
    on_permission_prompt: Any | None,
) -> bool:
    """Resolve permission for an fs operation and deny it when rejected."""
    opt_id = await _resolve_permission_id(
        _make_fs_permission_params(tool_kind, title, params),
        permissions_config,
        on_permission_prompt,
    )
    if opt_id == _FS_ALLOW_OPTION_ID:
        return True
    await conn.respond_with_error(msg_id, -32000, 'Permission denied')
    return False


async def _close_connection(proc, conn: Connection) -> None:
    """Close the connection and clean up the agent subprocess."""
    await conn.close()
    await cleanup_process(proc, conn.writer)


async def probe_agent(cmd: list[str], env: dict | None = None) -> dict | None:
    """Spawn an agent, send ``initialize``, clean up, and return the result.

    Unlike :func:`spawn_and_init`, this function handles its own cleanup
    before returning. The caller receives only the ``initialize`` result
    dict and never holds a reference to the process or connection.
    """
    acp_log('rpc', f'probe_agent: cmd={cmd}')
    proc, reader, writer = await spawn_subprocess(cmd, env, cwd=None)
    acp_log('rpc', f'probe_agent: subprocess spawned (pid={proc.pid})')
    conn = Connection(reader, writer)
    conn.request_callback = _make_request_callback(
        lambda mid, method, params: _handle_agent_request(
            conn, mid, method, params,
            os.getcwd(), None, 'one-shot',
        ),
    )
    try:
        result = await conn.send_request('initialize', _INITIALIZE_PARAMS)
    except (ACPError, asyncio.TimeoutError, ConnectionError) as exc:
        acp_log('rpc', f'probe_agent: failed: {exc}')
        result = None
    finally:
        await _close_connection(proc, conn)
    return result


async def list_capabilities(cmd: list[str], env: dict | None = None) -> dict | None:
    """Connect to an agent and retrieve its capabilities.

    Spawns the agent subprocess, sends an ``initialize`` request, and
    returns the agent capabilities result.
    """
    result = await probe_agent(cmd, env)
    if result is None:
        return None
    return {'result': result}


async def list_config(cmd: list[str], env: dict | None = None, command_timeout: int = 10) -> dict | None:
    """Connect to an agent and retrieve its configuration options and commands.

    Spawns the agent subprocess, runs the init phase, and waits for
    ``available_commands_update`` notifications. Returns the config options
    and any collected commands.
    """
    result = await spawn_and_init(cmd, env)
    if result is None:
        return None

    proc, conn, init_result = result
    config: dict[str, Any] = {'config_options': init_result.get('config_options')}

    commands = init_result.get('available_commands')
    if commands is not None:
        config['commands'] = commands
        await _close_connection(proc, conn)
        return config

    commands_event = asyncio.Event()
    collected_commands = None

    def on_notification(method: str, params: dict) -> None:
        nonlocal collected_commands
        matched, commands = _extract_available_commands(method, params)
        if matched:
            collected_commands = commands
            commands_event.set()

    async def on_request(msg_id: int, method: str, params: dict) -> None:
        handled = await _handle_agent_request(
            conn, msg_id, method, params,
            os.getcwd(), None, 'one-shot',
        )
        if not handled:
            await conn.respond_with_error(msg_id, -32601, f'Method not supported: {method}')

    async with conn.swap_callbacks(
        on_notification,
        _make_request_callback(on_request),
    ):
        try:
            await asyncio.wait_for(commands_event.wait(), timeout=command_timeout)
        except asyncio.TimeoutError:
            pass

    if collected_commands is not None:
        config['commands'] = collected_commands

    await _close_connection(proc, conn)
    return config


async def spawn_and_init(
    cmd: list[str],
    env: dict | None = None,
    model: str | None = None,
    session_id: str | None = None,
    cwd: str | None = None,
    permissions_config: dict | None = None,
    auth: bool | None = None,
) -> tuple[asyncio.subprocess.Process, Connection, dict] | None:
    """Spawn agent subprocess and run the init phase.

    Spawns the agent, opens a ``Connection``, and runs initialization
    (``initialize``, optional authentication, session resolution, and an
    optional ``session/set_config_option`` for ``model``). Installs
    request and notification callbacks to handle agent requests inline.

    To probe an agent without holding an open connection, use
    :func:`probe_agent` instead.

    Args:
        cmd: Agent command list (executable path followed by arguments).
        env: Optional environment variables layered over the current process env.
        model: Optional model name set via ``session/set_config_option``.
        session_id: Optional existing session ID to resume or load.
        cwd: Optional working directory for the agent subprocess.
        permissions_config: Optional permission rules for auto-allow /
            auto-reject handling.
        auth: Override authentication behavior: ``False`` skips auth
            entirely, ``None``/``True`` authenticates when the agent offers
            auth methods.

    Returns:
        ``(proc, conn, init_result)`` on success, or ``None`` on init failure.
    """
    acp_log('rpc', f'spawn_and_init: cmd={cmd}, session_id={session_id}, auth={auth}')

    proc, reader, writer = await spawn_subprocess(cmd, env, cwd)
    acp_log('rpc', f'spawn_and_init: subprocess spawned (pid={proc.pid})')

    conn = Connection(reader, writer)

    try:
        init_result = await _handle_init_phase(
            conn, model, session_id, cwd,
            workspace_root=cwd, permissions_config=permissions_config,
            auth=auth,
        )
        if init_result is None:
            acp_log('rpc', 'spawn_and_init: init phase returned None - cleaning up')
            await _close_connection(proc, conn)
            return None
        acp_log('rpc', f"spawn_and_init: init phase succeeded, sid={init_result.get('session_id')}")
        return proc, conn, init_result
    except Exception as exc:
        acp_log('rpc', f'spawn_and_init: exception during init: {exc}')
        await _close_connection(proc, conn)
        raise


async def send_prompt_and_stream(
    conn: Connection,
    session_id: str,
    prompt: str,
    system_prompt: str | None = None,
    callback: Any | None = None,
    callback_timeout: float = 60.0,
    workspace_root: str | None = None,
    permissions_config: dict | None = None,
    mode: str = 'one-shot',
    on_permission_prompt: Any | None = None,
    thoughts_mode: str = 'enabled',
    on_commands: Any | None = None,
) -> bool:
    """Send a ``session/prompt`` and stream the response.

    Installs temporary notification and request callbacks on *conn* to stream
    agent message and thought chunks through *callback*, resolve agent
    permission prompts (auto-allow/auto-reject or *on_permission_prompt*),
    and service synchronous ``fs/*`` requests against *workspace_root*. The
    fs methods are permission-gated (kinds ``read_file`` / ``write_file``)
    and denied with a JSON-RPC error when not allowed. The callbacks are
    restored before returning.

    Args:
        conn: Active ``Connection`` to the agent.
        session_id: Session ID to send the prompt to.
        prompt: User prompt text.
        system_prompt: Optional system prompt prepended as a leading text
            content block.
        callback: Optional callable ``callback(text)`` invoked with each
            streamed chunk; when omitted, chunks are written to ``stdout``.
        callback_timeout: Idle timeout in seconds for the ``session/prompt``
            request; it resets on any agent output (streamed chunks, tool
            calls, notifications), so it only fires when the agent goes
            silent. Defaults to 60.
        workspace_root: Root directory for ``fs/*`` request validation.
            Defaults to the current working directory.
        permissions_config: Optional permission rules passed to
            ``resolve_permission``.
        mode: ``'one-shot'`` or ``'daemon'``. ``'daemon'`` defers unresolved
            permission prompts to *on_permission_prompt*.
        on_permission_prompt: Optional callable returning the chosen
            ``optionId`` (or ``None`` to cancel) for interactive permission
            prompts. Only used when ``mode == 'daemon'``.
        thoughts_mode: ``'enabled'`` (default) streams thoughts to *callback*
            as blockquotes; ``'console'`` writes thoughts to ``stderr``;
            ``'disabled'`` drops thoughts entirely.
        on_commands: Optional callable invoked with the command list from any
            ``available_commands_update`` notification seen while streaming.

    Returns:
        ``True`` if the prompt completed, or ``False`` on ``ACPError``,
        timeout, or connection close.
    """
    acp_log('rpc', f'send_prompt_and_stream: sid={session_id}, mode={mode}, timeout={callback_timeout}')

    ws_root = workspace_root or os.getcwd()
    thought_buf: list[str] = []

    def on_notification(method: str, params: dict) -> None:
        nonlocal thought_buf
        if method != 'session/update':
            return
        matched, commands = _extract_available_commands(method, params)
        if matched:
            acp_log('rpc', f'send_prompt_and_stream: available_commands_update ({len(commands or [])} commands)')
            if on_commands:
                on_commands(commands)
            return
        chunks = _handle_session_update_notification({'method': method, 'params': params})
        for chunk in chunks:
            if isinstance(chunk, tuple) and chunk[0] == '__thought__':
                if thoughts_mode != 'disabled':
                    thought_buf.append(chunk[1])
            else:
                had_thoughts = _flush_thoughts(thought_buf, thoughts_mode, callback)
                if thoughts_mode in ('enabled', 'console') and had_thoughts:
                    chunk = '\n' + chunk
                if callback:
                    callback(chunk)
                else:
                    sys.stdout.write(chunk)
                if not callback:
                    sys.stdout.flush()

    async def on_request(msg_id: int, method: str, params: dict) -> None:
        handled = await _handle_agent_request(
            conn, msg_id, method, params,
            ws_root, permissions_config, mode, on_permission_prompt,
        )
        if not handled and method == 'terminal/create':
            await conn.respond_with_error(msg_id, -32601, 'Terminal not supported')

    prompt_blocks: list[dict[str, str]] = []
    if system_prompt:
        prompt_blocks.append({'type': 'text', 'text': system_prompt})
    prompt_blocks.append({'type': 'text', 'text': prompt})

    async with conn.swap_callbacks(
        on_notification,
        _make_request_callback(on_request),
    ):
        try:
            result = await conn.send_request('session/prompt', {
                'sessionId': session_id,
                'prompt': prompt_blocks,
            }, timeout=callback_timeout)
        except ACPError as exc:
            acp_log('rpc', f'send_prompt_and_stream: ACPError: {exc}')
            if callback:
                callback(f'**[Agent error]:** {exc}')
            else:
                sys.stdout.write(f'**[Agent error]:** {exc}\n')
                sys.stdout.flush()
            return False
        except asyncio.TimeoutError:
            acp_log('rpc', f'send_prompt_and_stream: timeout after {callback_timeout}s of inactivity')
            if callback:
                callback(f'*[Response stream timed out after {callback_timeout}s of inactivity]*')
            with contextlib.suppress(Exception):
                await conn.send_notification('session/cancel', {'sessionId': session_id})
                await conn.send_notification('$/cancel_request', {'requestId': conn.last_request_id})
            return False
        except ConnectionError as exc:
            acp_log('rpc', f'send_prompt_and_stream: agent closed connection: {exc}')
            return False
        except asyncio.CancelledError:
            acp_log('rpc', 'send_prompt_and_stream: prompt cancelled')
            return False

    flushed = ''
    if thoughts_mode == 'enabled':
        flushed = _flush_thought_buffer(thought_buf)
    elif thoughts_mode == 'console':
        flushed = ''.join(thought_buf)
        thought_buf.clear()
        if flushed:
            sys.stderr.write(flushed + '\n')
            sys.stderr.flush()
    texts = _handle_prompt_result({'result': result})
    all_text = '\n'.join(texts)
    if thoughts_mode == 'enabled' and flushed and all_text:
        all_text = flushed + '\n' + all_text
    elif thoughts_mode == 'enabled' and flushed:
        all_text = flushed
    if all_text:
        if callback:
            callback(all_text)
        else:
            sys.stdout.write(all_text)
            sys.stdout.flush()

    acp_log('rpc', 'send_prompt_and_stream: done')
    return True


async def acp(
    cmd: list[str],
    prompt: str,
    model: str | None = None,
    system_prompt: str | None = None,
    env: dict | None = None,
    callback: Any | None = None,
    callback_timeout: float = 60.0,
    session_id: str | None = None,
    cwd: str | None = None,
    permissions_config: dict | None = None,
    auth: bool | None = None,
    thoughts_mode: str = 'enabled',
) -> tuple[str | None, str, str | None]:
    """Run a one-shot ACP prompt.

    Spawns the agent, runs the init phase (resuming *session_id* when
    provided), streams the prompt response through *callback*, and cleans
    up the subprocess.

    Args:
        cmd: Agent command list (executable path followed by arguments).
        prompt: User prompt text.
        model: Optional model name set via ``session/set_config_option``.
        system_prompt: Optional system prompt prepended as a text content block.
        env: Optional environment variables layered over the current process env.
        callback: Optional callable ``callback(text)`` for streamed response
            chunks; when omitted chunks go to ``stdout``.
        callback_timeout: Idle timeout in seconds for the prompt request;
            it resets on any agent output and only fires on agent silence.
            Defaults to 60.
        session_id: Optional existing session ID to resume or load.
        cwd: Optional working directory for the agent subprocess.
        permissions_config: Optional permission rules for auto-allow /
            auto-reject handling.
        auth: Override authentication behavior; see :func:`spawn_and_init`.
        thoughts_mode: ``'enabled'`` (default) streams thoughts to *callback*
            as blockquotes; ``'console'`` writes thoughts to ``stderr``;
            ``'disabled'`` drops thoughts entirely.

    Returns:
        ``(session_id, status, session_error)`` where *status* is one of
        ``STATUS_NEW``, ``STATUS_RESUMED``, ``STATUS_LOADED``, or
        ``STATUS_ERROR``.  *session_error* is ``None`` on success, or a
        human-readable string when resume/load failed.
        ``(None, STATUS_ERROR, None)`` indicates a spawn or init failure.
    """
    result = await spawn_and_init(cmd, env, model, session_id, cwd,
                                  permissions_config=permissions_config,
                                  auth=auth)
    if result is None:
        return None, STATUS_ERROR, None

    proc, conn, init_result = result
    try:
        sid = init_result['session_id']
        opened_via = init_result['opened_via']
        session_error = init_result.get('session_error')

        await send_prompt_and_stream(
            conn, sid, prompt, system_prompt,
            callback=callback, callback_timeout=callback_timeout,
            workspace_root=cwd,
            permissions_config=permissions_config,
            thoughts_mode=thoughts_mode,
        )

        status = opened_via if opened_via in (STATUS_NEW, STATUS_RESUMED, STATUS_LOADED) else STATUS_NEW
        return sid, status, session_error
    finally:
        await _close_connection(proc, conn)
