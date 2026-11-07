"""Research jobs behind the worker socket: the agent's queries -> 3 x 4get + 1 x Google ->
crawl the results in search order under a word budget -> evidence passages committed
while pages arrive (progress frames feed Pi's prefill) -> one packet with answer rules.

Only URLs a search engine returned are ever crawled. A job's tabs belong to its own
owner id and are closed when it ends, however it ends.
"""
import asyncio
from datetime import datetime
import hashlib
import heapq
import itertools
import json
from pathlib import Path
import re
import secrets
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .images import ResearchImages
from .passages import TOP, ProgressivePassages, model_tokens, rough_words
from .providers import fourget, google, resolve_google_redirect  # worker.py imports resolve_google_redirect from here

ENGINES = ('4get', '4get', '4get', 'google')  # engine of query index 0..3 (q1..q4)
MAX_SEARCHES = 8       # per job, however many queries a client streams in
TOP_PER_QUERY = 5      # crawl each query's first 5 new results; pages beyond them mostly missed the timeout
FETCH_TIMEOUT = 3.0    # per-page load deadline, as for interactive crawl
TASK_TIMEOUT = 30.0    # any one search or crawl, including a browser relaunch
# Searches and crawls reuse a browser liveness probe this recent (seconds). One CDP probe
# holds the lifecycle lock (~0.26 s), so probing per page serialized the first crawls. A
# job probes once in the background at start; a dead browser still surfaces as a failed page.
PROBE_MAX_AGE = 30.0
EVIDENCE_BUDGET = 6000  # rough tokens of committed passages
IMAGE_WAIT = 1.0        # how long finished crawling waits for in-flight image downloads
CLEANUP_TIMEOUT = 5.0
MAX_PACKET_BYTES, MAX_PACKET_LINES = 2 * 1024 * 1024, 20000
PARAMS = {'jobId', 'question', 'queries', 'clock', 'streaming', 'answer', 'crawlConcurrency', 'crawlWordBudget',
          'deadlineMs', 'evidenceBudget', 'imageWaitMs'}

# Answer rules 2 and 5 of the closing instructions. A new question gets the
# breadth-first overview; the depth rules apply once the user picks an item.
BREADTH_RULE = ('2. Breadth first: this is the overview, not the deep dive. Cover every distinct on-topic item the '
                'evidence has; for 推薦、有什麼、活動、行程、比較、清單 questions aim for the widest coverage (typically 6-12 '
                'items when the evidence has them), grouped by date or type when that helps. One or two lines per item: '
                'its name, the key facts that decide whether it is worth a look (時間、地點、價格/費用, or the one number '
                'that matters) and 一句亮點. Do not expand any single item into paragraphs, step lists or checklists yet. '
                'A narrow factual question gets the direct answer plus its key supporting facts. Roughly 500-1500 '
                'Chinese characters.\n')
DEPTH_RULE = ('2. Depth: the user asked to go deeper on a specific item or point. Answer that item in full: cut only '
              'off-topic and repeated content, never on-topic detail; keep every concrete fact (numbers, dates, prices, '
              'names, places, conditions) that bears on it, and add the 2-3 things the user will most likely need next '
              '(e.g. 有賣嗎 → 價格、怎麼買、庫存/替代管道). Checklists, each item only when the evidence has it: '
              '旅遊/活動/美食 → 時間地點、交通、費用、網友評價/注意事項; 學術/技術 → 原理、機制、步驟、優劣; '
              '3C/產品 → 規格對比、售價、評測優缺點、適合誰. Roughly 800-2500 Chinese characters.\n')
BREADTH_CLOSURE = ('5. Closure: do not search, crawl, browse or fetch images on your own. End with one short line that '
                   'offers depth: name 1-3 items above the user may want expanded (詳細內容、交通、票價、評價…) and ask '
                   'which one to go deeper on.\n')
