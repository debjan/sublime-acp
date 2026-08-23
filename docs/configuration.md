# Configuration

ACP is configured through `acp.sublime-settings` (**Preferences → Package Settings → ACP → Settings**). All settings are optional - sensible defaults come from `modules/config.py`.

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

| Setting            | Default     | Purpose                                                      |
| ------------------ | ----------- | ------------------------------------------------------------ |
| `system_prompt`    | `null`      | Custom system prompt passed to the agent                     |
| `attach_selection` | `false`     | Auto-attach current selection as context (`@path:line-line`) |
| `actions`          | (see below) | Custom quick actions shown in the prompt panel               |

`actions` entries appear as ready-made prompts for selected text via the command palette → *ACP: Actions*. The selected text is always embedded in an action prompt as a fenced code block, independent of the `attach_selection` setting:

```jsonc
"actions": [
    { "title": "Explain",  "prompt": "Explain in simple terms" },
    { "title": "Summarize","prompt": "Summarize with key points" }
]
```

### Timeouts

| Setting               | Default | Purpose                                                                                                                                                                              |
| --------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `timeout`             | `600`   | Seconds to wait for *any* agent output before cancelling a turn. The clock resets on every streamed text, thought, tool call, or file edit - long multi-edit turns won't be cut off. |
| `daemon_idle_timeout` | `3600`  | Seconds of inactivity before an idle daemon shuts down (`0` = never)                                                                                                                 |
| `cache_ttl`           | `300`   | Seconds before the `@`-completions file cache expires (`0` = always refresh)                                                                                                         |

### Thoughts

The `thoughts` setting controls how agent thinking chunks are surfaced:

| Value        | Behavior                                            |
| ------------ | --------------------------------------------------- |
| `"enabled"`  | Rendered in the output view as blockquotes          |
| `"console"`  | Sent to the Sublime console only; view shows reply  |
| `"disabled"` | Dropped entirely                                    |

### Permissions

Controls automatic approval/rejection of agent tool calls and host-filesystem operations (see [permissions.md](permissions.md)):

```jsonc
"permissions": {
    "auto_allow": ["read*"],
    "auto_reject": []
}
```

Patterns are fnmatch-style globs against tool kinds. `fs/read_text_file` maps to kind `read_file`; `fs/write_text_file` maps to kind `write_file`. Unmatched writes **prompt** in daemon mode but are **denied** in one-shot/CLI mode.

### Autocomplete file filtering

Extra ignore rules for `@` path completions, applied on top of your `.gitignore` files:

```jsonc
"ignore": {
    "folders": [".git", ".venv", "node_modules"],
    "extensions": [".pyc", ".so", ".exe"]
}
```

### Debugging

| Setting | Default | Purpose                                                    |
| ------- | ------- | ---------------------------------------------------------- |
| `debug` | `false` | Log agent stderr / JSON-RPC traffic to the Sublime console |

## Related docs

- [daemon.md](daemon.md) - daemon lifecycle governed by `timeout`/`daemon_idle_timeout`
- [permissions.md](permissions.md) - permission pipeline behind `auto_allow`/`auto_reject`
- [completions.md](completions.md) - completions behind `cache_ttl`/`ignore`
