"""Background image fetching for research: pick illustrative images while pages
are crawled, download them off the critical path, and list the finished ones so
the answering agent can embed them next to the paragraphs they illustrate."""
import asyncio
import hashlib
import re

_SKIP_URL = re.compile(r'\.svg(\?|$)|^data:|sprite|logo|icon|avatar|favicon|pixel|spacer|blank\.', re.I)
_ROLE_SCORE = {'representative': 3.0, 'content': 2.0, 'gallery': 1.5}


def _bigrams(text):
    text = re.sub(r'\s+', '', (text or '').lower())
    return {text[i:i + 2] for i in range(len(text) - 1)}


class ResearchImages:
    """Collects at most `per_page` candidates per crawled page, starts at most
    `max_fetch` downloads, and reports at most `max_deliver` finished ones."""

    def __init__(self, question, fetch, *, max_deliver=3, max_fetch=6, per_page=2, min_side=200):
        self._question = _bigrams(question)
        self._fetch = fetch
        self.max_deliver, self.max_fetch, self.per_page, self.min_side = max_deliver, max_fetch, per_page, min_side
        self._tasks = []  # (source_id, title, candidate, relevance, task)
        self._urls = set()

    def _relevance(self, title, candidate):
        described = _bigrams(f"{candidate.get('caption') or ''} {candidate.get('alt') or ''}")
        overlap = len(self._question & described) / max(1, len(self._question))
        page = len(self._question & _bigrams(title)) / max(1, len(self._question))
        return _ROLE_SCORE.get(candidate.get('role'), 0) + 4 * overlap + page + min(candidate.get('score') or 0, 200) / 200

    def offer(self, source_id, title, candidates):
        """Called once per crawled page; never blocks the crawl."""
        picked = []
        for candidate in candidates or ():
            url = candidate.get('url') or ''
            if not url.startswith(('http://', 'https://')) or url in self._urls or _SKIP_URL.search(url):
                continue
            if candidate.get('role') not in _ROLE_SCORE:
                continue  # thumbnails and unknown roles
            width, height = candidate.get('width') or 0, candidate.get('height') or 0
            if width and height and min(width, height) < self.min_side:
                continue
            picked.append((self._relevance(title, candidate), candidate))
        picked.sort(key=lambda item: -item[0])
        for relevance, candidate in picked[:self.per_page]:
            if len(self._tasks) >= self.max_fetch:
                return
            self._urls.add(candidate['url'])
            task = asyncio.create_task(self._fetch(candidate['url']))
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            self._tasks.append((source_id, title, candidate, relevance, task))

    async def settle(self, timeout):
        """Give in-flight downloads at most `timeout` seconds once crawling is done."""
        pending = [t for *_, t in self._tasks if not t.done()]
        if pending and timeout > 0:
            await asyncio.wait(pending, timeout=timeout)

    def cancel(self):
        for *_, task in self._tasks:
            if not task.done():
                task.cancel()

    def section(self, delivered_sources=()):
        """Markdown listing finished downloads, pages with delivered passages first."""
        done = []
        for source_id, title, candidate, relevance, task in self._tasks:
            if not task.done() or task.cancelled() or task.exception() is not None:
                continue
            image = task.result() or {}
            if not image.get('path'):
                continue
            done.append((source_id in delivered_sources, relevance, source_id, title, candidate, image))
        done.sort(key=lambda item: (not item[0], -item[1]))
        lines, digests = [], set()
        for _, _, source_id, title, candidate, image in done:
            if len(lines) >= self.max_deliver:
                break
            try:  # the same picture is often linked under several URLs
                with open(image['path'], 'rb') as handle:
                    digest = hashlib.sha256(handle.read()).hexdigest()
            except OSError:
                continue
            if digest in digests:
                continue
            digests.add(digest)
            description = candidate.get('caption') or candidate.get('alt') or title or ''
            size = f" ({image['width']}x{image['height']})" if image.get('width') and image.get('height') else ''
            lines.append(f"- [[image: {image['path']}]] [{source_id}] {description[:120]}{size}")
        if not lines:
            return ''
        return ('\n## Images (already downloaded; do not fetch)\n' + '\n'.join(lines) + '\n'
                'To illustrate a point, copy its [[image: …]] marker exactly onto its own line inside the '
                'paragraph it supports (at most 3). Skip images that do not match the answer.\n')
