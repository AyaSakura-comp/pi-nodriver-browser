import asyncio
import io
import json
import unittest
from unittest import mock
import test_research_controller as fixtures
from test_research_controller import intent
from research import focus
from research.contracts import SearchResult, Limits
from research.controller_contracts import Plan, CrawlResult, RankedSources

PAGE = '\n'.join(['Home', 'Menu'] + [f'Sentence {i} about something.' for i in range(10)]
                 + ['Perihelion occurs on or about January 3.', 'Aphelion is about July 4.', 'Footer'])


class FocusTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_without_endpoint(self):
        with mock.patch.dict('os.environ', {}, clear=True):
            self.assertIsNone(await focus.focus_page('q', 't', PAGE))

    async def test_only_verbatim_lines_survive(self):
        reply = 'Perihelion occurs on or about January 3.\n- Aphelion is about July 4.\nEarth is closest in July.\nNONE'
        class R:
            def __init__(self): self.body = json.dumps({'choices': [{'message': {'content': reply}}]}).encode()
            def __enter__(self): return io.BytesIO(self.body)
            def __exit__(self, *a): return False
        with mock.patch.dict('os.environ', {'RESEARCH_FOCUS_URL': 'http://x'}), \
             mock.patch('urllib.request.urlopen', return_value=R()):
            text, stats = await focus.focus_page('when is perihelion', 't', PAGE)
        self.assertEqual(text.split('\n'), ['Perihelion occurs on or about January 3.', 'Aphelion is about July 4.'])
        self.assertNotIn('closest in July', text)  # invented line dropped

    async def test_all_none_means_irrelevant_not_full_page(self):
        with mock.patch.dict('os.environ', {'RESEARCH_FOCUS_URL': 'http://x'}), \
             mock.patch.object(focus, '_extract', return_value=[]):
            text, stats = await focus.focus_page('q', 't', PAGE)
        self.assertEqual(text, '')

    async def test_filter_failure_keeps_full_page(self):
        with mock.patch.dict('os.environ', {'RESEARCH_FOCUS_URL': 'http://x'}), \
             mock.patch.object(focus, '_extract', side_effect=OSError('down')):
            self.assertIsNone(await focus.focus_page('q', 't', PAGE))


class FocusControllerTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ControllerTests.asyncSetUp
    controller = fixtures.ControllerTests.controller

    async def test_focus_text_is_delivered_and_drives_word_budget(self):
        async def search(t): return [SearchResult('0', 'https://example.com/0', 'desc')]
        async def rank(req): return RankedSources(req.view.revision, tuple(s.source_id for s in req.sources)[:req.limit])
        async def crawl(req): return CrawlResult('one two three four five six', focus_text='[Focused] three')
        async def planner(v): return Plan(v.revision, (intent('query'),))
        c = self.controller(search, planner, None, crawl, rank=rank, limits=Limits(search_budget=1, crawl_word_budget=20000))
        r = await c.run()
        pages = [e for e in r.evidence if e.kind == 'page_extract']
        self.assertEqual([e.text for e in pages], ['[Focused] three'])
        self.assertEqual(r.crawl_words, 2)


class FocusStageTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ControllerTests.asyncSetUp
    controller = fixtures.ControllerTests.controller

    async def test_crawl_slot_is_released_before_the_serial_focus_stage(self):
        log = []; active = 0; peak = 0
        async def search(t): return [SearchResult(str(i), f'https://example.com/{i}', 'desc') for i in range(3)]
        async def rank(req): return RankedSources(req.view.revision, tuple(s.source_id for s in req.sources)[:req.limit])
        async def crawl(req):
            log.append(('crawl', req.source_id)); await asyncio.sleep(.01)
            return CrawlResult(f'full page {req.source_id} with plenty of words here')
        async def focus_stage(req):
            nonlocal active, peak
            active += 1; peak = max(peak, active); log.append(('focus', req.source_id))
            await asyncio.sleep(.05); active -= 1
            return f'[Focused] {req.source_id}'
        async def planner(v): return Plan(v.revision, (intent('query'),))
        c = self.controller(search, planner, None, crawl, rank=rank,
                            limits=Limits(search_budget=1, crawl_concurrency=1, crawl_word_budget=20000))
        c.focus = focus_stage
        r = await c.run()
        crawls = [i for i, (k, _) in enumerate(log) if k == 'crawl']
        focuses = [i for i, (k, _) in enumerate(log) if k == 'focus']
        self.assertLess(crawls[1], focuses[-1], 'next crawl must not wait for the previous page\'s filter')
        self.assertEqual(peak, 1, 'one page in the focus stage at a time per job')
        pages = sorted(e.text for e in r.evidence if e.kind == 'page_extract')
        self.assertEqual(pages, ['[Focused] s1', '[Focused] s2', '[Focused] s3'])
        self.assertEqual(r.crawl_words, 6)

    async def test_focus_failure_delivers_the_full_page(self):
        async def search(t): return [SearchResult('0', 'https://example.com/0', 'desc')]
        async def rank(req): return RankedSources(req.view.revision, tuple(s.source_id for s in req.sources)[:req.limit])
        async def crawl(req): return CrawlResult('the full page text')
        async def focus_stage(req): raise OSError('filter down')
        async def planner(v): return Plan(v.revision, (intent('query'),))
        c = self.controller(search, planner, None, crawl, rank=rank, limits=Limits(search_budget=1))
        c.focus = focus_stage
        r = await c.run()
        self.assertEqual([e.text for e in r.evidence if e.kind == 'page_extract'], ['the full page text'])


class FocusGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_sessions_share_one_fifo_gate_without_mixing_pages(self):
        order = []; inside = 0; peak = 0
        def fake_extract(url, question, title, window, timeout):
            nonlocal inside, peak
            inside += 1; peak = max(peak, inside)
            import time; time.sleep(.02)
            order.append(question); inside -= 1
            return [line for line in window.split('\n') if question in line]
        page_a = '\n'.join(['alpha fact one is here.'] + [f'filler line {i} of page A.' for i in range(10)])
        page_b = '\n'.join(['bravo fact two is here.'] + [f'filler line {i} of page B.' for i in range(10)])
        with mock.patch.dict('os.environ', {'RESEARCH_FOCUS_URL': 'http://x'}), \
             mock.patch.object(focus, '_extract', side_effect=fake_extract):
            a, b = await asyncio.gather(focus.focus_page('alpha', 't', page_a), focus.focus_page('bravo', 't', page_b))
        self.assertEqual(peak, 1, 'daemon-wide gate: one page at a time across sessions')
        self.assertEqual(order, ['alpha', 'bravo'], 'FIFO across sessions')
        self.assertEqual(a[0], 'alpha fact one is here.')
        self.assertEqual(b[0], 'bravo fact two is here.')


class FocusPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_pages_spread_over_servers_in_order_one_page_each(self):
        seen = []; inside = {}
        def fake_extract(url, question, title, window, timeout):
            inside[url] = inside.get(url, 0) + 1
            assert inside[url] == 1, 'one page at a time per server'
            import time; time.sleep(.03)
            seen.append(url); inside[url] -= 1
            return []
        page = '\n'.join(f'line {i} of a page with enough words.' for i in range(10))
        with mock.patch.dict('os.environ', {'RESEARCH_FOCUS_URL': 'http://gpu,http://cpu'}), \
             mock.patch.object(focus, '_extract', side_effect=fake_extract):
            await asyncio.gather(*(focus.focus_page('q', 't', page) for _ in range(4)))
            self.assertEqual(sorted(set(seen)), ['http://cpu', 'http://gpu'])
            await focus.focus_page('q', 't', page)
        self.assertEqual(seen[-1], 'http://gpu', 'an idle pool prefers the first (fastest) server')

    async def test_max_windows_limits_what_is_read_and_reports_it(self):
        long_page = '\n'.join(f'sentence number {i} with some filler words here.' for i in range(600))
        calls = []
        with mock.patch.dict('os.environ', {'RESEARCH_FOCUS_URL': 'http://x', 'RESEARCH_FOCUS_MAX_WINDOWS': '1'}), \
             mock.patch.object(focus, '_extract', side_effect=lambda *a: calls.append(a) or []):
            text, stats = await focus.focus_page('q', 't', long_page)
        self.assertEqual(len(calls), 1); self.assertGreater(stats['skipped_windows'], 0)


class LexicalSafetyNetTests(unittest.IsolatedAsyncioTestCase):
    def test_keyword_lines_are_verbatim_ranked_and_capped(self):
        page = '\n'.join(['Home | Menu | Login',
            'Earth reaches aphelion in early July and perihelion in early January each year.',
            'Our store sells telescopes and binoculars at great prices today.',
            "Earth's tilted axis causes the seasons, not the distance from the Sun.",
            '北半球的夏天其實是遠日點，冬天才是近日點。', 'Contact us'])
        got = focus.lexical_lines(page, '北半球夏天是因為七月最靠近太陽嗎？近日點、遠日點月份', ['NASA seasons perihelion aphelion tilt'])
        self.assertEqual(len(got), 3)
        self.assertTrue(all(g in page for g in got))
        self.assertNotIn('Our store sells telescopes and binoculars at great prices today.', got)

    async def test_model_judging_a_page_irrelevant_still_keeps_keyword_lines(self):
        page = '\n'.join(['Navigation'] * 5 + ["Earth's tilted axis causes the seasons; perihelion is in January."] + ['Footer'] * 5)
        with mock.patch.dict('os.environ', {'RESEARCH_FOCUS_URL': 'http://x'}), \
             mock.patch.object(focus, '_extract', return_value=[]):
            text, stats = await focus.focus_page('why seasons', 't', page, queries=['earth seasons tilt perihelion'])
        self.assertIn("Earth's tilted axis causes the seasons", text)
        self.assertEqual((stats['model_lines'], stats['lexical_lines']), (0, 1))
