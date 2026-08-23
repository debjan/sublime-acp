#!/usr/bin/env python3

"""Thin CLI entry point for the ACP JSON-RPC transport."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

src = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(src))
from modules import rpc as _rpc

list_capabilities = _rpc.list_capabilities
list_config = _rpc.list_config
acp = _rpc.acp
STATUS_ERROR = _rpc.STATUS_ERROR


def _format_capabilities(caps: Any, indent: int = 0) -> str:
    prefix = '  ' * indent
    lines: list[str] = []
    if isinstance(caps, dict):
        for key, value in caps.items():
            if key.startswith('_'):
                continue
            if isinstance(value, bool):
                lines.append(f'{prefix}{key}: {"yes" if value else "no"}')
            elif isinstance(value, dict):
                if value:
                    lines.append(f'{prefix}{key}:')
                    lines.append(_format_capabilities(value, indent + 1))
                else:
                    lines.append(f'{prefix}{key}: yes')
            elif value is None:
                lines.append(f'{prefix}{key}: no')
            else:
                lines.append(f'{prefix}{key}: {value}')
    return '\n'.join(lines)


def _format_config_options(config_options: list) -> str:
    lines = ['Config Options:']
    for opt in config_options:
        name = opt.get('name', opt['id'])
        cat = opt.get('category')
        line = f'  {name} ({opt["id"]})'
        if cat:
            line += f' [{cat}]'
        lines.append(line)
        lines.append(f'    type: {opt.get("type")}')
        lines.append(f'    currentValue: {opt.get("currentValue")}')
        for v in opt.get('options', []):
            label = v.get('name', v['value'])
            desc = v.get('description')
            lines.append(f'    - {label}: {desc}' if desc else f'    - {label}')
    return '\n'.join(lines)


def _format_commands(commands: list) -> str:
    lines = ['Available Slash Commands:']
    for c in commands:
        name = c.get('name', '?')
        lines.append(name)
    return '\n/'.join(lines)


async def print_capabilities(cmd: list[str]) -> None:
    """Fetch and print agent capabilities to stdout."""
    msg = await list_capabilities(cmd)
    if not msg:
        return
    result = msg.get('result', {})
    agent_info = result.get('agentInfo', {})
    name, ver = agent_info.get('name', 'unknown'), agent_info.get('version', '?')
    title = agent_info.get('title')
    print(f'Agent: {name} ({title}) v{ver}' if title else f'Agent: {name} v{ver}')
    print(f'Protocol Version: {result.get("protocolVersion")}')
    print('\nCapabilities:')
    print(_format_capabilities(result.get('agentCapabilities', {}), 1))


async def print_config_options(cmd: list[str]) -> None:
    """Fetch and print agent config options to stdout."""
    config = await list_config(cmd)
    config_options = config.get('config_options') if config else None
    if isinstance(config_options, list):
        print(_format_config_options(config_options))
    else:
        print('Agent did not advertise config options')


async def print_commands(cmd: list[str]) -> None:
    """Fetch and print agent slash commands to stdout."""
    config = await list_config(cmd)
    commands = config.get('commands') if config else None
    if commands is None:
        print('Agent did not advertise slash commands')
    elif isinstance(commands, list) and not commands:
        print('Agent advertised no slash commands (empty list)')
    elif isinstance(commands, list):
        print(_format_commands(commands))


if __name__ == '__main__':
    import argparse
    import shutil

    examples = """
Examples:
  python cli/rpc.py claude-agent-acp -- "hi there"
  python cli/rpc.py claude-agent-acp -ls
  python cli/rpc.py claude-agent-acp -c <SID> -- "follow up"
"""
    raw_args = sys.argv[1:]
    if '--' in raw_args:
        idx = raw_args.index('--')
        pre_args, prompt_text = raw_args[:idx], ' '.join(raw_args[idx + 1:])
    else:
        pre_args, prompt_text = raw_args, None

    parser = argparse.ArgumentParser(
        description='One-shot ACP JSON-RPC prompt client',
        epilog=examples,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('-lc', '--capabilities', action='store_true',
                        help='List agent capabilities and exit')
    parser.add_argument('-ls', '--commands', action='store_true',
                        help='List available slash commands and exit')
    parser.add_argument('-lo', '--config-options', action='store_true',
                        help='List session config options and exit')
    parser.add_argument('-c', '--continue', dest='continue_id', metavar='SESSION_ID',
                        default=None, help='Continue an existing session')
    parser.add_argument('-s', '--system-prompt', default=None,
                        help='System prompt prepended as a text content block')
    parser.add_argument('-m', '--model', default=None,
                        help='Model to set via session/set_config_option')
    parser.add_argument('agent_cmd', nargs='+',
                        help='Agent command; options may be intermixed, prompt goes after --')
    if not pre_args:
        parser.print_help()
        sys.exit(1)

    args, extra = parser.parse_known_intermixed_args(pre_args)
    entered_cmd = args.agent_cmd + extra

    resolved = shutil.which(entered_cmd[0])
    if not resolved:
        print('Invalid command', entered_cmd[0], file=sys.stderr)
        sys.exit(1)
    agent_cmd = [resolved] + entered_cmd[1:]

    if args.capabilities:
        asyncio.run(print_capabilities(cmd=agent_cmd))
        sys.exit(0)

    if args.config_options:
        asyncio.run(print_config_options(cmd=agent_cmd))
        sys.exit(0)

    if args.commands:
        asyncio.run(print_commands(cmd=agent_cmd))
        sys.exit(0)

    if prompt_text is None:
        prompt = sys.stdin.read().strip()
    else:
        prompt = prompt_text

    if not prompt:
        print('No prompt provided', file=sys.stderr)
        sys.exit(1)

    sid, status, _ = asyncio.run(acp(
        cmd=agent_cmd,
        prompt=prompt,
        model=args.model,
        system_prompt=args.system_prompt,
        session_id=args.continue_id,
    ))
    if sid is not None and status != STATUS_ERROR:
        hint = f'Follow up with: python cli/rpc.py -c {sid} {" ".join(entered_cmd)} -- "...'
        print(f'\n\n---\n\n{hint}\n', file=sys.stderr)
