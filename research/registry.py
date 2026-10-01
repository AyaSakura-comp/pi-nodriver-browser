"""Source identity and crawl state, owned by one controller/event loop.

Dedup identity is NOT navigation authority: source.url always retains an exact
provider-returned URL. Never navigate to the normalized key.
"""
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit
from .contracts import SearchResult, identifier, network_url, PROVIDERS


def url_key(url: str) -> str:
    u = urlsplit(network_url(url))
    return urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path, u.query, ''))


@dataclass
class Source:
    source_id: str
    url: str
    title: str
    status: str = 'seen'
    _topics: dict[str, None] = field(default_factory=dict, repr=False)
    discoveries: list[dict] = field(default_factory=list)

    @property
    def topic_ids(self) -> tuple[str, ...]:
        return tuple(self._topics)


class SourceRegistry:
    def __init__(self, job_id: str):
        self.job_id = identifier(job_id)
        self._sources: dict[str, Source] = {}
        self._urls: dict[str, str] = {}

    def get(self, source_id: str) -> Source:
        return self._sources[source_id]

    @property
    def sources(self) -> tuple[Source, ...]:
        return tuple(self._sources.values())

    def discover(self, result: SearchResult, topic_id: str, task_id: str, provider: str) -> tuple[Source, bool]:
        identifier(topic_id); identifier(task_id)
        if provider not in PROVIDERS:
            raise ValueError('unknown provider')
        key = url_key(result.url)
        fresh = key not in self._urls
        if fresh:
            sid = f's{len(self._sources) + 1}'
            self._urls[key] = sid
            self._sources[sid] = Source(sid, result.url, result.title)
        source = self._sources[self._urls[key]]
        source._topics[topic_id] = None
        discovery = dict(taskId=task_id, topicId=topic_id, provider=provider,
                         url=result.url, title=result.title, description=result.description,
                         truncated=result.truncated)
        if discovery not in source.discoveries:
            source.discoveries.append(discovery)
        return source, fresh

    def queue_crawl(self, source_id: str, *, retry: bool = False) -> bool:
        source = self.get(source_id)
        if source.status != 'seen' and not (retry and source.status == 'failed'):
            return False
        source.status = 'queued'
        return True

    def drop_queued(self, source_id: str) -> None:
        source = self.get(source_id)
        if source.status != 'queued':
            raise ValueError('only queued crawls can be dropped')
        source.status = 'seen'

    def start_crawl(self, source_id: str) -> None:
        source = self.get(source_id)
        if source.status != 'queued':
            raise ValueError('crawl must be queued')
        source.status = 'fetching'

    def finish_crawl(self, source_id: str, *, success: bool) -> None:
        source = self.get(source_id)
        if source.status != 'fetching':
            raise ValueError('crawl must be fetching')
        source.status = 'completed' if success else 'failed'
