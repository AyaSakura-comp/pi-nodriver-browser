"""Local HTTP fixture tests; never send a live web search."""
import asyncio
import unittest
from aiohttp import web
from research.contracts import SearchTask


class FourgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from research.providers import FourgetProvider, SearchBatch
        self.Provider, self.Batch = FourgetProvider, SearchBatch
        self.calls = []
        self.handler = lambda req: web.json_response({'web': []})
        async def handle(req):
            self.calls.append(dict(req.query))
            response = self.handler(req)
            return await response if asyncio.iscoroutine(response) else response
        app = web.Application()
        app.router.add_get('/api/v1/web', handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        self.providers = []

    async def asyncTearDown(self):
        for p in self.providers:
            await p.close()
        await self.runner.cleanup()

    def client(self, **kw):
        p = self.Provider(port=self.port, **kw)
        self.providers.append(p)
        return p

    def task(self, id='q1', provider='4get'):
        return SearchTask(id, id, f'測試 {id} + & 中文', 'official', provider=provider)

    async def test_protocol_sanitation_and_full_description(self):
        description = '很長的摘要\n' * 1000
        self.handler = lambda req: web.json_response({'web': [
            {'title': 'A', 'url': 'javascript:bad', 'description': 'bad'},
            {'title': '官方資料', 'url': 'https://example.com/a?x=1', 'description': description},
        ]})
        result = await self.client().search(self.task())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].description, description)
        self.assertEqual(result[0].url, 'https://example.com/a?x=1')
        self.assertEqual(self.calls[0], {'s': self.task().query, 'scraper': 'ddg', 'country': 'any'})

    async def test_top_ten_only(self):
        self.handler = lambda req: web.json_response({'web': [
            {'title': str(i), 'url': f'https://example.com/{i}', 'description': 'x'} for i in range(15)]})
        result = await self.client().search(self.task())
        self.assertEqual(len(result), 10)

    async def test_redirects_and_bad_json_fail_explicitly(self):
        from research.providers import ProviderError
        for response in (web.Response(status=302, headers={'Location': '/other'}),
                         web.Response(text='not json'), web.json_response({'error': 'bad'}),
                         web.Response(status=503, text='private error body')):
            self.handler = lambda req, response=response: response
            with self.assertRaises(ProviderError) as e:
                await self.client().search(self.task())
            self.assertNotIn('private error body', str(e.exception))

    async def test_body_limit_rejects_instead_of_truncating(self):
        from research.providers import ProviderError
        self.handler = lambda req: web.Response(text='x' * 2000)
        with self.assertRaises(ProviderError):
            await self.client(max_body_bytes=100).search(self.task())

    async def test_timeout_and_cancellation(self):
        from research.providers import ProviderError
        entered, release = asyncio.Event(), asyncio.Event()
        async def block(req):
            entered.set(); await release.wait()
            return web.json_response({'web': []})
        self.handler = block
        try:
            with self.assertRaises(ProviderError):
                await self.client(timeout=0.03).search(self.task())
            task = asyncio.create_task(self.client().search(self.task('q2')))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            release.set()

    async def test_parallel_batch_budget_and_early_result_delivery(self):
        from research.budget import QueryBudget
        started, release = asyncio.Event(), asyncio.Event()
        async def handle(req):
            if len(self.calls) >= 2:
                started.set()
            if 'slow' in req.query['s']:
                await release.wait()
            return web.json_response({'web': []})
        self.handler = handle
        batch = self.Batch({'4get': self.client()}, QueryBudget(2), concurrency=3)
        jobs = [self.task('slow'), self.task('fast'), self.task('not-sent')]
        results = batch.run(jobs)
        try:
            first = await asyncio.wait_for(anext(results), 1)
            await asyncio.wait_for(started.wait(), 1)
            self.assertEqual(first.task.task_id, 'fast')
            self.assertIsNone(first.error)
            self.assertEqual(batch.budget.used, 2)
            self.assertEqual(len(self.calls), 2)
            release.set()
            rest = [r async for r in results]
            self.assertEqual([r.task.task_id for r in rest], ['slow'])
        finally:
            release.set()
            await results.aclose()

    async def test_duplicate_query_is_not_dispatched_or_charged(self):
        from research.budget import QueryBudget
        batch = self.Batch({'4get': self.client()}, QueryBudget(3))
        a = self.task('q1')
        b = SearchTask('q2', 'q2', a.query.upper(), 'other')
        self.assertEqual(len([r async for r in batch.run([a, b])]), 1)
        self.assertEqual(batch.budget.used, 1)
        self.assertEqual(len(self.calls), 1)

    async def test_one_failure_does_not_cancel_sibling_queries(self):
        from research.budget import QueryBudget
        self.handler = lambda req: web.Response(status=500) if 'bad' in req.query['s'] else web.json_response({'web': []})
        batch = self.Batch({'4get': self.client()}, QueryBudget(2))
        results = [r async for r in batch.run([self.task('bad'), self.task('good')])]
        self.assertEqual(sum(r.error is not None for r in results), 1)
        self.assertEqual(batch.budget.used, 2)

    async def test_unsupported_provider_fails_before_budget_reservation(self):
        from research.budget import QueryBudget
        batch = self.Batch({'4get': self.client()}, QueryBudget(2))
        with self.assertRaises(ValueError):
            _ = [r async for r in batch.run([self.task('q1', provider='google')])]
        self.assertEqual(batch.budget.used, 0)
