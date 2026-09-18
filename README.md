# ACP - Agent Client Protocol for Sublime Text

Use AI coding agents (Kiro, Opencode, Pi, Droid, Claude Code, Copilot, …) directly from Sublime Text via the [Agent Client Protocol](https://agentclientprotocol.com/).

![screenshot](img/screenshot.png)

## Installation

1. Clone or copy this package into your Sublime Text `Packages/` directory:

```shell
git clone https://github.com/debjan/sublime-acp ACP
```

2. Restart Sublime Text.

## Quick Start

### One-shot prompt

1. `Ctrl+Shift+P` -> **"ACP: Prompt"**
2. Pick an agent from the quick panel (skipped if only one is configured)
3. Type your prompt in the input panel - use `@` for file autocompletion, `/` for agent slash commands
4. Press Enter - the response streams into a new tab

### Persistent chat session (daemon)

1. `Ctrl+Shift+P` -> **"ACP: Start Session"**
2. Pick an agent - a spinner shows while the agent initializes
3. Once ready, a dedicated **"ACP Chat: Agent Name"** tab opens
4. Send prompts via **"ACP: Prompt"** - responses accumulate in the chat tab
5. Stop the session with **"ACP: Stop Session"**

The daemon auto-terminates after 15 minutes of inactivity (configurable).

### Continue a previous session

- `Ctrl+Shift+P` -> **"ACP: Continue Session"** - pick an agent, then pick from its recent sessions (via `session/list`, filtered by the current working directory, capped by `session_list_limit`). Agents without `session/list` support fall back to the last cached session.

### Walkthrough

For quick walkthrough visit [walkthrough.md](docs/walkthrough.md)

## Configuration

Edit `ACP.sublime-settings` (Preferences -> Package Settings -> ACP -> Settings):

```jsonc
{
  // Agents you want to use
  "commands": [
    { "title": "Claude Code", "cmd": "claude-agent-acp" },
    { "title": "Opencode", "cmd": "opencode", "args": ["acp"] },
  ],

  // Quick actions (palette: ACP: Actions, sent with selected text)
  "actions": [
    { "title": "Explain", "prompt": "Explain in simple terms" },
    { "title": "Summarize", "prompt": "Summarize with key points" },
  ],

// ...

}
```

For available options see [configuration.md](docs/configuration.md).

## Commands

| Palette Command               | Keybinding         | Description                                            |
| ----------------------------- | ------------------ | ------------------------------------------------------ |
| ACP: Start Session            | `Ctrl+Alt+A`       | Start a persistent agent daemon                        |
| ACP: Stop Session             | `Ctrl+Alt+Shift+A` | Terminate the running daemon                           |
| ACP: Send Prompt              | `Alt+Shift+A`      | One-shot prompt (or send to daemon)                    |
| ACP: Interrupt Current Prompt | `Ctrl+Break`       | Cancel the in-flight prompt (daemon only)              |
| ACP: Continue Session         | -                  | Pick a recent session via `session/list` and resume it |
| ACP: Switch Model             | -                  | Change model mid-session (daemon only)                 |
| ACP: Switch Mode              | -                  | Change session mode mid-session (daemon only)          |
| ACP: Switch Thought Level     | -                  | Change reasoning effort mid-session (daemon only)      |

To enable keyboard shortcut open "Preferences -> Package Settings -> ACP -> Example Key Bindings".

### Switching model, mode, or thought level

While a daemon session is active, **ACP: Switch Model**, **ACP: Switch Mode**, and **ACP: Switch Thought Level** show a quick panel populated from the agent's advertised options. The current selection is marked with `✓`. These commands are only enabled when the active agent supports the corresponding option.

## Requirements

- Sublime Text 4+
- One or more ACP-compatible agents installed

## Documentation

Deeper dives live in [docs/index.md](docs/index.md): daemon architecture, permissions and the file walker, completions, and full settings reference.

## License

MIT
