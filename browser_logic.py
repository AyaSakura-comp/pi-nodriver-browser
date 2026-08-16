import os
import shlex
import shutil
from pathlib import Path


def parse_devtools_active_port(content: str) -> int:
    port = int(content.splitlines()[0])
    if not 1 <= port <= 65535:
        raise ValueError('DevTools port is out of range')
    return port


def should_disable_sandbox() -> bool:
    return os.environ.get('PI_NODRIVER_NO_SANDBOX', '').lower() in {'1', 'true', 'yes'}


def resolve_profile_dir() -> Path:
    configured = os.environ.get('PI_NODRIVER_PROFILE')
    return Path(configured).expanduser() if configured else Path.home() / '.pi' / 'agent' / 'nodriver-profile'


def resolve_browser_executable() -> str:
    configured = os.environ.get('PI_NODRIVER_CHROME')
    if configured:
        return configured
    for command in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'):
        executable = shutil.which(command)
        if executable:
            return executable
    raise RuntimeError('Chrome or Chromium was not found; set PI_NODRIVER_CHROME')


def parse_dismiss_options(parts: list[str]) -> str:
    if parts[:2] != ['dismiss', 'overlays']:
        raise ValueError('usage: dismiss overlays [--cookies=accept|reject-optional|ignore]')
    policy = 'reject-optional'
    for option in parts[2:]:
        if option.startswith('--cookies='):
            policy = option.split('=', 1)[1]
        else:
            raise ValueError(f'unknown dismiss option: {option}')
    if policy not in {'accept', 'reject-optional', 'ignore'}:
        raise ValueError(f'unsupported cookie policy: {policy}')
    return policy


def parse_command(command: str) -> list[str]:
    parts = shlex.split(command)
    if not parts:
        raise ValueError('empty browser command')
    if any(token in {'&&', '||', ';', '|'} for token in parts):
        raise ValueError('run exactly one browser command per tool call; command chaining is not supported')
    return parts


def _quoted(value: str) -> str:
    return value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')


def format_snapshot(elements: list[dict]) -> str:
    lines: list[str] = []
    for item in elements:
        ref = item.get('ref', '')
        tag = item.get('tag', 'element')
        text = (item.get('text') or item.get('value') or '').strip()
        line = f'@{ref} <{tag}>'
        if text:
            line += f' "{_quoted(text[:300])}"'
        for key, label in (
            ('ariaLabel', 'aria-label'),
            ('placeholder', 'placeholder'),
            ('href', 'href'),
            ('download', 'download'),
        ):
            value = (item.get(key) or '').strip()
            if value:
                line += f' {label}="{_quoted(value[:500])}"'
        lines.append(line)
    return '\n'.join(lines) if lines else '(no interactive elements)'
