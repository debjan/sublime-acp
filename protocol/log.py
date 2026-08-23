"""Shared debug logging helper for ACP packages."""

from __future__ import annotations

import os
import sys
from datetime import datetime

_TRUTHY = ('1', 'true', 'yes')


def acp_log(tag: str, msg: str) -> None:
    """Print a tagged debug line to stderr if ``ACP_DEBUG`` is enabled."""
    if os.environ.get('ACP_DEBUG', '').lower() in _TRUTHY:
        print(
            f'[{datetime.now().isoformat(timespec="seconds")}] [ACP:{tag}] {msg}',
            file=sys.stderr,
        )
