"""Offline event-gated controller contracts; no browser or model service."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from research.contracts import Limits, SearchResult
from research.controller import ResearchController
from research.controller_contracts import (
    Assessment, CrawlResult, Judgment, Plan, SearchIntent, SourceAction,
)


def intent(query, addresses=('answer',), parent=None, provider='4get'):
    return SearchIntent(query, 'fixture direction', addresses, parent, provider)


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def controller(self, search, planner, judge, crawl=None, **kwargs):
        class Provider:
            async def search(self, task):
                return await search(task)
        async def no_crawl(request):
            raise AssertionError('unexpected crawl')
        return ResearchController(
            job_id='job', question='fixture question', requirements=kwargs.pop('requirements', ('answer',)),
            artifact_root=Path(self.directory.name), providers={'4get': Provider(), 'google': Provider()},
            planner=planner, judge=judge, crawl=crawl or no_crawl, **kwargs)

    async def test_searches_overlap_crawl_overlaps_search_and_full_evidence(self):
        both = asyncio.Event(); crawling = asyncio.Event(); entered = []
        long_text = '完整\n' * 3000
        async def search(task):
            entered.append(task.query)
            if len(entered) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 1)
            if task.query == 'slow':
                await asyncio.wait_for(crawling.wait(), 1)
            return [SearchResult(task.query, 'https://example.com/' + task.query, long_text)]
        async def planner(view):
            if not view.tasks:
                return Plan(view.revision, (intent('fast'), intent('slow')))
            return Plan(view.revision, assessments=(Assessment('answer', tuple(e.event_id for e in view.evidence if not e.truncated)),), finish=True)
        async def judge(request):
            return Judgment(request.view.revision, (SourceAction(request.sources[0].source_id,
                'crawl' if request.task.query == 'fast' else 'use_snippet', ('answer',)),))
        async def crawl(request):
            crawling.set()
            return CrawlResult(long_text + 'extract', truncated=True)
        result = await asyncio.wait_for(self.controller(search, planner, judge, crawl).run(), 3)
        self.assertEqual(result.snapshot.status, 'sufficient')
        self.assertEqual(result.budget_used, 2)
        packet = result.snapshot.render(max_bytes=200000)
        self.assertIn(long_text + 'extract', packet)
        self.assertIn('truncated: True', packet)
        self.assertIn('search_snippet', packet)
        self.assertEqual(result.snapshot.record_count, 2)

    async def test_bfs_barrier_includes_judgments_and_bounds_judges(self):
        two = asyncio.Event(); release = asyncio.Event(); active = 0; peak = 0; starts = []
        async def search(task):
            starts.append(task)
            if task.depth:
                self.assertTrue(release.is_set())
            return [SearchResult(task.query, 'https://example.com/' + task.query, 'text')]
        async def judge(request):
            nonlocal active, peak
            active += 1; peak = max(peak, active)
            if active == 2:
                two.set()
            if not request.task.depth:
                await release.wait()
            active -= 1
            return Judgment(request.view.revision)
        async def planner(view):
            if not view.tasks:
                return Plan(view.revision, tuple(intent(str(i)) for i in range(3)))
            if len(view.tasks) == 3:
                return Plan(view.revision, (intent('child', parent=view.tasks[0].task_id),))
            return Plan(view.revision)
        controller = self.controller(search, planner, judge, limits=Limits(laya_concurrency=2), max_no_progress_rounds=3)
        running = asyncio.create_task(controller.run())
        await asyncio.wait_for(two.wait(), 1)
        self.assertEqual(len(starts), 3)
        self.assertFalse(running.done())
        release.set()
        result = await asyncio.wait_for(running, 2)
        self.assertEqual(peak, 2)
        self.assertEqual([t.depth for t in starts], [0, 0, 0, 1])
        self.assertEqual(result.snapshot.status, 'incomplete')

    async def test_budget_empty_data_and_explicit_cross_provider_query(self):
        dispatched = []
        async def search(task):
            dispatched.append(task)
            return []
        async def planner(view):
            return Plan(view.revision, (intent('same'), intent('same', provider='google'), intent('extra')), finish=True)
        async def judge(request):
            self.fail('empty results must not require judging')
        result = await self.controller(search, planner, judge, limits=Limits(search_budget=2)).run()
        self.assertEqual(result.budget_used, 2)
        self.assertEqual(len(dispatched), 2)
        self.assertEqual({t.provider for t in dispatched}, {'4get', 'google'})
        self.assertEqual(result.snapshot.status, 'incomplete')
        self.assertEqual(result.snapshot.reason, 'budget_exhausted')
        self.assertEqual(result.snapshot.record_count, 0)
        self.assertEqual(result.snapshot.gaps, ('answer',))

    async def test_pending_crawl_blocks_redundant_planning_and_duplicate_url_crawl(self):
        pending = asyncio.Event(); release = asyncio.Event(); calls = 0; crawls = []
        async def search(task):
            return [SearchResult('same', 'https://example.com/exact?x=1', 'snippet')]
        async def planner(view):
            nonlocal calls
            calls += 1
            if not view.tasks:
                return Plan(view.revision, (intent('one'), intent('two')))
            self.assertTrue(release.is_set())
            return Plan(view.revision, assessments=(Assessment('answer', (view.evidence[0].event_id,)),), finish=True)
        async def judge(request):
            return Judgment(request.view.revision, (SourceAction(request.sources[0].source_id, 'crawl', ('answer',)),))
        async def crawl(request):
            crawls.append(request)
            pending.set()
            await release.wait()
            return CrawlResult('entire page')
        running = asyncio.create_task(self.controller(search, planner, judge, crawl).run())
        await asyncio.wait_for(pending.wait(), 1)
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertEqual(calls, 1)
        release.set()
        result = await asyncio.wait_for(running, 2)
        self.assertEqual(len(crawls), 1)
        self.assertEqual(crawls[0].url, 'https://example.com/exact?x=1')
        self.assertEqual(set(result.sources[0].topic_ids), {'t1', 't2'})
        self.assertEqual(result.snapshot.status, 'sufficient')

    async def test_finish_requires_known_nonempty_evidence_and_conflicts_stay_open(self):
        async def search(task):
            return [SearchResult('source', 'https://example.com/', 'captured text')]
        async def judge(request):
            return Judgment(request.view.revision, (SourceAction(request.sources[0].source_id, 'use_snippet', ('answer',)),))
        async def planner(view):
            if not view.tasks:
                return Plan(view.revision, (intent('one'),))
            refs = (view.evidence[0].event_id,)
            return Plan(view.revision, assessments=(Assessment('answer', refs, refs),), finish=True)
        result = await self.controller(search, planner, judge).run()
        self.assertEqual(result.snapshot.status, 'incomplete')
        self.assertEqual(result.requirements[0].contradiction_ids, ('e1',))
        self.assertIn('answer', result.snapshot.gaps)

    async def test_no_progress_stops_unique_queries_not_result_count(self):
        async def search(task):
            return [SearchResult('irrelevant', 'https://example.com/' + task.query, 'ignored')]
        async def judge(request):
            return Judgment(request.view.revision)
        async def planner(view):
            parent = view.tasks[-1].task_id if view.tasks else None
            return Plan(view.revision, (intent(str(len(view.tasks)), parent=parent),))
        result = await self.controller(search, planner, judge, max_no_progress_rounds=2).run()
        self.assertEqual(result.snapshot.reason, 'no_progress')
        self.assertEqual(result.budget_used, 2)

    async def test_cancellation_bounded_drain_and_late_callback_cannot_write(self):
        entered = asyncio.Event(); release = asyncio.Event(); returned = asyncio.Event()
        async def search(task):
            return [SearchResult('source', 'https://example.com/', 'snippet')]
        async def planner(view):
            return Plan(view.revision, (intent('one'),))
        async def judge(request):
            return Judgment(request.view.revision, (SourceAction(request.sources[0].source_id, 'crawl', ('answer',)),))
        async def crawl(request):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()  # Deliberately non-cooperative transport fixture.
            returned.set()
            return CrawlResult('late text must not be written')
        running = asyncio.create_task(self.controller(search, planner, judge, crawl,
            limits=Limits(drain_timeout=0.02)).run())
        await asyncio.wait_for(entered.wait(), 1)
        running.cancel()
        result = await asyncio.wait_for(running, 1)
        before = result.snapshot.path.read_bytes()
        self.assertEqual(result.snapshot.status, 'cancelled')
        self.assertEqual(result.sources[0].status, 'failed')
        release.set()
        await asyncio.wait_for(returned.wait(), 1)
        await asyncio.sleep(0)
        self.assertEqual(result.snapshot.path.read_bytes(), before)
        self.assertNotIn('late text', result.snapshot.render(max_bytes=10000))

    async def test_stale_judgment_revalidated_but_stale_completion_rejected(self):
        first = asyncio.Event(); release = asyncio.Event()
        async def search(task):
            return [SearchResult(task.query, 'https://example.com/' + task.query, task.query)]
        async def judge(request):
            if request.task.query == 'two':
                await release.wait()
            else:
                first.set()
            return Judgment(request.view.revision, (SourceAction(request.sources[0].source_id, 'use_snippet', ('answer',)),),
                assessments=(Assessment('answer', ('fabricated',)),))
        async def planner(view):
            if not view.tasks:
                return Plan(view.revision, (intent('one'), intent('two')))
            return Plan(view.revision - 1, assessments=(Assessment('answer', tuple(e.event_id for e in view.evidence)),), finish=True)
        running = asyncio.create_task(self.controller(search, planner, judge).run())
        await asyncio.wait_for(first.wait(), 1)
        for _ in range(20):
            await asyncio.sleep(0)
        release.set()
        result = await asyncio.wait_for(running, 2)
        self.assertEqual(result.snapshot.record_count, 2)
        self.assertEqual(result.snapshot.status, 'incomplete')
        self.assertTrue(result.errors)

    async def test_drain_timeout_freezes_incomplete_pending_crawl(self):
        async def search(task):
            return [SearchResult('source', 'https://example.com/', 'snippet')]
        async def planner(view):
            return Plan(view.revision, (intent('one'),))
        async def judge(request):
            return Judgment(request.view.revision, (SourceAction(request.sources[0].source_id, 'crawl', ('answer',)),))
        async def crawl(request):
            await asyncio.Event().wait()
        result = await asyncio.wait_for(self.controller(search, planner, judge, crawl,
            limits=Limits(search_budget=1, drain_timeout=0.02)).run(), 1)
        self.assertEqual(result.snapshot.status, 'incomplete')
        self.assertEqual(result.snapshot.reason, 'budget_exhausted')
        records = [json.loads(line) for line in result.snapshot.path.read_text().splitlines()]
        self.assertEqual(records[0]['kind'], 'crawl_failure')
        self.assertEqual(result.sources[0].status, 'failed')
