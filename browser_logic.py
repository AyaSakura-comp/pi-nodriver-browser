import os
import shlex
import shutil


def resolve_browser_executable() -> str:
    configured = os.environ.get('PI_NODRIVER_CHROME')
    if configured:
        return configured
    for command in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'):
        executable = shutil.which(command)
        if executable:
            return executable
    raise RuntimeError('Chrome or Chromium was not found; set PI_NODRIVER_CHROME')


def parse_command(command: str) -> list[str]:
    parts = shlex.split(command)
    if not parts:
        raise ValueError('empty browser command')
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
        ):
            value = (item.get(key) or '').strip()
            if value:
                line += f' {label}="{_quoted(value[:500])}"'
        lines.append(line)
    return '\n'.join(lines) if lines else '(no interactive elements)'
