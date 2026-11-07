"""Research jobs end to end with a fake browser service and fake 4get; no Chrome, network or model."""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from research import jobs
from research.jobs import ResearchConnection
from research.passages import rough_words

CLOCK = {'iso': '2026-01-01T00:00:00Z', 'timezone': 'UTC'}


def service(**members):
    """A browser service fake with every member research uses."""
    async def crawl_one(url, *args, **kwargs):
        return dict(ok=True, text=f'台積電 收盤價 1200 元 {url}')

    async def search_one(search, *args, **kwargs):
        return dict(ok=True, results=[])

    async def run_fetch_image(url, session):
        raise ValueError('not an image')
    return SimpleNamespace(**dict(browser=object(), ensure_browser=AsyncMock(), crawl_one=crawl_one,
                                  search_one=search_one, cleanup_research_owner=AsyncMock(),
                                  research_cleanup_tasks=set(), image_fetch_semaphore=asyncio.Semaphore(4),
                                  run_fetch_image=run_fetch_image) | members)


def rows(*urls, snippet='台積電 收盤價'):
    return [(f'title {url}', url, snippet) for url in urls]


async def one_result(query):
    return rows(f'https://example.com/{len(query)}')


class JobTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.frames = []

    async def send(self, frame):
        self.frames.append(frame)

    def start(self, svc, fourget=one_result, session='owner', **params):
        """(connection, running job task); params default to one query."""
        params = dict(jobId='job', question='台積電 收盤價', queries=['台積電 收盤價'], clock=CLOCK) | params
        conn = ResearchConnection(svc, self.send, self.root)
        patcher = patch.object(jobs, 'fourget', fourget)
        patcher.start()
        self.addCleanup(patcher.stop)
        return conn, asyncio.create_task(conn.run(1, session, params))

    async def research(self, svc, fourget=one_result, **params):
        _, job = self.start(svc, fourget, **params)
        return await asyncio.wait_for(job, 5)

    def journal(self, job_id='job'):
        path = self.root / job_id / 'evidence.jsonl'
        return [json.loads(line) for line in path.read_bytes().split(b'\n') if line]

    async def test_input_must_be_exactly_the_documented_parameters(self):
        good = dict(jobId='job', question='q', queries=['q'], clock=CLOCK)
        bad = [dict(good, provider='auto'), dict(good, searchBudget=8), {k: v for k, v in good.items() if k != 'queries'},
               dict(good, jobId='../x'), dict(good, jobId='a/b'), dict(good, jobId=''), dict(good, question=' '),
               dict(good, question='x' * 4001), dict(good, queries=[]), dict(good, queries=['x'] * 5),
               dict(good, queries=['']), dict(good, queries=['x' * 201]), dict(good, queries='q'),
               dict(good, clock={'iso': '2026-01-01T00:00:00'}), dict(good, clock={'iso': '2026-01-01T00:00:00', 'timezone': 'UTC'}),
               dict(good, clock={'iso': 'fixture', 'timezone': 'UTC'}), dict(good, streaming='yes'),
               dict(good, answer='deep'), dict(good, crawlConcurrency=0), dict(good, crawlConcurrency=65),
               dict(good, crawlConcurrency=True), dict(good, crawlWordBudget=999), dict(good, crawlWordBudget=100001)]
        conn = ResearchConnection(service(), self.send, self.root)
        for params in bad:
            with self.subTest(params=params), self.assertRaisesRegex(ValueError, 'invalid_research_input'):
                await conn.run(1, 'owner', params)
        self.assertEqual(list(self.root.iterdir()), [], 'nothing starts for invalid input')
        self.assertEqual(conn.jobs, {})

    async def test_queries_go_to_three_4get_searches_and_one_google_search(self):
        searched = []

        async def fourget(query):
            searched.append(('4get', query))
            return rows(f'https://example.com/{len(searched)}')

        async def search_one(search, index, owner, slots, **kwargs):
            searched.append(('google', search['query']))
            self.assertTrue(owner.startswith('research-'))
            return dict(ok=True, results=[dict(title='g', url='https://example.com/google', snippet='台積電')])
        result = await self.research(service(search_one=search_one), fourget,
                                     queries=['TSMC close', '台積電 收盤', 'tsmc  CLOSE', '2330 price'])
        self.assertEqual(sorted(searched), [('4get', 'TSMC close'), ('4get', '台積電 收盤'), ('google', '2330 price')],
                         'q4 is the Google query; the same query on the same engine is searched once')
        self.assertEqual(result['budgetUsed'], 3)
        self.assertEqual(result['attempts'], ['q1', 'q2', 'q3'])
        self.assertIn('https://example.com/google', [s['url'] for s in result['sources']])

    async def test_results_are_crawled_in_search_order_top_five_per_query(self):
        first_crawl, crawled, options = asyncio.Event(), [], []

        async def fourget(query):
            if query == 'beta':
                await first_crawl.wait()
            return rows(*(f'https://example.com/{query[0]}/{i}' for i in range(7)))

        async def crawl_one(url, index, owner, slots, **kwargs):
            crawled.append(url)
            options.append((owner, kwargs))
            first_crawl.set()
            await asyncio.sleep(0.01)
            return dict(ok=True, text=f'page {url} alpha beta')
        result = await self.research(service(crawl_one=crawl_one), fourget, question='alpha beta',
                                     queries=['alpha', 'beta'], crawlConcurrency=1)
        self.assertEqual([u.split('/', 3)[3] for u in crawled],
                         ['a/0', 'b/0', 'a/1', 'b/1', 'a/2', 'b/2', 'a/3', 'b/3', 'a/4', 'b/4'],
                         "every query's 1st result, then its 2nd ...; results 6-7 are not crawled")
        owner, kwargs = options[0]
        self.assertNotEqual(owner, 'owner', 'crawl tabs never belong to the Pi session')
        self.assertEqual(kwargs, dict(max_text_units=1_000_000, fetch_timeout=3.0))
        self.assertTrue(result['fullEvidenceDelivered'])

    async def test_a_finished_search_is_crawled_while_another_search_runs(self):
        slow, fast_crawled = asyncio.Event(), asyncio.Event()

        async def fourget(query):
            if query == 'slow':
                await slow.wait()
            return rows(f'https://example.com/{query}')

        async def crawl_one(url, *args, **kwargs):
            if url.endswith('/fast'):
                fast_crawled.set()
            return dict(ok=True, text='台積電 收盤價 1200 元')
        conn, job = self.start(service(crawl_one=crawl_one), fourget, queries=['fast', 'slow'])
        try:
            await asyncio.wait_for(fast_crawled.wait(), 3)
        finally:
            slow.set()
        self.assertTrue((await asyncio.wait_for(job, 5))['fullEvidenceDelivered'])

    async def test_duplicate_urls_are_crawled_once_at_the_first_exact_url(self):
        crawled = []

        async def fourget(query):
            if query == 'third':
                return []
            if query == 'second':
                await asyncio.sleep(0.05)
                return rows('https://WWW.EXAMPLE.COM/path/?x=%2f#second', 'https://example.com/a/')
            return rows('https://www.example.com/path/?x=%2f#first', 'https://example.com/a', 'https://example.com/a?q=1')

        async def search_one(search, *args, **kwargs):
            return dict(ok=True, results=[dict(title='g', url='https://www.google.com/goto?url=CAES1', snippet='s')])

        async def crawl_one(url, *args, **kwargs):
            crawled.append(url)
            return dict(ok=True, text='台積電 收盤價 1200 元')
        with patch('research.providers.resolve_google_redirect', lambda url: 'https://example.com/a'):
            result = await self.research(service(crawl_one=crawl_one, search_one=search_one), fourget,
                                         queries=['first', 'second', 'third', 'google'])
        self.assertEqual(sorted(crawled), ['https://example.com/a', 'https://example.com/a/', 'https://example.com/a?q=1',
                                           'https://www.example.com/path/?x=%2f#first'],
                         'host case and fragment do not make a new page; a trailing slash or query does')
        authorizations = {a['url']: [d['taskId'] for d in a['discoveries']] for a in result['authorizations']}
        self.assertEqual(authorizations['https://www.example.com/path/?x=%2f#first'], ['q1'])
        self.assertEqual(authorizations['https://WWW.EXAMPLE.COM/path/?x=%2f#second'], ['q2'])
        self.assertEqual(authorizations['https://example.com/a'], ['q1', 'q4'], 'the resolved Google URL is the same page')
        self.assertEqual(len(result['sources']), 4)

    async def test_progressive_prefix_arrives_while_another_page_crawls(self):
        first_progress, second = asyncio.Event(), asyncio.Event()

        async def fourget(query):
            return rows('https://example.com/1', 'https://example.com/2')

        async def crawl_one(url, *args, **kwargs):
            if url.endswith('/2'):
                await second.wait()
                return dict(ok=True, text='台積電 收盤價 1190 元')
            return dict(ok=True, text='台積電 收盤價 1200 元')

        async def send(frame):
            self.frames.append(frame)
            if frame['type'] == 'progress' and '1200' in frame['prefix']:
                first_progress.set()
        self.send = send
        conn, job = self.start(service(crawl_one=crawl_one), fourget, clock={'iso': '2026-09-28T23:30:00Z', 'timezone': 'Asia/Taipei'})
        try:
            await asyncio.wait_for(first_progress.wait(), 3)
            self.assertFalse(job.done(), 'progress overlaps a page still in flight')
            prefixes = [f['prefix'] for f in self.frames if f['type'] == 'progress']
            self.assertTrue(all(f['jobId'] == 'job' and f['id'] == 1 and f['phase'] == 'evidence' for f in self.frames))
            self.assertTrue(prefixes[0].startswith('Today: 2026-09-29 (週二)'), 'every prefix starts with the local date line')
            self.assertIn('Top search results', prefixes[0], 'top snippets are committed at search time')
            self.assertTrue(all(b.startswith(a) for a, b in zip(prefixes, prefixes[1:])), 'later prefixes only append')
        finally:
            second.set()
        result = await asyncio.wait_for(job, 5)
        self.assertTrue(result['text'].startswith(prefixes[-1]))
        self.assertIn('1190', result['text'])
        self.assertEqual(result['status'], 'collected')
        self.assertEqual(result['reason'], 'searches_done')

    async def test_unicode_line_separators_in_page_text_survive(self):
        async def crawl_one(url, *args, **kwargs):
            return dict(ok=True, text='Mac mini 售價 NT$19,900 起\x85教育價\x1c另計')
        result = await self.research(service(crawl_one=crawl_one), question='Mac mini 售價', queries=['Mac mini 售價'])
        self.assertTrue(result['fullEvidenceDelivered'])
        self.assertIn('NT$19,900', result['text'])

    async def test_answer_mode_picks_breadth_or_depth_closing_rules(self):
        texts = {}
        for mode in (None, 'breadth', 'depth'):
            params = dict(jobId=f'job-{mode}')
            if mode:
                params['answer'] = mode
            texts[mode] = (await self.research(service(), **params))['text']
        for mode in (None, 'breadth'):  # breadth is the default for a new question
            self.assertIn(jobs.BREADTH_RULE, texts[mode])
            self.assertIn(jobs.BREADTH_CLOSURE, texts[mode])
            self.assertNotIn(jobs.DEPTH_RULE, texts[mode])
        self.assertIn(jobs.DEPTH_RULE, texts['depth'])
        self.assertIn(jobs.DEPTH_CLOSURE, texts['depth'])
        self.assertNotIn(jobs.BREADTH_RULE, texts['depth'])
        self.assertTrue(texts['depth'].endswith(jobs.DEPTH_CLOSURE), 'the packet ends with the closing rule')

    async def test_a_hung_browser_is_relaunched_and_the_page_retried_once(self):
        hung, fresh, calls = object(), object(), []
        svc = service(browser=hung)

        async def ensure_browser(max_age=None):
            if max_age is None:  # the job's full probe gives up on the hung Chrome and relaunches it
                await asyncio.sleep(0.3)
                svc.browser = fresh

        async def crawl_one(url, *args, **kwargs):
            calls.append((url, svc.browser is fresh))
            if svc.browser is hung:
                return dict(ok=False, text='', title='Timeout', browserSuspect=True)
            return dict(ok=True, text=f'台股 T+2 交割 {url} 成交後第二個營業日')

        async def fourget(query):
            return rows(*(f'https://example.com/{i}' for i in range(3)))
        svc.ensure_browser, svc.crawl_one = ensure_browser, crawl_one
        result = await self.research(svc, fourget, question='台股交割', queries=['台股 交割'])
        self.assertGreater(result['crawlWords'], 0)
        self.assertNotIn('crawl_empty_or_failed', result['errors'])
        for i in range(3):  # each page: at most one attempt on the hung browser, then one on the new one
            url = f'https://example.com/{i}'
            attempts = [c for c in calls if c[0] == url]
            self.assertLessEqual(attempts.count((url, False)), 1)
            self.assertEqual(attempts[-1], (url, True))

    async def test_a_slow_page_on_a_healthy_browser_is_not_retried(self):
        calls = []

        async def crawl_one(url, *args, **kwargs):
            calls.append(url)
            return dict(ok=False, text='', title='Timeout', browserSuspect=True)
        result = await self.research(service(crawl_one=crawl_one))
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['status'], 'incomplete')

    async def test_images_fetched_while_crawling_are_listed_before_the_rules(self):
        poster = self.root / 'poster.jpg'
        poster.write_bytes(b'jpeg')
        fetched = []

        async def crawl_one(url, *args, **kwargs):
            return dict(ok=True, text='台南 黑琵季 10/3 起在新光三越舉行', imageCandidates=[
                dict(url='https://cdn.example/poster.jpg', role='representative', width=900, height=1200, alt='黑琵季海報', caption='', score=90),
                dict(url='https://cdn.example/logo.png', role='content', width=900, height=900, alt='', caption='', score=10)])

        async def run_fetch_image(url, session):
            fetched.append((url, session))
            return poster, 'image/jpeg', 900, 1200, url
        result = await self.research(service(crawl_one=crawl_one, run_fetch_image=run_fetch_image),
                                     question='台南 黑琵季', queries=['黑琵季 台南'])
        self.assertEqual(fetched, [('https://cdn.example/poster.jpg', 'owner')], 'stored under the Pi session')
        text = result['text']
        self.assertIn('## Images (already downloaded; do not fetch)', text)
        self.assertIn(f'[[image: {poster}]]', text)
        self.assertIn('This evidence includes downloaded images', text)
        self.assertLess(text.index('## Images'), text.index('End of evidence'))

    async def test_streaming_job_takes_queries_while_running_and_waits_for_done(self):
        searched = []

        async def fourget(query):
            searched.append(('4get', query))
            return rows(f'https://example.com/{len(searched)}')

        async def search_one(search, *args, **kwargs):
            searched.append(('google', search['query']))
            return dict(ok=True, results=[])
        conn, job = self.start(service(search_one=search_one), fourget, queries=['台積電 收盤價 A'], streaming=True)
        await asyncio.sleep(0.2)
        self.assertFalse(job.done(), 'a streaming job waits for the rest of the call')
        conn.add_query('owner', dict(jobId='job', query='台積電 收盤價 B', index=1))
        conn.add_query('owner', dict(jobId='job', query='台積電 收盤價 A', index=3))  # q4: Google, a different search
        with self.assertRaisesRegex(ValueError, 'unknown_streaming_job'):
            conn.add_query('intruder', dict(jobId='job', query='x', index=2))
        with self.assertRaisesRegex(ValueError, 'unknown_streaming_job'):
            conn.add_query('owner', dict(jobId='other', query='x', index=2))
        for bad in (dict(jobId='job', query='', index=2), dict(jobId='job', query='x', index=4),
                    dict(jobId='job', query='x'), dict(jobId='job', done=1)):
            with self.assertRaisesRegex(ValueError, 'invalid_add_query'):
                conn.add_query('owner', bad)
        await asyncio.sleep(0.2)
        self.assertFalse(job.done())
        conn.add_query('owner', dict(jobId='job', done=True))
        result = await asyncio.wait_for(job, 5)
        self.assertEqual(searched, [('4get', '台積電 收盤價 A'), ('4get', '台積電 收盤價 B'), ('google', '台積電 收盤價 A')])
        self.assertTrue(result['fullEvidenceDelivered'])
        self.assertEqual(conn.streams, {})

    async def test_a_job_searches_at_most_eight_queries(self):
        conn, job = self.start(service(), queries=['q0'], streaming=True)
        await asyncio.sleep(0.05)
        for i in range(1, 12):
            conn.add_query('owner', dict(jobId='job', query=f'query {i}', index=i % 3))
        conn.add_query('owner', dict(jobId='job', done=True))
        self.assertEqual((await asyncio.wait_for(job, 5))['budgetUsed'], 8)

    async def test_word_budget_stops_new_crawls_but_keeps_pages_in_flight(self):
        crawled, page = [], ' '.join(['word'] * 1100)

        async def fourget(query):
            return rows(*(f'https://example.com/{i}' for i in range(5)))

        async def crawl_one(url, *args, **kwargs):
            crawled.append(url)
            await asyncio.sleep(0.01 * len(crawled))
            return dict(ok=True, text=f'{page} {url}')
        result = await self.research(service(crawl_one=crawl_one), fourget, crawlConcurrency=2, crawlWordBudget=1000)
        self.assertEqual(len(crawled), 2)
        self.assertEqual(result['reason'], 'crawl_word_budget_reached')
        pages = [r['text'] for r in self.journal() if r['kind'] == 'page_extract']
        self.assertEqual(sorted(pages), sorted(f'{page} {url}' for url in crawled), 'full page text is kept')
        self.assertEqual(result['crawlWords'], sum(map(rough_words, pages)))

    async def test_failed_and_truncated_pages_commit_no_passage(self):
        async def fourget(query):
            return rows(*(f'https://example.com/{i}' for i in range(3)), snippet='')

        async def crawl_one(url, *args, **kwargs):
            if url.endswith('/1'):
                return dict(ok=True, text='台積電 收盤價 1190 partial', truncated=True)
            if url.endswith('/2'):
                return dict(ok=False, text='', error='HTTP 500')
            return dict(ok=True, text='台積電 收盤價 1200 元')
        result = await self.research(service(crawl_one=crawl_one), fourget)
        self.assertIn('[s1]', result['text'])
        self.assertNotIn('1190', result['text'])
        self.assertNotIn('[s3]', result['text'])
        self.assertEqual(result['errors'].count('crawl_empty_or_failed'), 2)
        kinds = sorted((r['sourceId'], r['kind'], r.get('status')) for r in self.journal() if r['kind'] != 'search_snippet')
        self.assertEqual(kinds, [('s1', 'page_extract', 'completed'), ('s2', 'crawl_failure', 'failed'),
                                 ('s2', 'page_extract', 'failed'), ('s3', 'crawl_failure', 'failed')])

    async def test_search_failures_are_recorded_by_class_name_only(self):
        async def fourget(query):
            if query == 'error':
                raise OSError('secret-token-MUST-NOT-APPEAR')
            await asyncio.Event().wait()
        with patch.object(jobs, 'TASK_TIMEOUT', 0.05):
            result = await self.research(service(), fourget, queries=['error', 'timeout'])
        self.assertEqual(sorted(result['errors']), ['search:OSError', 'search:callback_timeout'])
        self.assertNotIn('secret-token', repr(result))
        self.assertEqual((result['status'], result['budgetUsed']), ('incomplete', 2))

    async def test_a_crawl_that_never_returns_times_out_as_a_failed_page(self):
        async def crawl_one(url, *args, **kwargs):
            await asyncio.Event().wait()
        with patch.object(jobs, 'TASK_TIMEOUT', 0.05):
            result = await self.research(service(crawl_one=crawl_one))
        self.assertEqual(result['errors'], ['crawl_empty_or_failed'])
        self.assertEqual(result['status'], 'incomplete')

    async def test_oversized_packet_is_not_delivered_and_the_artifact_is_kept(self):
        with patch.object(jobs, 'MAX_PACKET_BYTES', 100):
            result = await self.research(service())
        self.assertEqual((result['status'], result['reason']), ('incomplete', 'packet_too_large'))
        self.assertFalse(result['fullEvidenceDelivered'])
        self.assertIn('artifact is preserved', result['text'])
        raw = Path(result['artifactPath']).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), result['sha256'])
        self.assertIn('台積電 收盤價 1200 元 https://example.com/', raw.decode())
        self.assertEqual(Path(result['artifactPath']).parent.stat().st_mode & 0o777, 0o700)

    async def test_a_reused_job_id_fails_instead_of_sharing_a_journal(self):
        await self.research(service())
        with self.assertRaises(FileExistsError):
            await self.research(service())

    async def test_cancel_stops_crawls_and_closes_the_jobs_tabs(self):
        crawling, crawl_closed = asyncio.Event(), asyncio.Event()
        svc = service()

        async def crawl_one(url, index, owner, *args, **kwargs):
            crawling.set()
            try:
                await asyncio.Event().wait()
            finally:
                crawl_closed.set()
        svc.crawl_one = crawl_one
        conn, job = self.start(svc)
        await asyncio.wait_for(crawling.wait(), 3)
        job.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(job, 3)
        self.assertTrue(crawl_closed.is_set())
        owner = svc.cleanup_research_owner.await_args.args[0]
        self.assertTrue(owner.startswith('research-'))
        self.assertEqual(conn.jobs, {})

    async def test_repeated_cancellation_cannot_interrupt_owned_cleanup(self):
        crawling, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def crawl_one(url, *args, **kwargs):
            crawling.set()
            await asyncio.Event().wait()

        async def cleanup(owner):
            entered.set()
            await release.wait()
        conn, job = self.start(service(crawl_one=crawl_one, cleanup_research_owner=cleanup))
        await asyncio.wait_for(crawling.wait(), 3)
        job.cancel()
        await asyncio.wait_for(entered.wait(), 2)
        job.cancel()
        await asyncio.sleep(0.05)
        try:
            self.assertFalse(job.done(), 'repeated cancellation interrupted cleanup')
        finally:
            release.set()
            await asyncio.gather(job, return_exceptions=True)
        self.assertEqual(conn.jobs, {})

    async def test_cleanup_that_cannot_finish_is_reported_and_kept_until_it_does(self):
        release = asyncio.Event()

        async def cleanup(owner):
            await release.wait()
        svc = service(cleanup_research_owner=cleanup)
        with patch.object(jobs, 'CLEANUP_TIMEOUT', 0.1):
            with self.assertRaisesRegex(ValueError, 'research_cleanup_incomplete'):
                await self.research(svc)
        self.assertEqual(len(svc.research_cleanup_tasks), 1, 'the unfinished cleanup stays referenced')
        release.set()
        await asyncio.gather(*svc.research_cleanup_tasks)
        await asyncio.sleep(0)
        self.assertEqual(svc.research_cleanup_tasks, set())

    async def test_late_results_after_cancel_are_never_recorded(self):
        searching, crawling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def stubborn(event, value):  # ignores cancellation, like a transport that cannot be interrupted
            event.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
            return value

        async def fourget(query):
            if query == 'late':
                return await stubborn(searching, rows('https://example.com/late'))
            return rows('https://example.com/page')

        async def crawl_one(url, *args, **kwargs):
            return await stubborn(crawling, dict(ok=True, text='台積電 收盤價 late page'))
        created = []

        class Spy(jobs.Job):
            def __init__(self, *args):
                super().__init__(*args)
                created.append(self)
        with patch.object(jobs, 'Job', Spy):
            conn, job = self.start(service(crawl_one=crawl_one), fourget, queries=['page', 'late'])
            await asyncio.wait_for(asyncio.gather(searching.wait(), crawling.wait()), 3)
        job.cancel()
        with patch.object(jobs, 'CLEANUP_TIMEOUT', 0.1):
            with self.assertRaisesRegex(ValueError, 'research_cleanup_incomplete'):
                await asyncio.wait_for(job, 3)
        journal = (self.root / 'job' / 'evidence.jsonl').read_bytes()
        release.set()
        await asyncio.sleep(0.05)
        late = created[0]
        self.assertNotIn('https://example.com/late', late.authority, 'a late search result grants no crawl authority')
        self.assertNotIn('late page', late.passages.text)
        self.assertEqual(late.errors, [], 'nothing tried to write after the job ended')
        self.assertEqual((self.root / 'job' / 'evidence.jsonl').read_bytes(), journal)

    async def test_close_cancels_the_running_job_and_refuses_new_ones(self):
        crawling = asyncio.Event()

        async def crawl_one(url, *args, **kwargs):
            crawling.set()
            await asyncio.Event().wait()
        conn, job = self.start(service(crawl_one=crawl_one))
        await asyncio.wait_for(crawling.wait(), 3)
        with self.assertRaisesRegex(ValueError, 'research_connection_busy_or_closed'):
            await conn.run(2, 'owner', dict(jobId='other', question='q', queries=['q'], clock=CLOCK))
        await asyncio.wait_for(conn.close(), 3)
        self.assertTrue(job.cancelled())
        with self.assertRaisesRegex(ValueError, 'research_connection_busy_or_closed'):
            await conn.run(3, 'owner', dict(jobId='other', question='q', queries=['q'], clock=CLOCK))


class DateLineTests(unittest.TestCase):
    def test_local_date_line_uses_the_users_timezone(self):
        self.assertTrue(jobs.local_date_line({'iso': '2026-09-28T23:30:00Z', 'timezone': 'Asia/Taipei'})
                        .startswith('Today: 2026-09-29 (週二), timezone Asia/Taipei.'))
        self.assertTrue(jobs.local_date_line({'iso': '2026-09-28T23:30:00+08:00', 'timezone': 'Nowhere/Unknown'})
                        .startswith('Today: 2026-09-28 (週一)'), 'an unknown zone keeps the clock offset')


if __name__ == '__main__':
    unittest.main()
