"""ACP Sublime Text plugin — thin facade."""

import os

from .modules import file_walker
from .modules.commands import (
    AcpActionsCommand,
    AcpCommand,
    AcpContinueSessionCommand,
    AcpInputCommand,
    AcpInterruptCommand,
    AcpStartCommand,
    AcpStopCommand,
    AcpSwitchModeCommand,
    AcpSwitchModelCommand,
)
from .modules.completions import AcpFileCompletionListener
from .modules.config import STATUS_KEY_DAEMON, settings
from .modules.daemon import _stop_daemon, _stop_idle_timer, stop_all_daemons


def plugin_loaded():
    """Configure debug mode on plugin load."""
    if settings().get('debug', False):
        os.environ['ACP_DEBUG'] = '1'
    else:
        os.environ.pop('ACP_DEBUG', None)


def _stop_all_daemons() -> None:
    """Stop every running daemon across all windows (used on plugin unload)."""
    stop_all_daemons(_stop_daemon)


def plugin_unloaded():
    """Stop all daemons and clean up on package disable/reload."""
    _stop_idle_timer()
    _stop_all_daemons()
    try:
        from .modules.broadcast import erase_broadcast_status
        erase_broadcast_status(STATUS_KEY_DAEMON)
    except Exception:
        pass
    file_walker.clear_all_caches()
