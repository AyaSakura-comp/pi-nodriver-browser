"""Validated, transport-neutral research contracts (schema version 1)."""
from dataclasses import dataclass
import math
import re
from urllib.parse import urlsplit

PROVIDERS = frozenset({'4get', 'google'})
TERMINAL_STATES = frozenset({'collected', 'sufficient', 'incomplete', 'cancelled', 'failed'})


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value):
        raise ValueError('invalid identifier')
    return value


def network_url(value: str) -> str:
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError('invalid URL')
    try:
        u = urlsplit(value)
        if u.scheme not in ('http', 'https') or not u.hostname or u.username is not None or u.password is not None:
            raise ValueError('URL must be HTTP(S) without credentials')
        _ = u.port
    except (ValueError, TypeError) as exc:
        raise ValueError('invalid network URL') from exc
    return value


def natural(value: int, name: str, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f'{name} must be an integer >= {minimum}')


@dataclass(frozen=True)
class SearchTask:
    task_id: str
    topic_id: str
    query: str
    direction: str
    provider: str = '4get'
    depth: int = 0
    parent_topic_id: str | None = None
    addresses: tuple[str, ...] = ()

    def __post_init__(self):
        identifier(self.task_id); identifier(self.topic_id)
        if not isinstance(self.query, str) or not self.query.strip() or len(self.query) > 10000:
            raise ValueError('query must contain 1..10000 characters')
        if not isinstance(self.direction, str) or not self.direction.strip() or len(self.direction) > 256:
            raise ValueError('direction must contain 1..256 characters')
        if self.provider not in PROVIDERS:
            raise ValueError('unknown provider')
        natural(self.depth, 'depth')
        if (self.depth == 0) != (self.parent_topic_id is None):
            raise ValueError('only roots have no parent')
        if self.parent_topic_id is not None:
            identifier(self.parent_topic_id)
        if not isinstance(self.addresses, tuple) or any(not isinstance(a, str) or not a.strip() for a in self.addresses):
            raise ValueError('addresses must be a tuple of nonempty strings')

    @property
    def query_key(self) -> tuple[str, str]:
        return self.provider, ' '.join(self.query.casefold().split())


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    description: str
    truncated: bool = False

    def __post_init__(self):
        if type(self.truncated) is not bool:
            raise ValueError('invalid truncation flag')
        network_url(self.url)
        if not isinstance(self.title, str) or not self.title.strip() or not isinstance(self.description, str):
            raise ValueError('invalid result title/description')


@dataclass(frozen=True)
class Limits:
    search_budget: int = 6
    search_concurrency: int = 3
    laya_concurrency: int = 2
    crawl_concurrency: int = 2
    max_crawls: int = 6
    max_packet_bytes: int = 2_000_000
    drain_timeout: float = 30.0
    crawl_word_budget: int = 20000
    rank_batch_size: int = 4
    focus_concurrency: int = 1  # per job; the 0.6B gate is shared daemon-wide

    def __post_init__(self):
        natural(self.crawl_word_budget, 'crawl_word_budget', 1)
        natural(self.rank_batch_size, 'rank_batch_size', 1)
        if self.rank_batch_size > 16:
            raise ValueError('rank_batch_size must be <=16')
        for name in ('search_budget', 'max_crawls'):
            natural(getattr(self, name), name)
        for name in ('search_concurrency', 'laya_concurrency', 'crawl_concurrency', 'focus_concurrency', 'max_packet_bytes'):
            natural(getattr(self, name), name, 1)
        if isinstance(self.drain_timeout, bool) or not isinstance(self.drain_timeout, (float, int)) or not math.isfinite(self.drain_timeout) or self.drain_timeout <= 0:
            raise ValueError('drain_timeout must be finite and positive')
