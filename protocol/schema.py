"""JSON-RPC 2.0 message builders and validation.

Provides factory functions for constructing JSON-RPC 2.0 requests,
notifications, success responses, and error responses, along with
a basic message structure validator.
"""

from __future__ import annotations

from typing import Any

PROTOCOL_VERSION = 1


def validate_json_rpc(msg: dict[str, Any]) -> bool:
    """Validate a JSON-RPC 2.0 message structure."""
    if not isinstance(msg, dict):
        return False
    if msg.get('jsonrpc') != '2.0':
        return False
    if 'method' not in msg and 'id' not in msg:
        return False
    if 'id' in msg and 'method' not in msg:
        return ('result' in msg) != ('error' in msg)
    return True


def make_request(msg_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 request dict."""
    msg: dict[str, Any] = {'jsonrpc': '2.0', 'id': msg_id, 'method': method}
    if params is not None:
        msg['params'] = params
    return msg


def make_notification(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 notification (no ``id`` field)."""
    msg: dict[str, Any] = {'jsonrpc': '2.0', 'method': method}
    if params is not None:
        msg['params'] = params
    return msg


def make_success_response(msg_id: int, result: Any) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 success response dict."""
    return {'jsonrpc': '2.0', 'id': msg_id, 'result': result}


def make_error_response(msg_id: int, code: int, message: str, data: Any = None) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 error response dict."""
    msg: dict[str, Any] = {
        'jsonrpc': '2.0', 'id': msg_id,
        'error': {'code': code, 'message': message},
    }
    if data is not None:
        msg['error']['data'] = data
    return msg
