# Error Codes

ACP is built on JSON-RPC 2.0, so every request/response error is a JSON-RPC error object with `code`, `message`, and optional `data`. This page lists the codes defined by the ACP protocol schema, plus the codes this plugin itself emits and how failures surface to the user.

## Where the code lives

| File                     | Responsibility                                                   |
| ------------------------ | ---------------------------------------------------------------- |
| `protocol/schema.py`     | JSON-RPC error/response validation and `make_error_response()`   |
| `protocol/connection.py` | Raises `ACPError` for error responses from the agent             |
| `modules/rpc.py`         | Maps `session/prompt` failures to a result status for the daemon |
| `modules/daemon.py`      | Recovers from a dropped session/connection, streams errors       |

## Protocol-defined codes

ACP adopts the JSON-RPC 2.0 standard codes and adds its own in the reserved range `-32000` to `-32099`.

| Code     | Name                    | Meaning                                                           |
| -------- | ----------------------- | ----------------------------------------------------------------- |
| `-32700` | Parse error             | Invalid JSON received by the agent (JSON-RPC standard)            |
| `-32600` | Invalid request         | The JSON sent is not a valid Request object (JSON-RPC standard)   |
| `-32601` | Method not found        | The method does not exist or is not available (JSON-RPC standard) |
| `-32602` | Invalid params          | Invalid method parameter(s) (JSON-RPC standard)                   |
| `-32603` | Internal error          | Implementation-defined server error (JSON-RPC standard)           |
| `-32800` | Request cancelled       | Execution aborted by cancellation, resource limits, or shutdown   |
| `-32000` | Authentication required | Authentication is required before this operation can be performed |
| `-32002` | Resource not found      | A resource, such as a file, was not found                         |
| other    | undefined               | Any other integer; agent-defined and non-standard                 |

## Codes emitted by this plugin

| Code     | Where                                       | When                                                                                             |
| -------- | ------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `-32000` | `modules/rpc.py` (`_check_fs_permission`)   | A read/write file operation was denied by the permission system                                  |
| `-32602` | `modules/rpc.py` (fs handlers)              | Invalid fs params (`line`/`limit` not a positive int), path outside workspace, or file not found |
| `-32601` | `modules/rpc.py` (`send_prompt_and_stream`) | The agent issued an unsupported request (e.g. `terminal/create`)                                 |

## How failures surface

`send_prompt_and_stream()` converts the outcome into a status string so callers can react differently to each case (`modules/rpc.py`):

| Status                     | Trigger                                                 |
| -------------------------- | ------------------------------------------------------- |
| `PROMPT_OK`                | The prompt completed normally                           |
| `PROMPT_SESSION_NOT_FOUND` | The agent's error message contained `session not found` |
| `PROMPT_ERROR`             | Any other `ACPError` from the agent                     |
| `PROMPT_TIMEOUT`           | No agent output within `callback_timeout` seconds       |
| `PROMPT_CONNECTION_CLOSED` | The agent subprocess closed the connection              |
| `PROMPT_CANCELLED`         | The prompt was cancelled (`session/cancel`)             |

The daemon's prompt loop (`modules/daemon.py`) treats `PROMPT_SESSION_NOT_FOUND` and `PROMPT_CONNECTION_CLOSED` as a stale session/process and automatically reconnects: it closes the dead connection, spawns a fresh subprocess, and resumes the same session id (falling back to a new session if resume fails). All other statuses are surfaced to the output view as-is and the daemon keeps running.
