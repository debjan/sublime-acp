"""ACP Sublime Text plugin - thin facade."""

import os

import sublime_plugin

from .modules import file_walker
from .modules.commands import (
    AcpActionsCommand,
    AcpCommand,
    AcpInputCommand,
    AcpInterruptCommand,
    AcpStartCommand,
    AcpStopCommand,
    AcpSwitchModeCommand,
    AcpSwitchModelCommand,
    AcpSwitchSessionCommand,
    AcpSwitchThoughtLevelCommand,
)
from .modules.completions import AcpFileCompletionListener
from .modules.config import STATUS_KEY_DAEMON, STATUS_KEY_USAGE, settings
from .modules.daemon import (
    _stop_daemon,
    _stop_idle_timer,
    clear_unload,
    request_unload,
    stop_all_daemons,
)
from .modules.debug_panel import AcpClearLogPanelCommand, AcpUpdateLogPanelCommand


def _apply_debug_setting() -> None:
    """Sync ``ACP_DEBUG`` env flag from settings (panel-only sink stays registered)."""
    if settings().get('debug', False):
        os.environ['ACP_DEBUG'] = '1'
    else:
        os.environ.pop('ACP_DEBUG', None)


def plugin_loaded():
    """Configure debug mode and clear unload state on plugin load."""
    clear_unload()
    from .modules.debug_panel import init_debug_panel

    init_debug_panel()
    _apply_debug_setting()
    settings().add_on_change('acp_debug', _apply_debug_setting)


def _stop_all_daemons() -> None:
    """Stop every running daemon across all windows (used on plugin unload)."""
    request_unload()
    stop_all_daemons(_stop_daemon, join_timeout=None)


def plugin_unloaded():
    """Stop all daemons and clean up on package disable/reload."""
    _stop_idle_timer()
    _stop_all_daemons()
    try:
        from .modules.debug_panel import clear_panel_cache

        clear_panel_cache()
    except Exception:
        pass
    try:
        from .modules.broadcast import erase_broadcast_status
        erase_broadcast_status(STATUS_KEY_DAEMON)
        erase_broadcast_status(STATUS_KEY_USAGE)
    except Exception:
        pass
    file_walker.clear_all_caches()


class AcpExitListener(sublime_plugin.EventListener):
    """Guard on hard-quit."""
    def on_exit(self) -> None:
        _stop_all_daemons()
