"""Bounded parallel search, with HTTP-only 4get and injectable Google bridge.

Provider selection is explicit. No implicit fallback, retries, external model
calls, or browser startup happen in this module.
"""
import asyncio
from dataclasses import dataclass
import math
from typing import AsyncIterator, Protocol
import aiohttp
from .budget import QueryBudget
from .contracts import SearchTask, SearchResult, natural


class ProviderError(RuntimeError):
    pass


class SearchProvider(Protocol):
    async def search(self, task: SearchTask) -> list[SearchResult]: ...


class FourgetProvider:
    """Direct loopback 4get client. Only the service port is configurable.

    Keeps full descriptions within the response safety limit rather than applying
    the legacy Markdown formatter's per-description truncation. Original URLs
    are retained exactly. Invalid candidates are not given URL authority.
    """
    def __init__(self, *, port: int = 8088, timeout: float = 10.0,
                 max_body_bytes: int = 5 * 1024 * 1024):
        natural(port, 'port', 1)
        if port > 65535:
            raise ValueError('invalid service port')
        natural(max_body_bytes, 'max_body_bytes', 1)
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('timeout must be finite and positive')
        self.url = f'http://127.0.0.1:{port}/api/v1/web'
        self.timeout = timeout
        self.max_body_bytes = max_body_bytes
        self._session = None
        self._closed = False

    async def close(self):
        self._closed = True
        if self._session:
            await self._session.close()

    async def search(self, task: SearchTask) -> list[SearchResult]:
        if task.provider != '4get':
            raise ValueError('4get adapter cannot search another provider')
        if self._closed:
            raise RuntimeError('provider closed')
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers={'Accept': 'application/json', 'User-Agent': 'pi-agent-web-search/1.0'},
                trust_env=False, cookie_jar=aiohttp.DummyCookieJar())
        try:
            async with self._session.get(self.url, params={'s': task.query, 'scraper': 'ddg', 'country': 'any'},
                                         allow_redirects=False) as response:
                if response.status != 200:
                    raise ProviderError(f'4get HTTP {response.status}')
                if response.content_length and response.content_length > self.max_body_bytes:
                    raise ProviderError('4get response exceeds byte limit')
                raw = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    if len(raw) + len(chunk) > self.max_body_bytes:
                        raise ProviderError('4get response exceeds byte limit')
                    raw.extend(chunk)
                import json
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise ProviderError('4get returned invalid JSON') from None
                if not isinstance(data, dict) or not isinstance(data.get('web'), list):
                    raise ProviderError('4get response has no results array')
                results = []
                for item in data['web'][:100]:
                    if not isinstance(item, dict):
                        continue
                    try:
                        url = item.get('url')
                        if not isinstance(url, str) or len(url.encode('utf-8')) > 2048:
                            continue
                        title = item.get('title')
                        description = item.get('description')
                        result = SearchResult(title if isinstance(title, str) and title.strip() else 'No Title',
                                              url, description if isinstance(description, str) else 'No snippet available.')
                    except ValueError:
                        continue
                    results.append(result)
                    if len(results) == 10:
                        break
                return results
        except (aiohttp.ClientError, asyncio.TimeoutError):
            # Do not expose response bodies, credentials, or transport internals.
            raise ProviderError('4get network error or timeout') from None


@dataclass(frozen=True)
class SearchOutcome:
    task: SearchTask
    results: tuple[SearchResult, ...]
    error: str | None = None


class SearchBatch:
    """One job's dispatcher. Results arrive individually, not after a gather barrier.

    The owner calls run serially for BFS waves. A single run has bounded active
    tasks; closing/cancelling its async iterator cancels and joins all owned work.
    Providers own transport lifetime; the controller must close them separately.
    """
    def __init__(self, providers: dict[str, SearchProvider], budget: QueryBudget, *, concurrency: int = 3):
        natural(concurrency, 'concurrency', 1)
        self.providers = dict(providers)
        self.budget = budget
        self.concurrency = concurrency
        self._seen = set()
        self._running = False

    async def _search(self, task: SearchTask) -> SearchOutcome:
        try:
            results = await self.providers[task.provider].search(task)
            if not isinstance(results, list) or not all(isinstance(r, SearchResult) for r in results):
                raise ValueError('invalid provider result')
            return SearchOutcome(task, tuple(results))
        except Exception as exc:
            # Cancellation is BaseException and deliberately propagates.
            return SearchOutcome(task, (), type(exc).__name__)

    async def run(self, tasks: list[SearchTask], *, allow_repeated_queries: bool = False) -> AsyncIterator[SearchOutcome]:
        if self._running:
            raise RuntimeError('one SearchBatch run at a time')
        ids = set()
        for task in tasks:
            if not isinstance(task, SearchTask) or task.provider not in self.providers:
                raise ValueError('unsupported provider/task')
            if task.task_id in ids:
                raise ValueError('duplicate attempt ID')
            ids.add(task.task_id)
        self._running = True
        pending = set()
        remaining = iter(tasks)
        exhausted = False
        try:
            while True:
                while not exhausted and len(pending) < self.concurrency and self.budget.remaining:
                    try:
                        task = next(remaining)
                    except StopIteration:
                        exhausted = True
                        break
                    if task.query_key in self._seen and not allow_repeated_queries:
                        continue
                    if not await self.budget.reserve(task.task_id):
                        continue
                    self._seen.add(task.query_key)
                    # Reservation and task creation have no intervening await.
                    pending.add(asyncio.create_task(self._search(task), name=f'search:{task.task_id}'))
                if not pending:
                    break
                done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for future in done:
                    pending.remove(future)
                    yield future.result()
        finally:
            for future in pending:
                future.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            self._running = False
