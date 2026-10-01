"""Adversarial/offline proposal and shutdown cases for research.controller."""
import asyncio
from dataclasses import FrozenInstanceError
import unittest

import test_research_controller as fixtures
from test_research_controller import intent
from research.contracts import Limits, SearchResult
from research.controller_contracts import Assessment, CrawlResult, Judgment, Plan, SourceAction


# Reuse only fixture setup/helpers, not the base test methods.
class ControllerEdgeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ControllerTests.asyncSetUp
    controller = fixtures.ControllerTests.controller

    async def test_malformed_model_fields_are_rejected_without_crashing(self):
        async def search(task):
            return [SearchResult('fixture', 'https://example.com/', 'full snippet')]
        async def judge(request):
            return Judgment(request.view.revision, (
                SourceAction([], 'crawl', ('answer',)),
                SourceAction('unknown', 'crawl', ('answer',)),
                SourceAction(request.sources[0].source_id, 'use_snippet', ('answer',)),
            ), (Assessment([], ('e1',)),))
        async def planner(view):
            if not view.tasks:
                return Plan(view.revision, (intent('valid'),))
            return Plan(view.revision, assessments=(Assessment('answer', ('invented',)),), finish=True)
        result = await self.controller(search, planner, judge).run()
        self.assertEqual(result.snapshot.status, 'incomplete')
        self.assertEqual(result.snapshot.record_count, 1)
        self.assertTrue(result.errors)

    async def test_repeated_cancellation_still_returns_frozen_partial_artifact(self):
        entered = asyncio.Event(); cancelled = asyncio.Event(); release = asyncio.Event()
        async def search(task):
            return [SearchResult('fixture', 'https://example.com/', 'snippet')]
        async def judge(request):
            return Judgment(request.view.revision, (SourceAction(request.sources[0].source_id, 'crawl', ('answer',)),))
        async def planner(view):
            return Plan(view.revision, (intent('valid'),))
        async def crawl(request):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return CrawlResult('late')
        controller = self.controller(search, planner, judge, crawl, limits=Limits(drain_timeout=0.04))
        running = asyncio.create_task(controller.run())
        await asyncio.wait_for(entered.wait(), 1)
        running.cancel()
        await asyncio.sleep(0)
        running.cancel()
        try:
            result = await asyncio.wait_for(running, 1)
            self.assertEqual(result.snapshot.status, 'cancelled')
            self.assertTrue(result.snapshot.path.exists())
        finally:
            release.set()
            await asyncio.sleep(0)

    async def test_callback_timeout_is_bounded_and_redacts_transport_error(self):
        calls = []
        async def search(task):
            calls.append(task)
            if task.query == 'error':
                raise OSError('secret-token-MUST-NOT-APPEAR')
            await asyncio.Event().wait()
        async def planner(view):
            return Plan(view.revision, (intent('error'), intent('timeout')))
        async def judge(request):
            self.fail('no successful search')
        result = await asyncio.wait_for(self.controller(search, planner, judge,
            callback_timeout=0.02, limits=Limits(search_budget=2)).run(), 1)
        self.assertEqual(result.budget_used, len(calls))
        self.assertEqual(result.budget_used, 2)
        self.assertIn('search:OSError', result.errors)
        self.assertIn('search:callback_timeout', result.errors)
        self.assertNotIn('secret-token', repr(result))

    async def test_only_missing_nonpending_requirements_allow_new_queries(self):
        queries = []
        async def search(task):
            queries.append(task.query)
            return [SearchResult('fixture', 'https://example.com/', 'evidence')]
        async def judge(request):
            return Judgment(request.view.revision, (SourceAction(request.sources[0].source_id, 'use_snippet', ('answer',)),))
        async def planner(view):
            if not view.tasks:
                return Plan(view.revision, (intent('root'), intent('off-topic', ('invented_gap',))))
            return Plan(view.revision, (intent('redundant', parent=view.tasks[0].task_id),),
                (Assessment('answer', (view.evidence[0].event_id,)),), finish=True)
        result = await self.controller(search, planner, judge).run()
        self.assertEqual(queries, ['root'])
        self.assertEqual(result.snapshot.status, 'sufficient')

    async def test_zero_crawl_limit_and_empty_snippet_never_support_finish(self):
        async def search(task):
            return [SearchResult('fixture', 'https://example.com/', '')]
        async def judge(request):
            return Judgment(request.view.revision, (
                SourceAction(request.sources[0].source_id, 'crawl', ('answer',)),
                SourceAction(request.sources[0].source_id, 'use_snippet', ('answer',))))
        async def planner(view):
            if not view.tasks:
                return Plan(view.revision, (intent('root'),))
            return Plan(view.revision, assessments=(Assessment('answer', (view.evidence[0].event_id,)),), finish=True)
        result = await self.controller(search, planner, judge, limits=Limits(max_crawls=0)).run()
        self.assertEqual(result.snapshot.status, 'incomplete')
        self.assertIn('unsupported_evidence_reference', result.errors)

    async def test_callback_inputs_are_frozen_and_controller_is_single_use(self):
        async def search(task):
            return []
        async def judge(request):
            self.fail('empty results')
        async def planner(view):
            with self.assertRaises(FrozenInstanceError):
                view.revision = 123
            with self.assertRaises(FrozenInstanceError):
                view.requirements[0].evidence_ids = ('fake',)
            return Plan(view.revision)
        controller = self.controller(search, planner, judge)
        result = await controller.run()
        with self.assertRaises(RuntimeError):
            await controller.run()
        self.assertEqual(result.budget_used, 0)

    async def test_planner_timeout_is_failed_not_success(self):
        async def search(task):
            self.fail('planner did not supply queries')
        async def judge(request):
            self.fail('no results')
        async def planner(view):
            await asyncio.Event().wait()
        result = await self.controller(search, planner, judge, callback_timeout=0.02).run()
        self.assertEqual(result.snapshot.status, 'failed')
        self.assertEqual(result.snapshot.reason, 'planner_failed')
        self.assertEqual(result.budget_used, 0)
