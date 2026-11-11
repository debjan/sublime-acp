"""OS notifications - Windows toast, macOS banner, Linux notify-send."""

from __future__ import annotations

import html
import sys
import threading
from pathlib import Path

import sublime

from ..protocol import acp_log

#: AppUserModelId registered under HKCU, so the Windows toast carries a display
#: name and icon instead of PowerShell's default identity.
APP_ID = 'Sublime.Text.ACP.Notifier'

#: Display name for the toast title and the Windows app id entry.
APP_NAME = 'Sublime Text - ACP'

#: Windows toast audio per notice kind (``ms-winsoundevent`` names). These are
#: played by the shell and stay audible even with the "No Sounds" scheme.
WIN_SOUNDS = {'turn_end': 'Default', 'prompt': 'Reminder'}

#: macOS alert sounds per notice kind, the closest match to the Windows pair.
MAC_SOUNDS = {'turn_end': 'default', 'prompt': 'Glass'}

#: Icon file names shipped in ``icons/``. Looked up by file name, so the
#: package folder (``ACP`` in a release, anything in a dev checkout) is
#: irrelevant.
ICON_ICO = 'sublime_text.ico'
ICON_PNG = 'sublime_text.png'

#: Seconds a notification helper may run before the worker gives up.
_NOTIFY_TIMEOUT_S = 15


def show(message, kind='turn_end') -> None:
    """Post an OS notification without blocking the caller.

    Safe from any thread, including the UI thread: the platform helper runs on
    a daemon worker and every failure is logged rather than raised. *kind*
    selects the sound, ``'turn_end'`` (default) or ``'prompt'``. The message
    is all the toast carries - the app name is the shell's attribution line,
    so repeating it in the body would only crowd the toast.
    """
    threading.Thread(target=_send, args=(message, kind), daemon=True).start()


def _send(message, kind) -> None:
    """Dispatch to the platform helper and log whether it succeeded."""
    if sys.platform == 'win32':
        delivered = _toast_win(message, WIN_SOUNDS.get(kind, 'Default'))
    elif sys.platform == 'darwin':
        delivered = _banner_mac(message, MAC_SOUNDS.get(kind, 'default'))
    else:
        delivered = _notify_send(message)
    acp_log('notify', f'{kind} notification delivered={delivered}: {message}')


def _toast_win(message, sound) -> bool:
    """Show a Windows toast with *sound* and the shipped icon.

    Goes through the WinRT toast API exposed to ``powershell`` - no extra
    dependency. The toast XML and the enclosing PowerShell literal are escaped
    here because agent names and project folders end up in this text.
    """
    _register_app_id(_icon_path(ICON_ICO))
    script = (
        '[Windows.UI.Notifications.ToastNotificationManager, '
        'Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; '
        '[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, '
        'ContentType = WindowsRuntime] > $null; '
        '$xml = [Windows.Data.Xml.Dom.XmlDocument]::new(); '
        f"$xml.LoadXml('{_ps_literal(_toast_xml(message, sound))}'); "
        '$toast = [Windows.UI.Notifications.ToastNotification]::new($xml); '
        '[Windows.UI.Notifications.ToastNotificationManager]'
        f"::CreateToastNotifier('{APP_ID}').Show($toast);"
    )
    return _run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script])


def _toast_xml(message, sound) -> str:
    """Return the escaped ToastGeneric document for *message*.

    No title node: the shell already prints the app name above the body, so a
    title would duplicate it.
    """
    audio = f'<audio src="ms-winsoundevent:Notification.{sound}"/>'
    return (
        '<toast><visual><binding template="ToastGeneric">'
        f'<text>{_xml_text(message)}</text>'
        f'</binding></visual>{audio}</toast>'
    )


def _xml_text(value) -> str:
    """Escape *value* for a toast XML text node."""
    return html.escape(str(value), quote=False)


def _ps_literal(value) -> str:
    """Escape *value* for a PowerShell single-quoted string literal."""
    return str(value).replace("'", "''")