DEPTH_CLOSURE = ('5. Closure: do not search, crawl, browse or fetch images on your own. End with one short line on what '
                 'the evidence does not cover that matters for this question, then ask whether the user wants a deep '
                 'crawl of specific sources above or another research round.\n')


BUDGET_TAIL = '\nEnd of evidence. Answer now from the passages and search results above, following the rules at the top.\n'
IMAGE_TAIL = ('This evidence includes downloaded images: if they show the thing asked about, place one or two '
              '[[image: …]] markers from the Images list, copied exactly, on their own lines next to the text they illustrate.\n')
BUDGET_TOP = 6  # a latency-budget job commits only the top 6 snippets at once: room is left for page passages


def leading_rules(question, answer):
    """A latency-budget job's answer rules, placed BEFORE its evidence and kept to the few the tool description does
    not already give (answer from the evidence, cite it, do not search again): they are known when the job starts,
    so they are prefilled while the searches run, and every token of them is read before the answer."""
    shape = ('Breadth first: every distinct on-topic item, one or two lines each (時間/地點/價格 or the key number), '
             'then name 1-3 items to expand and ask which one.' if answer == 'breadth' else
             'Depth: that item in full with every concrete fact, plus the 2-3 things the user needs next.')
    return (f"The user's question: «{question[-600:]}»\n"
            f"# How to answer: this evidence is enough; answer now and do not search, crawl or call any tool again. Open "
            f"with the direct answer, a [來源](URL) link after every key fact, each fact once. {shape}\n")


def closing(answer, images):
    """What follows the evidence. A list of unread sources, the stop reason or the artifact
    path read as "evidence incomplete" and sent agents off to crawl or fetch more; those stay
    in the tool details. The text closes the lookup instead."""
    breadth = answer == 'breadth'
    return ('\nEnd of evidence. This is enough to answer: answer now from the passages and search results above.\n'
            '1. Focus: answer the question the user actually asked, not the whole topic. Open with the direct answer in the '
            'first one or two sentences, then only the evidence that supports or qualifies it. Leave out passages '
            'that do not serve the question (other branches, opening hours, unrelated products) even if they are here. '
            'If the evidence is about a different product, company or place than the one asked (a similar name, an '
            'older model), say so plainly; never present it as the same thing.\n'
            + (BREADTH_RULE if breadth else DEPTH_RULE) +
            '3. No repetition: state each fact once. No closing 結論/總結 restating the body, no second table '
            'repeating the same items, never the same content as both a table and bullets. Use a table only to '
            'compare several items across several attributes.\n'
            '4. Sources & media: short is not sourceless. Put a markdown link ([來源](URL)) right after every key '
            'fact or list item, even in a two-line answer. If downloaded images are listed below, place at most two '
            '[[image: …]] markers on their own lines (never inside a table) next to the text they illustrate, only images that show the '
            'thing asked about; skip logos, thumbnails and site banners, and skip images for yes/no or one-fact answers.\n'
            + (BREADTH_CLOSURE if breadth else DEPTH_CLOSURE)
            + ('This evidence includes downloaded images: if they show the thing asked about, place '
               'one or two [[image: …]] markers from the Images list, copied exactly, on their own lines '
               'next to the text they illustrate.\n' if images else ''))


def local_date_line(clock):
    when = datetime.fromisoformat(clock['iso'].replace('Z', '+00:00'))
    try:
        when = when.astimezone(ZoneInfo(clock['timezone']))
    except Exception:
        pass  # an unknown zone keeps the clock's own offset
    return (f"Today: {when:%Y-%m-%d} (週{'一二三四五六日'[when.weekday()]}), timezone {clock['timezone']}. "
            'Resolve relative dates (今天/明天/這週…) against this.')


def url_key(url):
    """Dedup identity only: a source is crawled at the exact URL a search engine returned."""
    u = urlsplit(url)
    return u.scheme.lower(), u.netloc.lower(), u.path, u.query


def _check(ok, error='invalid_research_input'):
    if not ok:
        raise ValueError(error)


