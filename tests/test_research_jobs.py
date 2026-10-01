import asyncio
from dataclasses import asdict
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from research.contracts import SearchResult
from research.controller_contracts import Judgment, SourceAction, ReviewDecision


class ResearchJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_research_page_fetch_deadline_is_three_seconds(self):
        from research.jobs import RESEARCH_FETCH_TIMEOUT
        self.assertEqual(RESEARCH_FETCH_TIMEOUT,3.0)

    async def test_owned_job_provider_routing_streamed_planner_and_separate_crawl(self):
        from research.jobs import ResearchConnection
        search_entered, crawl_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class Provider:
            async def search(self, task):
                if task.query == 'slow':
                    search_entered.set(); await release.wait()
                url = ('https://WWW.EXAMPLE.COM/path/?x=%2f#second' if task.query == 'slow'
                       else 'https://www.example.com/path/?x=%2f#first')
                return [SearchResult('Fixture',url,'description')]
            async def close(self): pass
        extract_calls=0
        async def extract(url, index, session, slots, **options):
            nonlocal extract_calls
            extract_calls+=1
            self.assertEqual(options['max_text_units'],1_000_000)
            self.assertEqual(options['fetch_timeout'],3.0)
            self.assertEqual(url,'https://www.example.com/path/?x=%2f#first')
            self.assertNotEqual(session,'owner')
            crawl_entered.set(); await release.wait()
            return dict(ok=True,text='完整\nfull extract',contentMode='full')
        engine = SimpleNamespace(ensure_browser=AsyncMock(),crawl_one=extract,background_slots=asyncio.Semaphore(2),
                                 cleanup_research_owner=AsyncMock())
        frames=[]
        connection=None
        async def send(frame):
            frames.append(frame)
            if frame['type']=='planner_request':
                view=frame['view']
                searches=[] if view['tasks'] else [dict(query=q,direction='official',provider='4get',addresses=['answer'],parent_task_id=None) for q in ('fast','slow')]
                self.assertNotIn('evidence',view)
                self.assertNotIn('sources',view)
                proposal=dict(revision=view['revision'],searches=searches)
                connection.reply(dict(type='planner_reply',id=frame['id'],jobId=frame['jobId'],requestId=frame['requestId'],proposal=proposal))
        async def judge(req):
            return Judgment(req.view.revision,tuple(SourceAction(s.source_id,'crawl',('answer',)) for s in req.sources))
        async def review(view):
            return ReviewDecision(view.revision,False,.1)
        with tempfile.TemporaryDirectory() as root:
            connection=ResearchConnection(engine,send,Path(root),fourget_factory=Provider,judge=judge,review=review)
            job=asyncio.create_task(connection.run(1,'owner',dict(jobId='job',question='question',provider='4get',searchBudget=2,
                searchConcurrency=2,layaConcurrency=2,clock={'iso':'2026-01-01T00:00:00Z','timezone':'UTC'})))
            try:
                await asyncio.wait_for(search_entered.wait(),2)
                await asyncio.wait_for(crawl_entered.wait(),3)
            finally: release.set()
            result=await asyncio.wait_for(job,3)
            self.assertEqual(result['budgetUsed'],2)
            self.assertTrue(result['fullEvidenceDelivered'])
            self.assertIn('完整\nfull extract',result['text'])
            self.assertEqual(len(result['sources']),1)
            self.assertEqual(extract_calls,1)
            self.assertEqual(result['sources'][0]['url'],'https://www.example.com/path/?x=%2f#first')
            self.assertEqual([r['url'] for r in result['authorizations']], [
                'https://www.example.com/path/?x=%2f#first',
                'https://WWW.EXAMPLE.COM/path/?x=%2f#second'])
            self.assertEqual([r['discoveries'][0]['taskId'] for r in result['authorizations']],['q1','q2'])
            self.assertTrue(all(f['type']=='planner_request' for f in frames))
            self.assertEqual(connection.jobs,{})

    async def test_progressive_packet_prefix_arrives_while_other_page_crawls(self):
        from research.jobs import ResearchConnection
        first_progress, second_release = asyncio.Event(), asyncio.Event()
        class Provider:
            async def search(self, task):
                return [SearchResult(str(i), f'https://example.com/{i}', '台積電 收盤價') for i in (1, 2)]
            async def close(self): pass
        async def extract(url, *args, **kwargs):
            if url.endswith('/2'):
                await second_release.wait()
                return dict(ok=True, text='台積電 收盤價 1190 元', contentMode='full')
            return dict(ok=True, text='台積電 收盤價 1200 元', contentMode='full')
        engine = SimpleNamespace(ensure_browser=AsyncMock(), crawl_one=extract,
                                 cleanup_research_owner=AsyncMock(), research_cleanup_tasks=set())
        frames = []
        conn = None
        async def send(frame):
            frames.append(frame)
            if frame['type'] == 'planner_request':
                view = frame['view']
                conn.reply(dict(type='planner_reply', id=frame['id'], jobId=frame['jobId'], requestId=frame['requestId'],
                                proposal=dict(revision=view['revision'], searches=[dict(
                                    query='台積電 收盤價', direction='official', provider='4get', addresses=['answer'], parent_task_id=None)])))
            elif frame['type'] == 'progress' and frame.get('phase') == 'evidence' and '1200' in frame['prefix']:
                first_progress.set()  # search snippets arrive first; wait for the first crawled page
        with tempfile.TemporaryDirectory() as root:
            conn = ResearchConnection(engine, send, Path(root), fourget_factory=Provider)
            job = asyncio.create_task(conn.run(4, 'owner', dict(jobId='job', question='台積電 收盤價',
                provider='4get', searchBudget=1, searchConcurrency=1, layaConcurrency=1,
                evidence='progressive', clock={'iso':'2026-01-01T00:00:00Z','timezone':'UTC'})))
            try:
                await asyncio.wait_for(first_progress.wait(), 3)
                self.assertFalse(job.done(), 'progress should overlap another in-flight page')
                progress = [f['prefix'] for f in frames if f['type'] == 'progress']
                self.assertIn('Top search results', progress[0], 'top snippets are committed at search time')
                prefix = next(p for p in progress if '1200' in p)
                self.assertTrue(prefix.startswith(progress[0]), 'later prefixes only append')
            finally:
                second_release.set()
            result = await asyncio.wait_for(job, 4)
            self.assertTrue(result['fullEvidenceDelivered'])
            self.assertTrue(result['text'].startswith(prefix))
            self.assertIn('1190', result['text'])

    async def test_progressive_job_survives_unicode_line_separators_in_page_text(self):
        # Crawled pages may carry raw U+2028/U+0085 (json.dumps(ensure_ascii=False) keeps
        # them); str.splitlines() split the evidence JSONL there -> JSONDecodeError.
        from research.jobs import ResearchConnection
        class Provider:
            async def search(self, task):
                return [SearchResult('1', 'https://example.com/1', 'Mac mini 售價')]
            async def close(self): pass
        async def extract(url, *args, **kwargs):
            return dict(ok=True, text='Mac mini 售價\u2028NT$19,900 起\x85教育價\x1c另計', contentMode='full')
        engine = SimpleNamespace(ensure_browser=AsyncMock(), crawl_one=extract,
                                 cleanup_research_owner=AsyncMock(), research_cleanup_tasks=set())
        with tempfile.TemporaryDirectory() as root:
            conn = ResearchConnection(engine, AsyncMock(), Path(root), fourget_factory=Provider)
            result = await asyncio.wait_for(conn.run(5, 'owner', dict(jobId='job', question='Mac mini 售價',
                provider='4get', searchBudget=1, searchConcurrency=1, layaConcurrency=1, queries=['Mac mini 售價'],
                evidence='progressive', clock={'iso':'2026-01-01T00:00:00Z','timezone':'UTC'})), 4)
        self.assertTrue(result['fullEvidenceDelivered'])
        self.assertIn('NT$19,900', result['text'])

    async def test_crawl_top_per_query_crawls_only_each_querys_first_results(self):
        from research.jobs import ResearchConnection
        class Provider:
            async def search(self, task):
                tag='a' if 'alpha' in task.query else 'b'
                return [SearchResult(f'{tag}{i}', f'https://example.com/{tag}/{i}', f'{task.query} {i}') for i in range(5)]
            async def close(self): pass
        crawled=[]
        async def extract(url, *args, **kwargs):
            crawled.append(url)
            return dict(ok=True, text=f'page {url} alpha beta', contentMode='full')
        engine = SimpleNamespace(ensure_browser=AsyncMock(), crawl_one=extract,
                                 cleanup_research_owner=AsyncMock(), research_cleanup_tasks=set())
        with tempfile.TemporaryDirectory() as root, patch('research.jobs.RESEARCH_CRAWL_TOP_PER_QUERY', 2):
            conn = ResearchConnection(engine, AsyncMock(), Path(root), fourget_factory=Provider)
            result = await asyncio.wait_for(conn.run(6, 'owner', dict(jobId='job', question='alpha beta',
                provider='4get', searchBudget=2, searchConcurrency=2, layaConcurrency=1, queries=['alpha', 'beta'],
                evidence='progressive', clock={'iso':'2026-01-01T00:00:00Z','timezone':'UTC'})), 4)
        self.assertEqual(sorted(crawled), ['https://example.com/a/0','https://example.com/a/1',
                                           'https://example.com/b/0','https://example.com/b/1'])
        self.assertTrue(result['fullEvidenceDelivered'])

    async def test_agent_written_queries_skip_the_planner_call(self):
        from research.jobs import ResearchConnection
        searched = []
        class Provider:
            async def search(self, task):
                searched.append(task.query)
                return [SearchResult('r', f'https://example.com/{len(searched)}', '台積電 收盤價')]
            async def close(self): pass
        async def extract(url, *args, **kwargs):
            return dict(ok=True, text='台積電 收盤價 1200 元', contentMode='full')
        engine = SimpleNamespace(ensure_browser=AsyncMock(), crawl_one=extract,
                                 cleanup_research_owner=AsyncMock(), research_cleanup_tasks=set())
        frames = []
        async def send(frame): frames.append(frame)
        with tempfile.TemporaryDirectory() as root:
            conn = ResearchConnection(engine, send, Path(root), fourget_factory=Provider)
            result = await asyncio.wait_for(conn.run(4, 'owner', dict(jobId='job', question='台積電昨天收盤價',
                provider='4get', searchBudget=4, searchConcurrency=4, layaConcurrency=1,
                queries=['台積電 2330 收盤價 2026-09-29', 'TSMC 2330 close price', '台積電 2330 收盤價 2026-09-29'],
                clock={'iso':'2026-01-01T00:00:00Z','timezone':'UTC'})), 5)
        self.assertFalse([f for f in frames if f['type'] == 'planner_request'])
        self.assertEqual(sorted(searched), sorted(['台積電 2330 收盤價 2026-09-29', 'TSMC 2330 close price']))
        self.assertTrue(result['fullEvidenceDelivered'])

    async def test_results_of_a_finished_search_are_crawled_while_another_search_runs(self):
        from research.jobs import ResearchConnection
        slow_release, fast_crawled = asyncio.Event(), asyncio.Event()
        class Provider:
            async def search(self, task):
                if task.query == 'slow':
                    await slow_release.wait()
                    return [SearchResult('slow', 'https://example.com/slow', '台積電 收盤價')]
                return [SearchResult('fast', 'https://example.com/fast', '台積電 收盤價')]
            async def close(self): pass
        async def extract(url, *args, **kwargs):
            if url.endswith('/fast'): fast_crawled.set()
            return dict(ok=True, text='台積電 收盤價 1200 元', contentMode='full')
        engine = SimpleNamespace(ensure_browser=AsyncMock(), crawl_one=extract,
                                 cleanup_research_owner=AsyncMock(), research_cleanup_tasks=set())
        async def send(frame): pass
        with tempfile.TemporaryDirectory() as root:
            conn = ResearchConnection(engine, send, Path(root), fourget_factory=Provider)
            job = asyncio.create_task(conn.run(4, 'owner', dict(jobId='job', question='台積電 收盤價',
                provider='4get', searchBudget=2, searchConcurrency=2, layaConcurrency=1, queries=['fast', 'slow'],
                clock={'iso':'2026-01-01T00:00:00Z','timezone':'UTC'})))
            try:
                await asyncio.wait_for(fast_crawled.wait(), 3)  # before the slow search returns
            finally:
                slow_release.set()
            result = await asyncio.wait_for(job, 5)
        self.assertTrue(result['fullEvidenceDelivered'])

    async def test_prefetched_search_is_reused_by_the_research_job(self):
        from research.jobs import ResearchConnection
        searched = []
        class Provider:
            async def search(self, task):
                searched.append(task.query)
                return [SearchResult(task.query, f'https://example.com/{len(searched)}', '台積電 收盤價')]
            async def close(self): pass
        async def extract(url, *args, **kwargs):
            return dict(ok=True, text='台積電 收盤價 1200 元', contentMode='full')
        engine = SimpleNamespace(ensure_browser=AsyncMock(), crawl_one=extract,
                                 cleanup_research_owner=AsyncMock(), research_cleanup_tasks=set())
        async def send(frame): pass
        with tempfile.TemporaryDirectory() as root:
            conn = ResearchConnection(engine, send, Path(root), fourget_factory=Provider)
            await conn.prefetch('owner', dict(query='台積電 收盤價 A', index=0))
            await conn.prefetch('owner', dict(query='台積電 收盤價 A', index=0))  # duplicate: ignored
            await asyncio.sleep(0.05)
            self.assertEqual(searched, ['台積電 收盤價 A'])
            result = await asyncio.wait_for(conn.run(4, 'owner', dict(jobId='job', question='台積電 收盤價',
                provider='4get', searchBudget=2, searchConcurrency=2, layaConcurrency=1,
                queries=['台積電 收盤價 A', '台積電 收盤價 B'], clock={'iso':'2026-01-01T00:00:00Z','timezone':'UTC'})), 5)
        self.assertEqual(sorted(searched), ['台積電 收盤價 A', '台積電 收盤價 B'], 'A was not searched twice')
        self.assertTrue(result['fullEvidenceDelivered'])

    async def test_streaming_job_takes_queries_while_running_and_waits_for_done(self):
        from research.jobs import ResearchConnection
        searched = []
        class Provider:
            async def search(self, task):
                searched.append(task.query)
                return [SearchResult(task.query, f'https://example.com/{len(searched)}', '台積電 收盤價')]
            async def close(self): pass
        async def extract(url, *args, **kwargs):
            return dict(ok=True, text='台積電 收盤價 1200 元', contentMode='full')
        engine = SimpleNamespace(ensure_browser=AsyncMock(), crawl_one=extract,
                                 cleanup_research_owner=AsyncMock(), research_cleanup_tasks=set())
        async def send(frame): pass
        with tempfile.TemporaryDirectory() as root:
            conn = ResearchConnection(engine, send, Path(root), fourget_factory=Provider)
            job = asyncio.create_task(conn.run(4, 'owner', dict(jobId='job', question='台積電 收盤價',
                provider='4get', searchBudget=4, searchConcurrency=4, layaConcurrency=1,
                queries=['台積電 收盤價 A'], streaming=True, clock={'iso':'2026-01-01T00:00:00Z','timezone':'UTC'})))
            await asyncio.sleep(0.3)
            self.assertFalse(job.done(), 'a streaming job waits for the rest of the queries')
            conn.add_query('owner', dict(jobId='job', query='台積電 收盤價 B', index=1))
            with self.assertRaises(ValueError):
                conn.add_query('intruder', dict(jobId='job', query='x', index=2))
            await asyncio.sleep(0.3)
            self.assertFalse(job.done())
            conn.add_query('owner', dict(jobId='job', done=True))
            result = await asyncio.wait_for(job, 5)
        self.assertEqual(searched, ['台積電 收盤價 A', '台積電 收盤價 B'])
        self.assertTrue(result['fullEvidenceDelivered'])

    async def test_invalid_agent_queries_are_rejected(self):
        from research.jobs import ResearchConnection
        conn = ResearchConnection(SimpleNamespace(), AsyncMock(), Path('/unused'))
        for bad in ([], ['x'] * 5, [''], ['x' * 201], 'q'):
            with self.assertRaises(ValueError):
                await conn.run(1, 'o', dict(jobId='job', question='q', provider='4get', searchBudget=1,
                    searchConcurrency=1, layaConcurrency=1, queries=bad, clock={'iso':'2026-01-01T00:00:00Z','timezone':'UTC'}))

    async def test_timed_out_target_cleanup_is_cancelled_to_release_shared_locks(self):
        from research.jobs import ResearchConnection
        cleaned=asyncio.Event()
        async def cleanup(owner):
            try: await asyncio.Event().wait()
            finally: cleaned.set()
        class Provider:
            async def close(self): pass
        engine=SimpleNamespace(cleanup_research_owner=cleanup,research_cleanup_tasks=set())
        conn=None
        async def send(frame):
            conn.reply(dict(type='planner_reply',id=1,jobId='job',requestId=frame['requestId'],
                proposal=dict(revision=frame['view']['revision'],searches=[])))
        with tempfile.TemporaryDirectory() as root:
            conn=ResearchConnection(engine,send,Path(root),fourget_factory=Provider)
            try:
                with self.assertRaisesRegex(ValueError,'research_cleanup_incomplete'):
                    await asyncio.wait_for(conn.run(1,'owner',dict(jobId='job',question='q',provider='4get',searchBudget=1,
                        searchConcurrency=1,layaConcurrency=1,clock={'iso':'2026-01-01T00:00:00Z','timezone':'UTC'})),4)
                self.assertTrue(cleaned.is_set(),'timed out cleanup retained a cooperative shared lock')
            finally:
                pending=tuple(engine.research_cleanup_tasks)
                for task in pending: task.cancel()
                await asyncio.gather(*pending,return_exceptions=True)

    async def test_repeated_cancellation_cannot_interrupt_owned_cleanup(self):
        from research.jobs import ResearchConnection
        entered,release,planned=asyncio.Event(),asyncio.Event(),asyncio.Event()
        async def cleanup(owner): entered.set(); await release.wait()
        class Provider:
            async def close(self): pass
        engine=SimpleNamespace(cleanup_research_owner=cleanup,research_cleanup_tasks=set())
        async def send(frame): planned.set()
        with tempfile.TemporaryDirectory() as root:
            conn=ResearchConnection(engine,send,Path(root),fourget_factory=Provider)
            task=asyncio.create_task(conn.run(1,'owner',dict(jobId='job',question='q',provider='4get',searchBudget=1,
                searchConcurrency=1,layaConcurrency=1,clock={'iso':'2026-01-01T00:00:00Z','timezone':'UTC'})))
            await asyncio.wait_for(planned.wait(),2); task.cancel()
            await asyncio.wait_for(entered.wait(),2); task.cancel()
            barrier=asyncio.Event(); asyncio.get_running_loop().call_soon(barrier.set); await barrier.wait()
            try: self.assertFalse(task.done(),'repeated cancellation interrupted cleanup')
            finally:
                release.set(); await asyncio.gather(task,return_exceptions=True)
            self.assertFalse(conn.jobs)

    async def test_google_routes_one_query_and_preserves_exact_provider_authority(self):
        from research.jobs import _Google, _RegisteredProvider
        from research.contracts import SearchTask
        engine=SimpleNamespace(ensure_browser=AsyncMock(),search_one=AsyncMock(return_value={
            'ok':True,'results':[{'title':'Fixture','url':'https://www.example.com/a/?q=%2f','snippet':'x'*600}]}))
        authority={}; accepting=[True]
        provider=_RegisteredProvider(_Google(engine,'job-owner'),authority,'google',accepting)
        task=SearchTask('q1','t1','query','official','google')
        rows=await provider.search(task)
        self.assertEqual(engine.search_one.await_count,1)
        self.assertEqual(rows[0].url,'https://www.example.com/a/?q=%2f')
        self.assertTrue(rows[0].truncated)
        self.assertEqual(authority[rows[0].url],[{'taskId':'q1','provider':'google'}])
        accepting[0]=False
        engine.search_one.return_value['results'][0]['url']='https://example.com/new'
        with self.assertRaisesRegex(ValueError,'research_frozen'):
            await provider.search(task)
        self.assertNotIn('https://example.com/new',authority)
        self.assertEqual(engine.search_one.await_count,1)

    async def test_unsolicited_reply_and_unknown_provider_fail_before_work(self):
        from research.jobs import ResearchConnection
        connection=ResearchConnection(SimpleNamespace(),AsyncMock(),Path('/unused'))
        with self.assertRaises(ValueError): connection.reply(dict(type='planner_reply',id=1,jobId='other',requestId='p1',proposal={}))
        with self.assertRaises(ValueError):
            await connection.run(1,'owner',dict(jobId='job',question='q',provider='unknown',searchBudget=1,
                searchConcurrency=1,layaConcurrency=1,clock={'iso':'fixture','timezone':'UTC'}))
