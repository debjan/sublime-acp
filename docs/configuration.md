# Configuration

ACP is configured through `ACP.sublime-settings` (**Preferences -> Package Settings -> ACP -> Settings**). All settings are optional - sensible defaults come from `modules/config.py`.

## Settings reference

### Agents

```jsonc
"commands": [
    { "title": "Kiro", "cmd": "kiro-cli", "args": ["acp"] },
    { "title": "Pi",   "cmd": "pi-acp",   "env": { "PI_PERMISSION_LEVEL": "low" } },
    { "title": "Droid","cmd": "droid",    "args": ["exec", "--output-format", "acp"],
      "model": "custom:OpenRouter-0" }
]
```

Each entry launches one ACP agent:

| Key      | Purpose                                                     |
| -------- | ----------------------------------------------------------- |
| `title`  | Name shown in the agent-selection quick panel               |
| `cmd`    | Executable to spawn                                         |
| `args`   | Extra arguments (e.g. the `acp` subcommand)                 |
| `env`    | Extra environment variables for the subprocess              |
| `model`  | Model to request at session start                           |
| `auth`   | Set `false` to skip the authentication step                 |

With more than one entry, a quick panel lets you pick; a single entry is auto-selected.

### Prompts and context

| Setting            | Default     | Purpose                                                                                                                          |
| ------------------ | ----------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `system_prompt`    | `null`      | Custom system prompt passed to the agent                                                                                         |
| `attach_selection` | `false`     | Auto-attach current selection as context (`@path:line-line`)                                                                     |
| `context_usage`    | `true`      | Show context token usage in the status bar (e.g. `ctx 27% (53k/200k)`)                                                           |
| `git_turn_summary` | `"counts"`  | Turn-end git summary: `"diff"` (filenames + patches), `"counts"` (filenames + change counts), `false` (off). `true` = `"counts"` |
| `actions`          | (see below) | Custom quick actions shown in the prompt panel                                                                                   |

`actions` entries appear as ready-made prompts for selected text via the command palette -> *ACP: Actions*. The selected text is always embedded in an action prompt as a fenced code block, independent of the `attach_selection` setting:

```jsonc
"actions": [
    { "title": "Explain",  "prompt": "Explain in simple terms" },
    { "title": "Summarize","prompt": "Summarize with key points" }
]
```

### Timeouts

| Setting                     | Default | Purpose                                                                                                                                                                                                                                                  |
| --------------------------- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `daemon_idle_timeout`       | `900`   | Seconds of inactivity before an idle daemon shuts down (`0` = never)                                                                                                                                                                                     |
| `timeout`                   | `600`   | Seconds to wait for *any* agent output before cancelling a turn. The clock resets on every streamed text, thought, tool call, or file edit - long multi-edit turns won't be cut off.                                                                     |
| `permission_prompt_timeout` | `300`   | Hard cap in seconds for an open permission prompt before it is denied (`0` = wait forever). Prompts queue per window.                                                                                                                                    |
| `cache_ttl`                 | `300`   | Seconds before the `@`-completions file cache expires (`0` = always refresh)                                                                                                                                                                             |
| `session_list_limit`        | `10`    | Max sessions shown by *ACP: Switch Session* (`session/list`, filtered by cwd)                                                                                                                                                                            |
| `session_replay_on_switch`  | `false` | Replay full conversation history into the output view when switching sessions (via `session/load`). `false` = only show a "switched" notice. Thoughts/tool calls follow `thoughts`/`tool_calls`; view is cleared with a session header plus turn divider |

### Thoughts

The `thoughts` setting controls how agent thinking chunks are surfaced:

| Value        | Behavior                                   |
| ------------ | ------------------------------------------ |
| `"enabled"`  | Rendered in the output view as blockquotes |
| `"disabled"` | Dropped entirely                           |

### Tool calls

The `tool_calls` setting controls whether agent tool call results are surfaced in the output view:

| Value        | Behavior                                                              |
| ------------ | --------------------------------------------------------------------- |
| `"enabled"`  | Each completed/failed tool call renders as a one-line markdown bullet |
| `"disabled"` | Tool call results are not rendered                                    |

Bullets stream inline as the agent reports each tool finishing, for example `` - ✓ **read** `Read modules/rpc.py` ``. Failed calls use `✗` and append a one-line error summary. Non-terminal statuses are not shown.

### Output view

| Setting     | Default | Purpose                                                                              |
| ----------- | ------- | ------------------------------------------------------------------------------------ |
| `font_size` | `null`  | Font size (points) for the ACP scratch output views; `null`/unset = global font size |

### Permissions

Controls automatic approval/rejection of agent tool calls and host-filesystem operations (see [permissions.md](permissions.md)):

```jsonc
"permissions": {
    "auto_allow": ["read*"],
    "auto_reject": []
}
```

Patterns are fnmatch-style globs against tool kinds. `fs/read_text_file` maps to kind `read_file`; `fs/write_text_file` maps to kind `write_file`. Unmatched writes **prompt** in daemon mode but are **denied** in one-shot mode.

### Autocomplete file filtering

Extra ignore rules for `@` path completions, applied on top of your `.gitignore` files:

```jsonc
"ignore": {
    "folders": [".git", ".venv", "node_modules"],
    "extensions": [".pyc", ".so", ".exe"]
}
```

### Debugging

| Setting | Default | Purpose                                                                                                                 |
| ------- | ------- | ----------------------------------------------------------------------------------------------------------------------- |
| `debug` | `false` | Log agent stderr / JSON-RPC traffic to the dedicated ACP Log output panel (`View > Output > ACP Log`; never auto-shown) |

## Related docs

- [daemon.md](daemon.md) - daemon lifecycle governed by `timeout`/`daemon_idle_timeout`
- [permissions.md](permissions.md) - permission pipeline behind `auto_allow`/`auto_reject`
- [completions.md](completions.md) - completions behind `cache_ttl`/`ignore`