def _query(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 200


class ResearchConnection:
    """Research for one worker socket connection, one job at a time."""

    def __init__(self, service, send, artifact_root, *, admission=None):
        self.service, self.send, self.root = service, send, Path(artifact_root)
        self.admission = admission or asyncio.Semaphore(2)  # daemon-wide job slots
        self.jobs = {}     # request id -> task running the job
        self.streams = {}  # job id -> streaming job still taking queries
        self.closed = False

    def add_query(self, session_id, params):
        """Query `index` of the research call still being written, or done=True once it is complete."""
        job = self.streams.get(params.get('jobId')) if isinstance(params, dict) else None
        _check(job is not None and job.session == session_id, 'unknown_streaming_job')
        if params.get('done') is True and set(params) == {'jobId', 'done'}:
            job.close_stream()
            return
        _check(set(params) == {'jobId', 'query', 'index'} and _query(params['query'])
               and type(params['index']) is int and 0 <= params['index'] < 4, 'invalid_add_query')
        job.add_search(params['query'].strip(), ENGINES[params['index']])

    async def run(self, request_id, session_id, params):
        if self.closed or self.jobs:
            raise ValueError('research_connection_busy_or_closed')
        job = Job(self, request_id, session_id, params)
        self.jobs[request_id] = asyncio.current_task()
        if job.open:
            self.streams[job.id] = job
        try:
            async with self.admission:
                return await job.run()
        finally:
            self.jobs.pop(request_id, None)
            self.streams.pop(job.id, None)

    async def close(self):
        self.closed = True
        tasks = list(self.jobs.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class Job:
    """One research request, validated on creation (anything unexpected is invalid_research_input)."""

    def __init__(self, conn, request_id, session, p):
        _check(isinstance(p, dict) and {'jobId', 'question', 'queries', 'clock'} <= p.keys() <= PARAMS)
        _check(isinstance(p['jobId'], str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', p['jobId']))  # a dir name
        _check(isinstance(p['question'], str) and p['question'].strip() and len(p['question']) <= 4000)
        _check(isinstance(p['queries'], list) and 1 <= len(p['queries']) <= 4 and all(map(_query, p['queries'])))
        clock = p['clock']
        _check(isinstance(clock, dict) and clock.keys() == {'iso', 'timezone'} and isinstance(clock['iso'], str)
               and isinstance(clock['timezone'], str) and len(clock['timezone']) <= 100)
        try:
            aware = datetime.fromisoformat(clock['iso'].replace('Z', '+00:00')).tzinfo is not None
        except ValueError:
            aware = False
        self.open, self.answer = p.get('streaming', False), p.get('answer', 'breadth')
        self.concurrency, self.word_budget = p.get('crawlConcurrency', 32), p.get('crawlWordBudget', 20000)
        # A latency budget (research-first answers): deliver what is committed when the deadline passes,
        # with a smaller evidence budget and no wait for images still downloading.
        self.deadline = p.get('deadlineMs')
        self.evidence_budget = p.get('evidenceBudget', EVIDENCE_BUDGET)
        image_wait_ms = p.get('imageWaitMs', int(IMAGE_WAIT * 1000))
        _check(aware and type(self.open) is bool and self.answer in ('breadth', 'depth')
               and type(self.concurrency) is int and 1 <= self.concurrency <= 64
               and type(self.word_budget) is int and 1000 <= self.word_budget <= 100000
               and (self.deadline is None or (type(self.deadline) is int and 500 <= self.deadline <= 120000))
               and type(self.evidence_budget) is int and 500 <= self.evidence_budget <= 20000
               and type(image_wait_ms) is int and 0 <= image_wait_ms <= 10000)
        self.image_wait, self.expired = image_wait_ms / 1000, False
        self.conn, self.service, self.request_id, self.session = conn, conn.service, request_id, session
        self.id, self.date_line = p['jobId'], local_date_line(clock)
        self.owner = 'research-' + secrets.token_hex(16)  # its tabs never share interactive session state
        # Query i of the call goes to ENGINES[i]; while `open`, the agent is still writing
        # the call and later queries arrive through add_search() until close_stream().
        self.wanted = [(q, ENGINES[i]) for i, q in enumerate(dict.fromkeys(q.strip() for q in p['queries']))]
        self.searched = {}   # (engine, normalized query) -> attempt id
        self.authority = {}  # exact URL a search engine returned -> [{taskId, provider}]
        self.sources = {}    # url_key -> {'id', 'url', 'title'}
        self.queue, self.order = [], itertools.count()  # crawl queue: (position in its query, arrival, source)
        self.tasks, self.crawling, self.searching = set(), 0, 0
        self.words, self.reason, self.collected, self.done = 0, None, False, False
        self.errors = []
        self.wake, self.changed = asyncio.Event(), asyncio.Event()
        self.sha256 = hashlib.sha256()
        # a latency budget counts the model's tokens (the time to read them); the default keeps its rough count
        self.passages = (ProgressivePassages(p['question'], self.evidence_budget, BUDGET_TOP, model_tokens) if self.deadline
                         else ProgressivePassages(p['question'], self.evidence_budget))
        self.lead = leading_rules(p['question'], self.answer) if self.deadline else ''
        self.images = ResearchImages(p['question'], self._fetch_image, on_ready=self._image,
                                     is_delivered=self.passages.delivered.__contains__)

    def add_search(self, query, engine):
        if self.open:
            self.wanted.append((query, engine))
            self.wake.set()

    def close_stream(self):
        self.open = False
        self.wake.set()
        if self.searches_done():
            self.changed.set()

    async def run(self):
        directory = self.conn.root / self.id
        directory.mkdir(mode=0o700, parents=True)  # a reused job id fails instead of sharing a journal
        self.journal_path = directory / 'evidence.jsonl'
        # The journal keeps every snippet and full page: the artifact behind a packet that is too large.
        with self.journal_path.open('w', encoding='utf-8') as self.journal:
            probe = asyncio.create_task(self.service.ensure_browser())
            probe.add_done_callback(lambda task: task.cancelled() or task.exception())
            sender = asyncio.create_task(self._send_progress())
            if self.lead:
                self.changed.set()   # the leading rules go out (and get prefilled) before any search returns
            timer = asyncio.get_running_loop().call_later(self.deadline / 1000, self._expire) if self.deadline else None
            try:
                while True:
                    while self.wanted:
                        self._start_search(*self.wanted.pop(0))
                    while self.queue and self.crawling < self.concurrency and not self.reason:
                        self.crawling += 1
                        self._spawn(self._crawl(heapq.heappop(self.queue)[2]))
                    # Past the deadline the searches and pages still loading are dropped (_stop cancels them).
                    if self.expired or (not self.tasks and (self.reason or not self.open)):
                        break
                    await self.wake.wait()
                    self.wake.clear()
                return await self._deliver()
            finally:
                if timer:
                    timer.cancel()
                await self._stop(sender)

    def _expire(self, reason='deadline_reached'):
        self.expired = True
        self.reason = self.reason or reason
        self.wake.set()

    def _check_full(self):
        # A latency budget delivers once its evidence budget is (nearly) used: later pages could not commit anything,
        # so waiting for them until the deadline only delays the answer.
        if self.deadline and self.passages.used >= 0.9 * self.passages.budget:
            self._expire('evidence_budget_reached')

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task):
        self.tasks.discard(task)
        self.wake.set()
        if not task.cancelled() and task.exception() is not None:
            self.errors.append(f'internal:{type(task.exception()).__name__}')

    def _start_search(self, query, engine):
        key = engine, ' '.join(query.casefold().split())
        if key in self.searched or len(self.searched) >= MAX_SEARCHES or self.reason:
            return
        self.searched[key] = attempt = f'q{len(self.searched) + 1}'
        self.passages.queries.append(query)
        self.searching += 1
        self._spawn(self._search(attempt, engine, query))

    async def _results(self, engine, query):
        if engine == '4get':
            return await fourget(query)
        await self.service.ensure_browser(max_age=PROBE_MAX_AGE)
        return await google(self.service, self.owner, query)

    def searches_done(self):
        """Every query of the call has its results (or failed): the snippets are complete, pages may still load."""
        return not self.open and not self.wanted and self.searching == 0

    async def _search(self, attempt, engine, query):
        try:
            await self._search_one(attempt, engine, query)
        finally:
            self.searching -= 1
            if self.searches_done():
                self.changed.set()                       # a frame that says so, even with no new evidence

    async def _search_one(self, attempt, engine, query):
        try:
            results = await asyncio.wait_for(self._results(engine, query), TASK_TIMEOUT)
        except TimeoutError:
            self.errors.append('search:callback_timeout')
            return
        except Exception as error:  # the class name only: messages can carry transport details
            self.errors.append(f'search:{type(error).__name__}')
            return
        if self.done:
            return  # a late result never grants crawl authority
        new, rows_seen = [], []
        for title, url, description in results:
            self.authority.setdefault(url, []).append({'taskId': attempt, 'provider': engine})
            source = self.sources.get(url_key(url))
            if source is None:
                source = self.sources[url_key(url)] = {'id': f's{len(self.sources) + 1}', 'url': url, 'title': title}
                new.append(source)
            self._record(sourceId=source['id'], kind='search_snippet', url=url, title=title, text=description)
            rows_seen.append((source['id'], title, url, description))
        if self.passages.add_results(rows_seen):
            self.changed.set()
            self._check_full()
        # Crawl in search order: every query's 1st new result, then its 2nd, and so on.
        # This queue is the only way to a crawl, so a job opens nothing but search results.
        for position, source in enumerate(new[:TOP_PER_QUERY]):
            heapq.heappush(self.queue, (position, next(self.order), source))

    async def _crawl(self, source):
        try:
            try:
                result = await asyncio.wait_for(self._load(source['url']), TASK_TIMEOUT)
            except Exception:
                result = {}
            if not self.done:  # a late page is never recorded
                self._page(source, result)
        finally:
            self.crawling -= 1

    async def _load(self, url):
        async def crawl():
            await self.service.ensure_browser(max_age=PROBE_MAX_AGE)
            return await self.service.crawl_one(url, 0, self.owner, asyncio.Semaphore(1),
                                                max_text_units=1_000_000, fetch_timeout=FETCH_TIMEOUT)
        browser = self.service.browser
        result = await crawl()
        if result.get('browserSuspect'):
            # A hung Chrome fails every page at the fetch timeout while the job's own probe is
            # still deciding: the first research after hours idle got 0 of 15 pages twice
            # (2026-10-27/28). Wait for a full probe, which relaunches a dead browser, and retry
            # the page once on the new one.
            await self.service.ensure_browser()
            if self.service.browser is not browser:
                result = await crawl()
        return result

    def _page(self, source, result):
        text = result.get('text') or ''
        ok = bool(result.get('ok')) and not result.get('truncated') and bool(text.strip())
        if result.get('ok'):
            self.images.offer(source['id'], source['title'], result.get('imageCandidates') or [])
        if text:
            self.words += rough_words(text)
            if self.words >= self.word_budget:
                self.reason = 'crawl_word_budget_reached'  # no new crawls; pages in flight still count
            self._record(sourceId=source['id'], kind='page_extract', url=source['url'], title=source['title'],
                         text=text, status='completed' if ok else 'failed', truncated=bool(result.get('truncated')))
        if ok:
            self.collected = True
        else:
            self._record(sourceId=source['id'], kind='crawl_failure', url=source['url'], title=source['title'],
                         text='', status='failed')
            self.errors.append('crawl_empty_or_failed')
        # A failed or partial page commits nothing and frees the budget reserved for it.
        if self.passages.add(source['id'], source['title'], source['url'], text if ok else ''):
            self.changed.set()
            self._check_full()

    def _record(self, **record):
        line = json.dumps(record, ensure_ascii=False) + '\n'
        self.journal.write(line)
        self.sha256.update(line.encode())

    async def _fetch_image(self, url):
        async with self.service.image_fetch_semaphore:
            path, _mime, width, height, _url = await self.service.run_fetch_image(url, self.session)
        return {'path': str(path), 'width': width, 'height': height}

    def _image(self, line):
        if not self.done:
            self.passages.text += line  # outside the passage budget, append-only like the passages
            self.changed.set()

    async def _send_progress(self):
        while True:
            await self.changed.wait()
            self.changed.clear()
            try:
                await self.conn.send(dict(type='progress', id=self.request_id, jobId=self.id, phase='evidence',
                                          prefix=self.date_line + '\n' + self.lead + self.passages.text,
                                          searched=self.searches_done()))
            except Exception:
                pass  # best effort: the final packet carries everything

    async def _deliver(self):
        # Everything was committed (and prefilled) while crawling: from here on only the
        # images still downloading and the closing rules follow the last prefix.
        self.done = True
        packet = self.date_line + '\n' + self.lead + self.passages.text
        await self.images.settle(self.image_wait)
        packet += self.images.section(self.passages.delivered)
        packet += (BUDGET_TAIL + (IMAGE_TAIL if self.images.delivered else '')) if self.lead \
            else closing(self.answer, self.images.delivered)
        result = dict(action='research', jobId=self.id, status='collected' if self.collected else 'incomplete',
                      reason=self.reason or 'searches_done', text=packet, fullEvidenceDelivered=True,
                      artifactPath=str(self.journal_path), sha256=self.sha256.hexdigest(),
                      budgetUsed=len(self.searched), attempts=list(self.searched.values()),
                      sources=[dict(sourceId=s['id'], url=s['url'], discoveries=self.authority[s['url']])
                               for s in self.sources.values()],
                      authorizations=[dict(url=url, discoveries=d) for url, d in self.authority.items()],
                      errors=self.errors, crawlWords=self.words, crawlWordBudget=self.word_budget,
                      crawlConcurrency=self.concurrency, rulesFirst=bool(self.lead))
        if len(packet.encode()) > MAX_PACKET_BYTES or packet.count('\n') >= MAX_PACKET_LINES:
            result.update(status='incomplete', reason='packet_too_large', fullEvidenceDelivered=False,
                          text='Research incomplete: packet_too_large. Full evidence was NOT delivered. '
                               'The immutable artifact is preserved for recovery, not a successful one-turn handoff.')
        return result

    async def _stop(self, sender):
        """Stop what still runs and close the job's tabs; another cancel cannot interrupt this."""
        self.done = True
        self.images.cancel()
        work = [*self.tasks, sender]
        for task in work:
            task.cancel()

        async def close():
            await asyncio.gather(*work, return_exceptions=True)  # searches and crawls close their own tabs
            await self.service.cleanup_research_owner(self.owner)  # and whatever a failed close left open
        cleanup = asyncio.ensure_future(close())
        loop = asyncio.get_running_loop()
        deadline, cancelled = loop.time() + CLEANUP_TIMEOUT, False
        while not cleanup.done() and loop.time() < deadline:
            try:
                await asyncio.wait({cleanup}, timeout=deadline - loop.time())
            except asyncio.CancelledError:
                cancelled = True
        if not cleanup.done():
            # Keep it referenced: it still closes the tabs once a stuck crawl lets go.
            self.service.research_cleanup_tasks.add(cleanup)
            cleanup.add_done_callback(lambda t: (self.service.research_cleanup_tasks.discard(t),
                                                 t.cancelled() or t.exception()))
            raise ValueError('research_cleanup_incomplete')
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError
