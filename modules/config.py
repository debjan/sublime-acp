"""Shared constants and settings accessor for the ACP plugin."""

import sublime

_SETTINGS_FILE = 'ACP.sublime-settings'

def settings():
    """Return the (cached) ``Settings`` object for ``ACP.sublime-settings``."""
    return sublime.load_settings(_SETTINGS_FILE)


DEFAULT_TIMEOUT = 600              # agent command timeout (s)
CACHE_TTL_DEFAULT = 300            # file-walker cache expiry (s)
IDLE_TIMEOUT_DEFAULT = 900         # daemon idle shutdown timeout (s)
IDLE_TIMER_INTERVAL = 30           # idle-timeout check interval (s)
INPUT_VIEW_NAME = 'acp_input'      # prompt input panel view name
SPINNER_INTERVAL_MS = 250          # spinner frame interval (ms)
STATUS_KEY_DAEMON = 'acp_daemon'   # broadcast-status key for daemon state
STATUS_KEY_NOTIFY = 'acp_notify'   # transient notification key (auto-cleared)
MAX_HINT_LENGTH = 120              # max completion annotation chars (long hints break the popup)

DEFAULT_PERMISSIONS = {'auto_allow': ['read*'], 'auto_reject': []}  # default auto-allow / auto-reject rules

PROMPT = """
- Keep explanations brief and separate from code. Use fenced code blocks with language identifiers. Place explanation after the code block.
- Prioritize correctness, conciseness, and maintaining existing code style.
- If no single best answer exists, pick the most idiomatic solution and note trade-offs in one sentence.
- Always reply in well-formatted Markdown. Omit preamble, postamble, and polite pleasantries.
"""

ONE_SHOT_PROMPT = f"""
You are an expert coding assistant. The user invokes you from a code editor with a one-shot, non-interactive prompt.

Rules:

- Produce a single, complete, self-contained response. Never ask follow-up questions or request clarification.
- Never edit files. If modification is needed, return only the diff of changed block(s) with clear file path and line number annotations (e.g., `src/main.py:24-30`).
- If the user's input lacks context (missing file paths, ambiguous references), state your assumption and proceed - do not ask for confirmation.{PROMPT}
"""

SESSION_PROMPT = f"""
You are an expert coding assistant in an interactive chat session. The user will send multiple prompts in sequence.

Rules:

- Be concise but thorough. Ask follow-up questions when the user's intent is ambiguous or critical context is missing - otherwise proceed on your best interpretation.
- When suggesting code changes, always include the file path and line numbers in annotations (e.g., `src/main.py:24-30`). If the user wants file edits, return clear diffs.
- Proactively suggest follow-up directions or next steps when appropriate - the session is interactive and iterative.{PROMPT}
"""
