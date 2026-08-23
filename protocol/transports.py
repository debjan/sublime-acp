"""ACP agent subprocess lifecycle management.

Provides the ``SubprocessTransport`` class for spawning and managing ACP
agent subprocesses, along with convenience functions for subprocess
cleanup and stream writer management.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
from typing import Any

from .log import acp_log


class AgentSpawnError(Exception):
    """Raised when the agent binary cannot be found or spawned."""

    def __init__(self, cmd: str) -> None:
        """Initialize the AgentSpawnError.

        Args:
            cmd: The command string that could not be found.
        """
        super().__init__(f'Agent binary not found: {cmd}')
        self.cmd = cmd


class SubprocessTransport:
    """Manages the lifecycle of an ACP agent subprocess."""

    def __init__(
        self,
        cmd: str,
        args: list | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        """Initialize the subprocess transport.

        Args:
            cmd: Path or name of the agent binary.
            args: Additional command-line arguments for the agent.
            env: Environment variables for the subprocess.
            cwd: Working directory for the subprocess.
        """
        self._cmd = cmd
        self._args = args or []
        self._env = env
        self._cwd = cwd
        self.proc: asyncio.subprocess.Process | None = None
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None

    async def spawn(self) -> tuple[asyncio.subprocess.Process, asyncio.StreamReader, asyncio.StreamWriter]:
        """Resolve command, spawn subprocess, return (proc, reader, writer)."""
        full_cmd = [self._cmd] + self._args
        acp_log('transports', f'spawning: {" ".join(full_cmd)}')

        extra_kwargs: dict[str, Any] = {}
        if sys.platform == 'win32':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            extra_kwargs['startupinfo'] = startupinfo
            extra_kwargs['creationflags'] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        else:
            extra_kwargs['start_new_session'] = True
        if self._env:
            extra_kwargs['env'] = self._env
        if self._cwd:
            extra_kwargs['cwd'] = self._cwd

        resolved = shutil.which(full_cmd[0])
        if not resolved:
            acp_log('transports', f'binary not found: {full_cmd[0]}')
            raise AgentSpawnError(full_cmd[0])
        full_cmd[0] = resolved

        proc = await asyncio.create_subprocess_exec(
            *full_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=1024 * 1024,
            **extra_kwargs,
        )
        acp_log('transports', f'subprocess spawned: pid={proc.pid}')
        self.proc = proc
        self.reader = proc.stdout
        self.writer = proc.stdin
        return proc, self.reader, self.writer  # ty:ignore[invalid-return-type]

    async def cleanup(self) -> None:
        """Gracefully shut down the subprocess and close its writer."""
        await _cleanup_proc_impl(self.proc, self.writer)


async def spawn_subprocess(
    cmd: list,
    env: dict | None = None,
    cwd: str | None = None,
) -> tuple[asyncio.subprocess.Process, asyncio.StreamReader, asyncio.StreamWriter]:
    """Convenience function: create a SubprocessTransport and spawn.

    Returns ``(proc, reader, writer)``.
    """
    transport = SubprocessTransport(cmd[0], cmd[1:], env, cwd)
    return await transport.spawn()


async def close_writer(writer: asyncio.StreamWriter) -> None:
    """Gracefully close a StreamWriter: write_eof (if supported), close, wait_closed.

    Errors are suppressed - used during shutdown where the writer may already
    be half-closed by the peer or torn down mid-flight. Two separate
    ``suppress`` blocks so a ``wait_closed`` failure doesn't mask the
    ``write_eof``/``close`` step that precedes it.
    """
    with contextlib.suppress(Exception):
        if hasattr(writer, 'write_eof'):
            writer.write_eof()
        writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


def signal_process_group(pid: int, kill: bool = True) -> None:
    """Signal the whole process tree rooted at *pid* (agent plus grandchildren).

    Agents are spawned as the leader of a new session/process group (see
    :meth:`SubprocessTransport.spawn`), so a single ``killpg`` (POSIX) or
    ``taskkill /T`` (Windows) reaches the agent and every tool subprocess it forked.
    """
    if sys.platform == 'win32':
        args = ['taskkill', '/T', '/PID', str(pid)]
        if kill:
            args.insert(1, '/F')
        creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        with contextlib.suppress(Exception):
            subprocess.run(args, capture_output=True, check=False, creationflags=creationflags)
        return
    try:
        pgid = os.getpgid(pid)
    except OSError:
        return
    sig = signal.SIGKILL if kill else signal.SIGTERM
    with contextlib.suppress(OSError):
        os.killpg(pgid, sig)


async def _cleanup_proc_impl(
    proc: asyncio.subprocess.Process | None,
    writer: asyncio.StreamWriter | None,
) -> None:
    """Core cleanup logic shared by SubprocessTransport.cleanup and cleanup_process."""
    proc_pid = proc.pid if proc else None
    acp_log('transports', f'_cleanup_proc_impl: pid={proc_pid}, proc={proc is not None}, writer={writer is not None}')

    if writer:
        acp_log('transports', '_cleanup_proc_impl: closing writer')
        await close_writer(writer)
        acp_log('transports', '_cleanup_proc_impl: writer closed')

    if proc is not None and proc.returncode is not None:
        acp_log('transports', f'_cleanup_proc_impl: process already exited (returncode={proc.returncode})')
        await asyncio.sleep(0.05)
        return

    if proc is not None:
        acp_log('transports', f'_cleanup_proc_impl: terminating process group pid={proc.pid} (returncode={proc.returncode})')
    try:
        if proc is not None:
            signal_process_group(proc.pid, kill=False)
            await asyncio.wait_for(proc.wait(), timeout=2.0)
            acp_log('transports', f'_cleanup_proc_impl: terminated, returncode={proc.returncode}')
    except asyncio.TimeoutError:
        acp_log('transports', f'_cleanup_proc_impl: terminate timed out (2s) for pid={proc_pid}')
    except Exception as e:
        acp_log('transports', f'_cleanup_proc_impl: error terminating process: {e}')

    if proc is not None and proc.returncode is None:
        acp_log('transports', f'_cleanup_proc_impl: killing process group pid={proc.pid}')
        with contextlib.suppress(Exception):
            signal_process_group(proc.pid, kill=True)
            await asyncio.wait_for(proc.wait(), timeout=1.0)
            acp_log('transports', f'_cleanup_proc_impl: killed, returncode={proc.returncode}')

    if sys.platform == 'win32':
        await asyncio.sleep(0.1)

    acp_log('transports', f'_cleanup_proc_impl: done (pid={proc_pid}, final returncode={proc.returncode if proc else None})')


async def cleanup_process(
    proc: asyncio.subprocess.Process,
    writer: asyncio.StreamWriter,
) -> None:
    """Convenience function to clean up a subprocess."""
    await _cleanup_proc_impl(proc, writer)
