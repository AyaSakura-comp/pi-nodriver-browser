"""A real core-only vertical slice, using a local 4get fixture, not Laya/Chrome.

The production controller/model bindings are intentionally not emulated here.
This proves the reusable primitives work together before integrating those parts.
"""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from aiohttp import web
from research.contracts import SearchTask, SearchResult
from research.budget import QueryBudget
from research.frontier import Frontier
from research.registry import SourceRegistry
from research.providers import FourgetProvider, SearchBatch
from research.evidence import EvidenceWriter


class CorePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_bfs_evidence_snapshot_without_text_trimming(self):
        entered = set()
        two_queries = asyncio.Event()
        async def search(request):
            query = request.query['s']
            entered.add(query)
            if len(entered) == 2:
                two_queries.set()
            # A serial implementation cannot pass this barrier.
            await asyncio.wait_for(two_queries.wait(), 1)
            return web.json_response({'web': [dict(title='來源', url='https://example.com/source',
                                                   description=query + '\n' + '完整文字' * 2000)]})
        app = web.Application(); app.router.add_get('/api/v1/web', search)
        runner = web.AppRunner(app); await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0); await site.start()
        provider = FourgetProvider(port=site._server.sockets[0].getsockname()[1])
        try:
            with tempfile.TemporaryDirectory() as d:
                frontier = Frontier(); registry = SourceRegistry('job')
                tasks = [SearchTask('q1', 't1', 'first', 'official'), SearchTask('q2', 't2', 'second', 'independent')]
                for task in tasks:
                    frontier.add(task)
                batch = SearchBatch({'4get': provider}, QueryBudget(2))
                async with EvidenceWriter(Path(d), 'job') as writer:
                    async for outcome in batch.run(frontier.take(2)):
                        self.assertIsNone(outcome.error)
                        for result in outcome.results:
                            source, _ = registry.discover(result, outcome.task.topic_id, outcome.task.task_id, outcome.task.provider)
                            await writer.append(dict(eventId=outcome.task.task_id, sourceId=source.source_id,
                                topicIds=[outcome.task.topic_id], kind='search_snippet', url=result.url,
                                title=result.title, text=result.description, status='completed', truncated=False))
                        frontier.complete(outcome.task.task_id)
                    snapshot = await writer.freeze(status='incomplete', reason='no_planner_attached', gaps=['verify answer'])
                self.assertTrue(frontier.idle)
                self.assertEqual(len(registry.sources), 1)
                self.assertEqual(set(registry.sources[0].topic_ids), {'t1', 't2'})
                self.assertEqual(batch.budget.used, 2)
                packet = snapshot.render(max_bytes=200000)
                self.assertIn('first\n' + '完整文字' * 2000, packet)
                self.assertIn('second\n' + '完整文字' * 2000, packet)
                self.assertEqual(snapshot.record_count, 2)
        finally:
            await provider.close(); await runner.cleanup()

    async def test_iterator_close_cancels_and_joins_pending_provider_work(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        class Provider:
            async def search(self, task):
                if task.task_id == 'slow':
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        stopped.set()
                else:
                    await started.wait()
                    return [SearchResult('title', 'https://example.com/', 'text')]
        batch = SearchBatch({'4get': Provider()}, QueryBudget(3), concurrency=2)
        tasks = [SearchTask(id, id, id, 'd') for id in ('slow', 'fast', 'never-sent')]
        stream = batch.run(tasks)
        outcome = await asyncio.wait_for(anext(stream), 1)
        self.assertEqual(outcome.task.task_id, 'fast')
        await asyncio.wait_for(stream.aclose(), 1)
        self.assertTrue(stopped.is_set())
        self.assertEqual(batch.budget.used, 2)
        self.assertEqual(batch.budget.attempts, ('slow', 'fast'))

    async def test_explicit_retry_consumes_another_attempt(self):
        class Provider:
            async def search(self, task):
                raise OSError('do not persist transport secrets')
        b = SearchBatch({'4get': Provider()}, QueryBudget(2))
        first = SearchTask('q1', 't', 'same query', 'd')
        retry = SearchTask('retry', 't', 'same query', 'd')
        results = [r async for r in b.run([first])]
        results += [r async for r in b.run([retry], allow_repeated_queries=True)]
        self.assertEqual(b.budget.used, 2)
        self.assertEqual(len(results), 2)
        self.assertEqual({r.error for r in results}, {'OSError'})
