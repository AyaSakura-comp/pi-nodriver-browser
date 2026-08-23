import os
import shlex
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class TabLimitError(ValueError):
    pass


@dataclass
class TabActivity:
    target_id: str
    page: object
    session_id: str
    kind: str
    created_at: float
    last_active_at: float


class TabActivityRegistry:
    def __init__(self, max_tabs: int = 20, clock: Callable[[], float] = time.monotonic):
        if max_tabs < 1:
            raise ValueError('max_tabs must be at least 1')
        self.max_tabs = max_tabs
        self.clock = clock
        self._records: dict[str, TabActivity] = {}

    @staticmethod
    def target_id(page: object) -> str:
        target = getattr(page, 'target', None)
        value = getattr(target, 'target_id', None)
        return str(value) if value else f'object-{id(page)}'

    def register(
        self,
        page: object,
        session_id: str,
        kind: str = 'page',
        *,
        last_active_at: float | None = None,
    ) -> TabActivity:
        target_id = self.target_id(page)
        now = self.clock() if last_active_at is None else last_active_at
        existing = self._records.get(target_id)
        record = TabActivity(
            target_id=target_id,
            page=page,
            session_id=session_id,
            kind=kind,
            created_at=existing.created_at if existing else now,
            last_active_at=now,
        )
        self._records[target_id] = record
        return record

    def touch(self, page: object) -> None:
        record = self._records.get(self.target_id(page))
        if record is not None:
            record.last_active_at = self.clock()

    def remove(self, page: object) -> TabActivity | None:
        return self._records.pop(self.target_id(page), None)

    def records(self) -> tuple[TabActivity, ...]:
        return tuple(self._records.values())

    def evictions_for_new_tabs(
        self,
        required: int = 1,
        *,
        protected_sessions: set[str] | None = None,
        protected_target_ids: set[str] | None = None,
    ) -> list[TabActivity]:
        overflow = len(self._records) + required - self.max_tabs
        if overflow <= 0:
            return []
        protected_sessions = protected_sessions or set()
        protected_target_ids = protected_target_ids or set()
        candidates = sorted(
            (
                record for record in self._records.values()
                if record.session_id not in protected_sessions
                and record.target_id not in protected_target_ids
            ),
            key=lambda record: (record.last_active_at, record.created_at, record.target_id),
        )
        if len(candidates) < overflow:
            raise TabLimitError(
                f'TAB_LIMIT: cannot open {required} new tab(s); all {self.max_tabs} tabs are active or protected'
            )
        return candidates[:overflow]


class OpenActionGuard:
    def __init__(self, limit: int = 2):
        self.limit = limit
        self._counts: dict[str, int] = {}

    def check(self, session_id: str, action: str) -> None:
        if action != 'open':
            self._counts.pop(session_id, None)
            return

        count = self._counts.get(session_id, 0) + 1
        self._counts[session_id] = count
        if count > self.limit:
            raise ValueError(
                f'OPEN_LOOP_GUARD: open has run {count} times consecutively in this session and is '
                f'blocked until a non-open browser action runs. Use the current page, crawl URLs in '
                f'one batch, or stop browsing instead of opening more pages.'
            )

    def clear(self, session_id: str) -> None:
        self._counts.pop(session_id, None)


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


def _compact(value: str, limit: int) -> str:
    value = value.strip()
    return value if len(value) <= limit else value[:limit] + '…'


def format_snapshot(elements: list[dict]) -> str:
    lines: list[str] = []
    for item in elements:
        ref = item.get('ref', '')
        tag = item.get('tag', 'element')
        text = _compact(item.get('text') or item.get('value') or '', 160)
        line = f'@{ref} <{tag}>'
        if text:
            line += f' "{_quoted(text)}"'
        for key, label in (
            ('ariaLabel', 'aria-label'),
            ('placeholder', 'placeholder'),
            ('href', 'href'),
            ('download', 'download'),
        ):
            value = _compact(item.get(key) or '', 160)
            if value:
                line += f' {label}="{_quoted(value)}"'
        lines.append(line)
    return '\n'.join(lines) if lines else '(no interactive elements)'
