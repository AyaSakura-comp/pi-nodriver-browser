"""Offline Laya protocol/concurrency tests using a loopback HTTP fixture."""
import asyncio
import unittest
from aiohttp import web


class LayaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from research.decisions import LayaClient
        self.Client = LayaClient
        self.requests = []
        self.handler = lambda body: {'answers': {'next': {'probabilities': {'crawl': 0.7, 'none_match': 0.3}}}}
        async def handle(req):
            body = await req.json(); self.requests.append(body)
            result = self.handler(body)
            if asyncio.iscoroutine(result):
                result = await result
            return web.json_response(result)
        app = web.Application(); app.router.add_post('/v1/systemone', handle)
        self.runner = web.AppRunner(app); await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0); await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        self.clients = []

    async def asyncTearDown(self):
        for c in self.clients:
            await c.close()
        await self.runner.cleanup()

    def client(self, **kw):
        c = self.Client(port=self.port, **kw); self.clients.append(c); return c

    async def test_wire_format_ranked_scores_not_a_finish_decision(self):
        c = self.client()
        r = await c.choose('goal + candidate evidence', {'crawl': 'Read the official page'})
        self.assertEqual(r.ranked, (('crawl', 0.7), ('none_match', 0.3)))
        self.assertGreaterEqual(r.elapsed_ms, 0)
        body = self.requests[0]
        self.assertEqual(body['state'], 'goal + candidate evidence')
        self.assertEqual(body['questions']['next']['type'], 'choice')
        self.assertIn('none_match', body['questions']['next']['criteria'])
        self.assertFalse(hasattr(r, 'sufficient'))

    async def test_invalid_probabilities_are_not_accepted(self):
        from research.decisions import DecisionError
        bad = [ {'crawl': 0.9, 'invented_url': 0.1}, {'crawl': 1.5, 'none_match': -0.5},
                {'crawl': True, 'none_match': 0}, {'crawl': float('nan'), 'none_match': 1},
                {'crawl': 0.6}, {'crawl': 0.9, 'none_match': 0.9},
                {'crawl': 10**400, 'none_match': 0},
                {'crawl': -(10**400), 'none_match': 1} ]
        for probs in bad:
            self.handler = lambda body, p=probs: {'answers': {'next': {'probabilities': p}}}
            with self.subTest(probs=probs), self.assertRaises(DecisionError):
                await self.client().choose('s', {'crawl': 'x'})

    async def test_two_judgments_can_overlap(self):
        two_entered = asyncio.Event()
        async def block(body):
            if len(self.requests) == 2:
                two_entered.set()
            await asyncio.wait_for(two_entered.wait(), 1)
            return {'answers': {'next': {'probabilities': {'crawl': 0.7, 'none_match': 0.3}}}}
        self.handler = block
        c = self.client(concurrency=2)
        rs = await asyncio.wait_for(asyncio.gather(*(c.choose(str(i), {'crawl': 'read'}) for i in range(2))), 2)
        self.assertEqual(len(rs), 2)

    async def test_concurrency_limit_is_enforced(self):
        first_entered, release = asyncio.Event(), asyncio.Event()
        async def block(body):
            first_entered.set(); await release.wait()
            return {'answers': {'next': {'probabilities': {'crawl': 0.7, 'none_match': 0.3}}}}
        self.handler = block
        c = self.client(concurrency=1)
        one = asyncio.create_task(c.choose('one', {'crawl': 'read'}))
        await asyncio.wait_for(first_entered.wait(), 1)
        two_started = asyncio.Event()
        async def second():
            two_started.set()
            return await c.choose('two', {'crawl': 'read'})
        two = asyncio.create_task(second())
        try:
            await two_started.wait()
            self.assertEqual(len(self.requests), 1)
            release.set(); await asyncio.gather(one, two)
            self.assertEqual(len(self.requests), 2)
        finally:
            release.set(); await asyncio.gather(one, two, return_exceptions=True)

    async def test_rejects_oversized_state_without_trimming_or_request(self):
        with self.assertRaises(ValueError):
            await self.client(max_state_chars=10).choose('x' * 11, {'crawl': 'read'})
        self.assertEqual(self.requests, [])

    async def test_invalid_candidates_and_configuration(self):
        for candidates in ({}, {'none_match': 'override'}, {'crawl': ''}, {'bad key': 'x'}):
            with self.subTest(candidates=candidates), self.assertRaises(ValueError):
                await self.client().choose('s', candidates)
        for kw in (dict(concurrency=0), dict(concurrency=True), dict(timeout=float('inf'))):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                self.client(**kw)
