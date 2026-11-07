"""Images for research evidence: pick illustrative images while pages are crawled,
download them off the critical path, and list the finished ones as [[image: …]] markers
the answering agent can place next to the paragraphs they illustrate."""
import asyncio
import hashlib
import re

MAX_DELIVER, MAX_FETCH, PER_PAGE, MIN_SIDE = 3, 6, 2, 200
_SKIP_URL = re.compile(r'\.svg(\?|$)|^data:|sprite|logo|icon|avatar|favicon|pixel|spacer|blank\.', re.I)
_ROLE_SCORE = {'representative': 3.0, 'content': 2.0, 'gallery': 1.5}  # thumbnails and unknown roles are skipped
_HEADER = ('\n## Images (already downloaded; do not fetch). To illustrate a point, copy its [[image: …]] '
           'marker exactly onto its own line inside the paragraph it supports (at most 3); skip images that '
           'do not match the answer.\n')


def bigrams(text):  # keeps punctuation, unlike passages.bigrams: that is what image relevance was tuned with
    text = re.sub(r'\s+', '', text.lower())
    return {text[i:i + 2] for i in range(len(text) - 1)}


class ResearchImages:
    """Up to PER_PAGE candidates per page, MAX_FETCH downloads and MAX_DELIVER listed images.

    A finished download from a page whose passages are already in the evidence goes out at
    once through `on_ready` (append-only, so it is prefilled while crawling); the others
    wait for section() at the end. `fetch(url)` returns {'path', 'width', 'height'}."""

    def __init__(self, question, fetch, *, on_ready=None, is_delivered=None):
        self._question, self._fetch = bigrams(question), fetch
        self._on_ready, self._is_delivered = on_ready, is_delivered
        self._entries = []  # (source_id, title, candidate, relevance, download task)
        self._urls, self._digests, self._listed = set(), set(), set()
        self.delivered = 0

    def _relevance(self, title, candidate):
        described = bigrams(f"{candidate.get('caption') or ''} {candidate.get('alt') or ''}")
        overlap = len(self._question & described) / max(1, len(self._question))
        page = len(self._question & bigrams(title)) / max(1, len(self._question))
        return _ROLE_SCORE[candidate['role']] + 4 * overlap + page + min(candidate.get('score') or 0, 200) / 200

    def offer(self, source_id, title, candidates):
        """The image candidates of one crawled page; never blocks the crawl."""
        picked = []
        for candidate in candidates:
            url, width, height = candidate.get('url') or '', candidate.get('width') or 0, candidate.get('height') or 0
            if (url.startswith(('http://', 'https://')) and url not in self._urls and not _SKIP_URL.search(url)
                    and candidate.get('role') in _ROLE_SCORE and not (width and height and min(width, height) < MIN_SIDE)):
                picked.append((self._relevance(title, candidate), candidate))
        picked.sort(key=lambda item: -item[0])
        for relevance, candidate in picked[:PER_PAGE]:
            if len(self._entries) >= MAX_FETCH:
                return
            self._urls.add(candidate['url'])
            entry = (source_id, title, candidate, relevance, asyncio.create_task(self._fetch(candidate['url'])))
            entry[4].add_done_callback(lambda _task, entry=entry: self._finished(entry))
            self._entries.append(entry)

    def _line(self, entry):
        """The marker line of a finished, not yet listed, non-duplicate download, or None."""
        source_id, title, candidate, _, task = entry
        if not task.done() or task.cancelled() or task.exception() is not None or id(task) in self._listed:
            return None
        image = task.result() or {}
        if not image.get('path'):
            return None
        try:  # the same picture is often linked under several URLs
            with open(image['path'], 'rb') as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
        except OSError:
            return None
        if digest in self._digests:
            return None
        self._digests.add(digest)
        self._listed.add(id(task))
        self.delivered += 1
        description = candidate.get('caption') or candidate.get('alt') or title or ''
        size = f" ({image['width']}x{image['height']})" if image.get('width') and image.get('height') else ''
        return f"- [[image: {image['path']}]] [{source_id}] {description[:120]}{size}"

    def _finished(self, entry):
        if entry[4].cancelled() or entry[4].exception() is not None:
            return
        if self._on_ready is None or self.delivered >= MAX_DELIVER or not self._is_delivered(entry[0]):
            return  # page not used by the evidence (yet): keep it for section()
        line = self._line(entry)
        if line:
            self._on_ready(f'{_HEADER if self.delivered == 1 else ""}{line}\n')

    async def settle(self, timeout):
        """Give in-flight downloads at most `timeout` seconds once crawling is done."""
        pending = [entry[4] for entry in self._entries if not entry[4].done()]
        if pending and timeout > 0:
            await asyncio.wait(pending, timeout=timeout)

    def cancel(self):
        for entry in self._entries:
            entry[4].cancel()

    def section(self, delivered_sources=()):
        """Finished downloads not listed while crawling (pages with delivered passages first)."""
        done = sorted((entry for entry in self._entries if entry[4].done()),
                      key=lambda entry: (entry[0] not in delivered_sources, -entry[3]))
        lines = []
        for entry in done:
            if self.delivered >= MAX_DELIVER:
                break
            line = self._line(entry)
            if line:
                lines.append(line)
        if not lines:
            return ''
        return (_HEADER if self.delivered == len(lines) else '\n') + '\n'.join(lines) + '\n'
