"""JSON-RPC 2.0 connection layer for ACP agent communication.

Provides the ``Connection`` class that manages request/response matching
via pending futures, dispatches incoming notifications and requests to
registered callbacks, and handles low-level I/O over asyncio streams.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any, AsyncIterator, Callable

from .log import acp_log
from .schema import (
    make_error_response,
    make_notification,
    make_request,
    make_success_response,
    validate_json_rpc,
)


class ACPError(Exception):
    """JSON-RPC error returned by the agent."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f'[{code}] {message}')


def _log_response_failure(task: asyncio.Task) -> None:
    """Log any unhandled exception from an error-response send task."""
    if task.cancelled():
        return
    if exc := task.exception():
        acp_log('connection', f'Failed to send error response: {exc}')


class Connection:
    """JSON-RPC 2.0 connection over newline-delimited stdio.

    Supports request/response matching via pending futures, and dispatches
    incoming notifications and requests to registered callbacks. The reader
    loop that consumes incoming messages is started automatically on
    construction, so callers do not need to schedule it themselves.

    Typical usage:
        conn = Connection(reader, writer)
        result = await conn.send_request('some_method', {...})
        ...
        await conn.close()
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,) -> None:
        """Initialize the JSON-RPC 2.0 connection.

        Starts the background reader task that dispatches incoming messages;
        it is cancelled automatically by :meth:`close`.

        Args:
            reader: Stream reader for receiving agent messages.
            writer: Stream writer for sending messages to the agent.
        """
        self._reader = reader
        self._writer = writer
        self._next_id = 1
        self._last_request_id: int | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = asyncio.ensure_future(self.read_loop())
        self._last_activity: float = asyncio.get_event_loop().time()

        # Callbacks for unsolicited messages from the agent
        self.notification_callback: Callable[[str, dict[str, Any]], Any] | None = None
        self.request_callback: Callable[[int, str, dict[str, Any]], Any] | None = None

    # Sending

    async def send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 60.0,
    ) -> Any:
        """Send a request and return the result (awaiting the response).

        Args:
            method: The method name to invoke on the agent.
            params: Optional parameters for the request.
            timeout: Maximum seconds of inactivity to wait for the response.
                The clock resets on every received message, so the request
                only times out if the agent goes silent for *timeout* seconds.
                Defaults to 60.0.

        Returns:
            The result field from the agent's response.

        Raises:
            asyncio.TimeoutError: If the response is not received within the
                inactivity window.
            ACPError: If the agent returns an error response.
        """
        msg_id = self._next_id
        self._next_id += 1
        self._last_request_id = msg_id

        future = asyncio.Future()
        self._pending[msg_id] = future

        await self._write_raw(make_request(msg_id, method, params))
        self._last_activity = asyncio.get_event_loop().time()

        loop = asyncio.get_event_loop()
        try:
            while True:
                remaining = timeout - (loop.time() - self._last_activity)
                if remaining <= 0:
                    raise asyncio.TimeoutError(
                        f'no response to {method!r} after {timeout:g}s of inactivity'
                    )
                done, _ = await asyncio.wait((future,), timeout=remaining)
                if future in done:
                    return future.result()
        except asyncio.TimeoutError:
            future.cancel()
            self._pending.pop(msg_id, None)
            raise

    async def cancel_pending_request(
        self, msg_id: int, session_id: str | None = None,
    ) -> bool:
        """Send session/cancel and $/cancel_request, then cancel a pending request.

        When *session_id* is given, first sends the ACP ``session/cancel``
        notification so the agent aborts the active prompt turn (it then
        responds to the original ``session/prompt`` with a ``cancelled`` stop
        reason). Then sends ``$/cancel_request`` and cancels the matching
        pending future so that ``send_request()`` unblocks. The connection
        stays alive, preserving the session.
        """
        future = self._pending.pop(msg_id, None)
        if future is None:
            return False
        if session_id:
            try:
                await self.send_notification('session/cancel', {'sessionId': session_id})
            except Exception as exc:
                acp_log('connection', f'Failed to send session/cancel: {exc}')
        await self.send_notification('$/cancel_request', {'requestId': msg_id})
        if not future.done():
            future.cancel()
        return True

    async def send_notification(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        """Send a JSON-RPC 2.0 notification (no response expected).

        Args:
            method: The method name to invoke on the agent.
            params: Optional parameters for the notification.
        """
        await self._write_raw(make_notification(method, params))

    async def respond_to_request(
        self,
        msg_id: int,
        result: Any,
    ) -> None:
        """Respond to an incoming request from the agent.

        Args:
            msg_id: The ID of the request being answered.
            result: The result data to send back.
        """
        await self._write_raw(make_success_response(msg_id, result))

    async def respond_with_error(
        self,
        msg_id: int,
        code: int,
        message: str,
        data: Any = None,
    ) -> None:
        """Respond to an incoming request with a JSON-RPC error.

        Args:
            msg_id: The ID of the request being answered.
            code: JSON-RPC error code.
            message: Human-readable error message.
            data: Optional additional error data.
        """
        await self._write_raw(make_error_response(msg_id, code, message, data))

    @property
    def next_id(self) -> int:
        """Return the next available message ID."""
        return self._next_id

    @property
    def last_request_id(self) -> int | None:
        """Return the ID of the most recently sent request, or ``None``."""
        return self._last_request_id

    @property
    def writer(self) -> asyncio.StreamWriter:
        """Return the underlying stream writer."""
        return self._writer

    # Callback management

    @contextlib.asynccontextmanager
    async def swap_callbacks(
        self,
        notification_callback: Callable[[str, dict[str, Any]], Any] | None,
        request_callback: Callable[[int, str, dict[str, Any]], Any] | None,
    ) -> AsyncIterator[None]:
        """Temporarily replace notification and request callbacks.

        Saves the current callbacks, installs *notification_callback* and
        *request_callback*, then restores the originals on exit.

        Yields to the event loop before restoring so that any trailing
        notifications already received by the reader task are dispatched
        through the temporary callbacks.
        """
        saved_notification = self.notification_callback
        saved_request = self.request_callback
        self.notification_callback = notification_callback
        self.request_callback = request_callback
        try:
            yield
        finally:
            await asyncio.sleep(0)
            self.notification_callback = saved_notification
            self.request_callback = saved_request

    # Receiving

    async def read_one(self) -> bool:
        """Read one message from the stream and dispatch it."""
        line = await self._reader.readline()
        if not line:
            return False

        raw = line.decode().strip()
        if not raw:
            return True

        self._last_activity = asyncio.get_event_loop().time()

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as exc:
            acp_log('connection', f'JSON decode error: {exc}')
            return True

        try:
            self._dispatch(msg)
        except ACPError as exc:
            acp_log('connection', f'Invalid JSON-RPC message: {exc}')
            return True

        return True

    async def read_loop(self) -> None:
        """Read messages until EOF. Run as a background task."""
        try:
            while True:
                ok = await self.read_one()
                if not ok:
                    break
            await self.close()
        finally:
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(ConnectionError('Agent closed connection'))
            self._pending.clear()

    def _dispatch(self, msg: dict[str, Any]) -> None:
        if not validate_json_rpc(msg):
            raise ACPError(-32700, 'Invalid JSON-RPC message')

        msg_id = msg.get('id')
        method = msg.get('method')
        has_error = 'error' in msg

        # Response to one of our pending requests
        if msg_id is not None and method is None:
            future = self._pending.pop(msg_id, None)
            if future is not None and not future.done():
                if has_error:
                    err = msg['error']
                    if not isinstance(err.get('code'), int) or not isinstance(err.get('message'), str):
                        future.set_exception(ACPError(-32603, 'Malformed error response from agent'))
                    else:
                        future.set_exception(ACPError(
                            err['code'], err['message'], err.get('data'),
                        ))
                else:
                    future.set_result(msg.get('result'))
            return

        # Incoming request from agent (has both id and method)
        if msg_id is not None and method is not None:
            cb = self.request_callback
            if cb is not None:
                try:
                    cb(msg_id, method, msg.get('params', {}))
                except Exception as exc:
                    acp_log('connection', f'Request callback failed: {exc}')
                    asyncio.ensure_future(
                        self.respond_with_error(msg_id, -32603, str(exc)),
                    ).add_done_callback(_log_response_failure)
            return

        # Notification from agent (no id, has method)
        if msg_id is None and method is not None:
            cb = self.notification_callback
            if cb is not None:
                try:
                    cb(method, msg.get('params', {}))
                except Exception as exc:
                    acp_log('connection', f'Notification callback failed: {exc}')
            return

    async def _write_raw(self, msg: dict[str, Any]) -> None:
        data = json.dumps(msg, separators=(',', ':'))
        self._writer.write((data + '\n').encode('utf-8'))
        await self._writer.drain()

    @property
    def reader_task(self) -> asyncio.Task | None:
        """Return the background reader task started by :meth:`__init__`."""
        return self._reader_task

    async def close(self) -> None:
        """Close the connection and cancel the reader task if not self-calling."""
        if (
            self._reader_task is not None
            and not self._reader_task.done()
            and asyncio.current_task() != self._reader_task
        ):
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception as e:
            acp_log('connection', f'error closing writer: {e}')