def _register_app_id(icon) -> bool:
    """Create or refresh the HKCU AppUserModelId entry behind the toast.

    Idempotent and cheap; re-running it also repairs the icon path after the
    Sublime cache was cleared. False means the registry was not writable - the
    toast itself may still appear, just without the custom name and icon.
    """
    try:
        import winreg
    except Exception as exc:
        acp_log('notify', f'winreg unavailable: {exc!r}')
        return False
    key_path = 'Software\\Classes\\AppUserModelId\\' + APP_ID
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValueEx(key, 'DisplayName', 0, winreg.REG_SZ, APP_NAME)
            if icon:
                winreg.SetValueEx(key, 'IconUri', 0, winreg.REG_SZ, icon)
    except Exception as exc:
        acp_log('notify', f'AppUserModelId registration failed: {exc!r}')
        return False
    return True


def _icon_path(filename):
    """Return a filesystem path to a shipped icon, or ``None`` when missing.

    ``IconUri`` and ``notify-send --icon`` need a real path, but an installed
    package may live inside a ``.sublime-package`` archive, so the resource is
    copied into the plugin cache and refreshed when its bytes change.
    """
    resource = _icon_resource(filename)
    if resource is None:
        return None
    target = Path(sublime.cache_path()) / 'ACP' / filename
    try:
        data = sublime.load_binary_resource(resource)
        if not target.exists() or target.read_bytes() != data:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    except Exception as exc:
        acp_log('notify', f'icon extraction failed for {filename!r}: {exc!r}')
        return None
    return str(target)


def _icon_resource(filename):
    """Return the resource name of a shipped ``icons/`` file, or ``None``.

    ``find_resources`` matches by file name, but its exact pattern semantics
    differ between builds, so the bare name and two path patterns are tried
    and a match inside ``icons/`` wins over any same-named file elsewhere.
    """
    for pattern in (filename, 'icons/' + filename, '*/icons/' + filename):
        try:
            found = sublime.find_resources(pattern)
        except Exception as exc:
            acp_log('notify', f'icon lookup failed for {pattern!r}: {exc!r}')
            continue
        ours = [name for name in found if name.endswith('/icons/' + filename)]
        if ours:
            return ours[0]
        if found:
            return found[0]
    acp_log('notify', f'icon {filename!r} is not shipped')
    return None


def _banner_mac(message, sound) -> bool:
    """Post a Notification Center banner that plays the *sound* alert.

    A banner always needs a title, so the app name is repeated there; the
    project label in *message* keeps it distinguishable from other windows.
    """
    script = (
        f'display notification {_applescript(message)}'
        f' with title {_applescript(APP_NAME)}'
        f' sound name {_applescript(sound)}'
    )
    return _run(['osascript', '-e', script])


def _applescript(value) -> str:
    """Quote *value* for an AppleScript string literal."""
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def _notify_send(message) -> bool:
    """Post a desktop notification through ``notify-send`` when installed.

    The desktop plays its own notification sound, so no sound helper is
    spawned; the icon and app name mirror the Windows toast. Older libnotify
    builds without ``--app-name``/``--urgency`` get a plain retry.
    """
    import shutil

    if shutil.which('notify-send') is None:
        acp_log('notify', 'notify-send is not installed')
        return False
    command = ['notify-send', '--app-name', APP_NAME, '--urgency=normal']
    icon = _icon_path(ICON_PNG)
    if icon:
        command += ['--icon', icon]
    if _run([*command, APP_NAME, message]):
        return True
    return _run(['notify-send', APP_NAME, message])


def _run(command) -> bool:
    """Run *command* hidden, with a timeout, and log any failure.

    Every notification helper is spawned with ``CREATE_NO_WINDOW`` on Windows
    so no console flashes, and with a timeout so a hung desktop service cannot
    pin the worker thread. True only for a clean exit.
    """
    import subprocess

    try:
        result = subprocess.run(
            command, capture_output=True, check=False,
            timeout=_NOTIFY_TIMEOUT_S, **_hidden_window_kwargs(),
        )
    except Exception as exc:
        acp_log('notify', f'{Path(command[0]).name} failed: {exc!r}')
        return False
    if result.returncode:
        stderr = (result.stderr or b'').decode('utf-8', 'replace').strip()
        acp_log(
            'notify',
            f'{Path(command[0]).name} exited {result.returncode}: {stderr[:300]}',
        )
        return False
    return True


def _hidden_window_kwargs() -> dict:
    """Return Windows-only ``subprocess`` kwargs that keep console windows hidden."""
    import subprocess

    if sys.platform == 'win32':
        return {'creationflags': subprocess.CREATE_NO_WINDOW}
    return {}
