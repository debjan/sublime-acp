# Completions

The ACP prompt input panel offers two kinds of autocomplete, driven by the first character you type:

| Trigger | What you get                          | Source                             |
| ------- | ------------------------------------- | ---------------------------------- |
| `@`     | file and folder paths in your project | project file cache (file walker)   |
| `/`     | agent slash commands                  | agent metadata from initialization |

## Where the code lives

| File                     | Responsibility                                                          |
| ------------------------ | ----------------------------------------------------------------------- |
| `modules/completions.py` | `AcpFileCompletionListener` (`on_query_completions` and friends)        |
| `modules/file_walker.py` | project file enumeration + cache (see [permissions.md](permissions.md)) |
| `modules/commands.py`    | opens the input panel, stores slash commands on view settings           |

## How `@` path completions work

1. When a prompt panel opens, ACP warms the project file cache for the window (background thread - never on the UI thread).
2. As you type after `@`, completions come from that cache as relative paths.
3. The cache refreshes in the background; if it is stale or mid-refresh, the listener returns an async `CompletionList` and fills it when ready.
4. Saving any file expires the cache (`on_post_save_async`), so new files appear without restarting the session.

## How `/` slash command completions work

Slash commands are advertised by the agent during initialization (`session/available_commands`). ACP stores them on view settings when the input panel opens and offers them when you type `/`. Long hints are truncated to 120 characters (`MAX_HINT_LENGTH`) so the popup stays intact.

### Trigger detection quirk

Sublime breaks completion prefixes at word separators (`:`, `/`, ...). To cope, `_find_trigger_and_prefix()` scans the buffer backwards from the cursor to find the real `/` or `@` trigger and reconstructs the full prefix text.

## Related docs

- [permissions.md](permissions.md) - gitignore-aware file walking behind `@`
- [configuration.md](configuration.md) - `cache_ttl`, `ignore`, `actions`
