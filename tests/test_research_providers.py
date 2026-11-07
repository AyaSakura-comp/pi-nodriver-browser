"""The search engines against local fixtures; never a live web search."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import urllib.error

from aiohttp import web

from research import providers
from research.providers import ProviderError, fourget, google, resolve_google_redirect, web_url


class FourgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.handler = lambda request: web.json_response({'web': []})

        async def handle(request):
            self.calls.append(dict(request.query))
            response = self.handler(request)
            return await response if asyncio.iscoroutine(response) else response
        app = web.Application()
        app.router.add_get('/api/v1/web', handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def search(self, query='測試 + & 中文', **options):
        return await fourget(query, port=self.port, **options)

    async def test_protocol_url_sanitation_and_full_descriptions(self):
        description = '很長的摘要\n' * 1000
        self.handler = lambda request: web.json_response({'web': [
            {'title': 'A', 'url': 'javascript:bad', 'description': 'bad'},
            {'title': 'B', 'url': 'https://user:pw@example.com/', 'description': 'credentials'},
            {'title': 'C', 'url': 'https://example.com/a b', 'description': 'whitespace'},
            {'title': '官方資料', 'url': 'https://example.com/a?x=1', 'description': description},
            {'title': ' ', 'url': 'http://example.com/b', 'description': None}]})
        self.assertEqual(await self.search(), [('官方資料', 'https://example.com/a?x=1', description),
                                               ('No Title', 'http://example.com/b', 'No snippet available.')])
        self.assertEqual(self.calls[0], {'s': '測試 + & 中文', 'scraper': 'ddg', 'country': 'any'})

    async def test_top_ten_only(self):
        self.handler = lambda request: web.json_response({'web': [
            {'title': str(i), 'url': f'https://example.com/{i}', 'description': 'x'} for i in range(15)]})
        self.assertEqual(len(await self.search()), 10)

    async def test_redirects_errors_and_bad_json_fail_without_the_body(self):
        for response in (web.Response(status=302, headers={'Location': '/other'}), web.Response(text='not json'),
                         web.json_response({'error': 'bad'}), web.json_response({'web': {}}),
                         web.Response(status=503, text='private error body')):
            self.handler = lambda request, response=response: response
            with self.subTest(status=response.status), self.assertRaises(ProviderError) as caught:
                await self.search()
            self.assertNotIn('private error body', str(caught.exception))

    async def test_body_limit_rejects_instead_of_truncating(self):
        self.handler = lambda request: web.Response(text='x' * 2000)
        with self.assertRaisesRegex(ProviderError, 'byte limit'):
            await self.search(max_bytes=100)

    async def test_timeout_and_cancellation(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def block(request):
            entered.set()
            await release.wait()
            return web.json_response({'web': []})
        self.handler = block
        try:
            with self.assertRaisesRegex(ProviderError, 'timeout'):
                await self.search(timeout=0.03)
            entered.clear()
            task = asyncio.create_task(self.search())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            release.set()


class GoogleTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_browser_search_and_redirect_tokens_become_their_targets(self):
        service = SimpleNamespace(search_one=AsyncMock(return_value={'ok': True, 'results': [
            {'title': 'Fixture', 'url': 'https://www.google.com/goto?url=CAES1', 'snippet': 'x' * 600},
            {'title': '', 'url': 'https://www.example.com/a/?q=%2f', 'snippet': 'y'},
            {'title': 'bad', 'url': 'https://www.google.com/goto?url=bad', 'snippet': 'z'}]}))
        targets = {'https://www.google.com/goto?url=CAES1': 'https://nasa.gov/seasons', 'https://www.google.com/goto?url=bad': 'ftp://x'}
        with patch.object(providers, 'resolve_google_redirect', lambda url: targets.get(url, url)):
            found = await google(service, 'research-owner', 'seasons')
        self.assertEqual(found, [('Fixture', 'https://nasa.gov/seasons', 'x' * 600),
                                 ('No Title', 'https://www.example.com/a/?q=%2f', 'y')])
        search, index, owner, slots = service.search_one.await_args.args
        self.assertEqual((search['query'], owner), ('seasons', 'research-owner'))

    async def test_a_failed_search_is_an_error(self):
        service = SimpleNamespace(search_one=AsyncMock(return_value={'ok': False, 'results': [], 'error': 'blocked'}))
        with self.assertRaisesRegex(ProviderError, 'google_search_failed'):
            await google(service, 'owner', 'q')


class RedirectAndUrlTests(unittest.TestCase):
    def test_google_goto_resolves_to_its_302_target_and_failures_keep_the_url(self):
        class Opener:
            def __init__(self, location):
                self.location = location

            def open(self, request, timeout):
                raise urllib.error.HTTPError(request.full_url, 302, 'Found', {'Location': self.location}, None)
        goto = 'https://www.google.com/goto?url=CAES123'
        with patch('urllib.request.build_opener', return_value=Opener('https://spaceplace.nasa.gov/seasons/')):
            self.assertEqual(resolve_google_redirect(goto), 'https://spaceplace.nasa.gov/seasons/')
            self.assertEqual(resolve_google_redirect('https://example.com/a'), 'https://example.com/a')
        with patch('urllib.request.build_opener', return_value=Opener('javascript:alert(1)')):
            self.assertEqual(resolve_google_redirect(goto), goto)
        with patch('urllib.request.build_opener', side_effect=OSError('offline')):
            self.assertEqual(resolve_google_redirect(goto), goto)

    def test_only_plain_web_urls_can_be_crawled(self):
        for url in ('file:///etc/passwd', 'javascript:alert(1)', 'https://user:pw@example.com', 'https://',
                    'https://example.com/\nfoo', 'https://example.com:99999/', 'chrome://settings', 'x' * 9000):
            with self.subTest(url=url):
                self.assertFalse(web_url(url))
        self.assertTrue(web_url('https://example.com/?x=1#f'))
        self.assertTrue(web_url('http://例子.tw:8080/路徑'))


if __name__ == '__main__':
    unittest.main()
