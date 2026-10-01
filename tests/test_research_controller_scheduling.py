"""Bounded scheduling and late lineage fixtures for the controller owner."""
import asyncio
import unittest

import test_research_controller as fixtures
from test_research_controller import intent
from research.contracts import Limits, SearchResult
from research.controller_contracts import Assessment, CrawlResult, Judgment, Plan, SourceAction


class ControllerSchedulingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ControllerTests.asyncSetUp
    controller = fixtures.ControllerTests.controller

    async def test_pending_gap_excluded_while_other_gap_can_expand(self):
        crawling = asyncio.Event(); child = asyncio.Event(); seen = []
        async def search(task):
            seen.append(task)
            if task.depth:
                self.assertTrue(crawling.is_set())
                child.set()
            return [SearchResult(task.query, 'https://example.com/' + task.query, 'text')]
        async def judge(request):
            return Judgment(request.view.revision, (SourceAction(request.sources[0].source_id,
                'crawl' if request.task.query == 'root' else 'use_snippet', request.task.addresses),))
        async def crawl(request):
            crawling.set()
            await asyncio.wait_for(child.wait(), 1)
            return CrawlResult('page text')
        async def planner(view):
            if not view.tasks:
                return Plan(view.revision, (intent('root', ('a',)),))
            if len(view.tasks) == 1:
                self.assertEqual(view.pending_addresses, ('a',))
                self.assertEqual(view.searchable_gaps, ('b',))
                return Plan(view.revision, (
                    intent('redundant-a', ('a',), view.tasks[0].task_id),
                    intent('child-b', ('b',), view.tasks[0].task_id)))
            assessments = tuple(Assessment(r, tuple(e.event_id for e in view.evidence if r in e.addresses)) for r in ('a', 'b'))
            return Plan(view.revision, assessments=assessments, finish=True)
        result = await asyncio.wait_for(self.controller(search, planner, judge, crawl,
            requirements=('a', 'b')).run(), 2)
        self.assertEqual([t.query for t in seen], ['root', 'child-b'])
        self.assertEqual(result.snapshot.status, 'sufficient')

    async def test_crawl_concurrency_and_count_limits(self):
        two = asyncio.Event(); release = asyncio.Event(); active = 0; peak = 0; count = 0
        async def search(task):
            return [SearchResult(str(i), f'https://example.com/{i}', 'text') for i in range(5)]
        async def judge(request):
            return Judgment(request.view.revision, tuple(SourceAction(s.source_id, 'crawl', ('answer',)) for s in request.sources))
        async def planner(view):
            if not view.tasks:
                return Plan(view.revision, (intent('root'),))
            return Plan(view.revision)
        async def crawl(request):
            nonlocal active, peak, count
            active += 1; count += 1; peak = max(active, peak)
            if active == 2:
                two.set()
            await release.wait()
            active -= 1
            return CrawlResult('text')
        running = asyncio.create_task(self.controller(search, planner, judge, crawl,
            limits=Limits(crawl_concurrency=2, max_crawls=3)).run())
        await asyncio.wait_for(two.wait(), 1)
        self.assertEqual(count, 2)
        release.set()
        result = await asyncio.wait_for(running, 2)
        self.assertEqual(count, 3)
        self.assertEqual(peak, 2)
        self.assertEqual(result.snapshot.record_count, 3)

    async def test_late_shallower_topic_keeps_parent_depth_and_precedes_deeper_work(self):
        depths = []
        async def search(task):
            depths.append(task.depth)
            return []
        async def judge(request):
            self.fail('empty search')
        async def planner(view):
            count = len(view.tasks)
            if count == 0:
                return Plan(view.revision, (intent('root'),))
            if count == 1:
                return Plan(view.revision, (intent('child', parent=view.tasks[0].task_id),))
            if count == 2:
                return Plan(view.revision, (intent('grandchild', parent=view.tasks[1].task_id),))
            if count == 3:
                return Plan(view.revision, (
                    intent('deeper', parent=view.tasks[2].task_id),
                    intent('late-shallow', parent=view.tasks[0].task_id)))
            return Plan(view.revision)
        result = await self.controller(search, planner, judge, max_no_progress_rounds=10).run()
        self.assertEqual(depths, [0, 1, 2, 1, 3])
        self.assertEqual(result.budget_used, 5)

    async def test_same_text_for_distinct_requirements_retains_coverage(self):
        async def search(task):
            return [SearchResult('fixture', 'https://example.com/', 'same complete text')]
        async def judge(request):
            return Judgment(request.view.revision, (SourceAction(request.sources[0].source_id, 'use_snippet', request.task.addresses),))
        async def planner(view):
            if not view.tasks:
                return Plan(view.revision, (intent('a', ('a',)), intent('b', ('b',))))
            return Plan(view.revision, assessments=tuple(Assessment(r,
                tuple(e.event_id for e in view.evidence if r in e.addresses)) for r in ('a', 'b')), finish=True)
        result = await self.controller(search, planner, judge, requirements=('a', 'b')).run()
        self.assertEqual(result.snapshot.status, 'sufficient')
        self.assertEqual(set(result.sources[0].topic_ids), {'t1', 't2'})
