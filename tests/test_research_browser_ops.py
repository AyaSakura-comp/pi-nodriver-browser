"""Real operation methods with fake managed tabs; no browser/network startup."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from worker import BrowserWorker, GOOGLE_RESULTS_JS


class BrowserResearchOpsTests(unittest.IsolatedAsyncioTestCase):
    def engine(self, results=None, text='Complete rendered text for offline fixture.'):
        engine = BrowserWorker()
        tab = SimpleNamespace(send=AsyncMock(), get=AsyncMock(), close=AsyncMock())
        async def evaluate(js):
            if js == GOOGLE_RESULTS_JS:
                return results or []
            return 'Fixture title' if js == 'document.title' else text
        tab.evaluate = AsyncMock(side_effect=evaluate)
        engine.create_managed_tab = AsyncMock(return_value=tab)
        engine.create_pool_tab = AsyncMock(return_value=tab)
        engine.close_pool_tab = AsyncMock()
        engine.ensure_browser = AsyncMock()
        engine.wait_for_page_ready = AsyncMock()
        engine.begin_tab_activity = Mock(); engine.end_tab_activity = Mock()
        engine.page_is_pdf = AsyncMock(return_value=False)
        engine.extract_image_candidate_result = AsyncMock(return_value=dict(candidates=[], status='ok', error=None))
        return engine, tab

    async def test_cleanup_only_evicts_exact_job_owner(self):
        engine, owned = self.engine()
        unrelated = SimpleNamespace()
        engine.tab_registry.register(owned,'research-owner','crawl')
        engine.tab_registry.register(unrelated,'interactive-owner','page')
        engine.evict_tab = AsyncMock()
        engine.cleanup_pdf_session = AsyncMock()
        await engine.cleanup_research_owner('research-owner')
        self.assertEqual(engine.evict_tab.await_count,1)
        self.assertIs(engine.evict_tab.call_args.args[0].page,owned)
        engine.cleanup_pdf_session.assert_awaited_once_with('research-owner')

    async def test_raw_exact_google_urls_and_cleanup(self):
        urls = ['https://www.example.com/a/?x=%2f&utm_source=a#z', 'https://example.com/a']
        engine, tab = self.engine([dict(title='Title', url=u, snippet='description') for u in urls])
        result = await engine.search_one(dict(query='fixture', direction='official'), 0, 'owner', asyncio.Semaphore(1))
        self.assertEqual([r['url'] for r in result['results']], urls)
        self.assertTrue(result['ok']); tab.close.assert_awaited_once()
        engine.begin_tab_activity.assert_called_once_with(tab)
        engine.end_tab_activity.assert_called_once_with(tab)

    async def test_google_research_reports_upstream_fallback_truncation(self):
        engine, tab = self.engine([dict(title='Title',url='https://example.com/',snippet='short fallback',truncated=True)])
        result=await engine.search_one(dict(query='q',direction='d'),0,'job',asyncio.Semaphore(1),research=True)
        self.assertTrue(result['results'][0]['truncated'])
        legacy=await engine.search_one(dict(query='q',direction='d'),0,'job',asyncio.Semaphore(1))
        self.assertNotIn('truncated',legacy['results'][0])

    async def test_search_crawl_overlap_with_shared_admission(self):
        engine, tab = self.engine([dict(title='Title', url='https://example.com/', snippet='text')])
        entered, release = asyncio.Event(), asyncio.Event()
        count = 0
        async def navigate(url):
            nonlocal count
            count += 1
            if count == 2: entered.set()
            await release.wait()
        tab.get.side_effect = navigate
        slots = asyncio.Semaphore(2)
        tasks = [asyncio.create_task(engine.search_one(dict(query='fixture', direction='official'), 0, 'job', slots)),
                 asyncio.create_task(engine.crawl_one('https://example.com/', 0, 'job', slots))]
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual(count, 2)
        finally:
            release.set()
            results = await asyncio.gather(*tasks)
        self.assertTrue(all(r['ok'] for r in results))
        self.assertEqual(slots._value, 2)

    async def test_research_crawl_preserves_capped_capture_metadata(self):
        engine, tab = self.engine()
        captured = {'text':'A😀', 'sourceChars':8, 'capturedChars':3,
                    'captureLimit':3, 'captureUnits':'utf16', 'truncated':True}
        async def evaluate(js):
            return 'Fixture' if js == 'document.title' else captured
        tab.evaluate.side_effect = evaluate
        result = await engine.crawl_one('https://example.com/',0,'job',asyncio.Semaphore(1),max_text_units=3)
        self.assertEqual(result['text'],captured['text'])
        for key in captured:
            self.assertEqual(result[key],captured[key])

    async def test_research_capture_uses_json_string_transport_for_nodriver_deep_objects(self):
        import json
        engine, tab = self.engine()
        content = 'Actual readable page content from the browser transport.'
        captured = {'text':content, 'sourceChars':len(content), 'capturedChars':len(content),
                    'captureLimit':100, 'captureUnits':'utf16', 'truncated':False}
        async def evaluate(js):
            if js == 'document.title': return 'Fixture'
            # nodriver's default deep serialization represents an object as
            # key/value entries, not a Python dict. A JSON string is portable.
            if js.startswith('JSON.stringify('): return json.dumps(captured)
            return [[k, {'type':'string', 'value':v}] for k,v in captured.items()]
        tab.evaluate.side_effect = evaluate
        result = await engine.crawl_one('https://example.com/',0,'job',asyncio.Semaphore(1),max_text_units=100)
        self.assertTrue(result['ok'], result.get('error'))
        self.assertEqual(result['text'], content)

    async def test_background_limit_is_shared_across_independent_calls(self):
        engine, tab = self.engine()
        self.assertTrue(hasattr(engine, 'background_slots'))
        engine.background_slots = asyncio.Semaphore(1)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def navigate(url):
            calls.append(url); entered.set(); await release.wait()
        tab.get.side_effect = navigate
        first = asyncio.create_task(engine.crawl_one('https://example.com/a',0,'one',asyncio.Semaphore(2)))
        await entered.wait()
        second = asyncio.create_task(engine.crawl_one('https://example.com/b',0,'two',asyncio.Semaphore(2)))
        barrier=asyncio.Event(); asyncio.get_running_loop().call_soon(barrier.set); await barrier.wait()
        self.assertEqual(len(calls),1)
        release.set(); await asyncio.gather(first,second)
        self.assertEqual(len(calls),2)

    async def test_cancelled_navigation_bounds_stalled_close_for_search_and_crawl(self):
        for operation in ('search','crawl'):
            with self.subTest(operation=operation), patch('worker.BACKGROUND_TAB_CLOSE_TIMEOUT',0.02,create=True):
                engine,tab=self.engine()
                unrelated=SimpleNamespace()
                engine.tab_registry.register(tab,'owned','crawl')
                engine.tab_registry.register(unrelated,'unrelated','page')
                entered,closing=asyncio.Event(),asyncio.Event()
                async def navigate(url): entered.set(); await asyncio.Event().wait()
                async def evict(record): closing.set(); await asyncio.Event().wait()
                tab.get.side_effect=navigate
                engine.evict_tab=AsyncMock(side_effect=evict)
                slots=asyncio.Semaphore(1)
                engine.background_slots=asyncio.Semaphore(1)
                coro=(engine.search_one(dict(query='q',direction='d'),0,'owned',slots,research=True)
                      if operation=='search' else engine.crawl_one('https://example.com/',0,'owned',slots))
                task=asyncio.create_task(coro)
                try:
                    await asyncio.wait_for(entered.wait(),1); task.cancel()
                    await asyncio.wait_for(closing.wait(),1)
                    done,_=await asyncio.wait({task},timeout=0.3)
                    self.assertTrue(done,'cancelled background close retained the shared lock')
                    with self.assertRaises((asyncio.CancelledError,TimeoutError)): task.result()
                    await asyncio.wait_for(engine.tab_management_lock.acquire(),1)
                    engine.tab_management_lock.release()
                    await asyncio.wait_for(slots.acquire(),1); slots.release()
                    await asyncio.wait_for(engine.background_slots.acquire(),1); engine.background_slots.release()
                    self.assertEqual({r.session_id for r in engine.tab_registry.records()},{'owned','unrelated'})
                    self.assertIs(engine.evict_tab.call_args.args[0].page,tab)
                finally:
                    task.cancel(); await asyncio.gather(task,return_exceptions=True)

    async def test_late_owner_cleanup_also_bounds_close_and_preserves_other_owner(self):
        with patch('worker.BACKGROUND_TAB_CLOSE_TIMEOUT',0.02,create=True):
            engine,tab=self.engine()
            engine.tab_registry.register(tab,'owned','crawl')
            unrelated=SimpleNamespace()
            engine.tab_registry.register(unrelated,'unrelated','page')
            async def evict(record): await asyncio.Event().wait()
            engine.evict_tab=AsyncMock(side_effect=evict)
            cleanup=asyncio.create_task(engine.cleanup_research_owner('owned'))
            try:
                done,_=await asyncio.wait({cleanup},timeout=0.3)
                self.assertTrue(done,'late cleanup has no close deadline')
                with self.assertRaises(TimeoutError): cleanup.result()
                self.assertFalse(engine.tab_management_lock.locked())
                self.assertEqual({r.session_id for r in engine.tab_registry.records()},{'owned','unrelated'})
            finally:
                cleanup.cancel(); await asyncio.gather(cleanup,return_exceptions=True)

    async def test_close_failure_still_releases_both_admission_slots(self):
        for operation in ('search','crawl'):
            with self.subTest(operation=operation):
                engine,tab=self.engine([dict(title='Title',url='https://example.com/',snippet='text')])
                tab.close.side_effect=RuntimeError('fixture close failed')
                slots=asyncio.Semaphore(1)
                engine.background_slots=asyncio.Semaphore(1)
                with self.assertRaisesRegex(RuntimeError,'fixture close failed'):
                    if operation=='search':
                        await engine.search_one(dict(query='q',direction='d'),0,'owned',slots)
                    else:
                        await engine.crawl_one('https://example.com/',0,'owned',slots)
                self.assertEqual(slots._value,1)
                self.assertEqual(engine.background_slots._value,1)

    async def test_cancelled_crawl_closes_tab_and_releases_slot(self):
        engine, tab = self.engine()
        entered = asyncio.Event()
        async def navigate(url):
            entered.set(); await asyncio.Event().wait()
        tab.get.side_effect = navigate
        slots = asyncio.Semaphore(1)
        task = asyncio.create_task(engine.crawl_one('https://example.com/', 0, 'job', slots))
        await entered.wait(); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        tab.close.assert_awaited_once(); self.assertEqual(slots._value, 1)
