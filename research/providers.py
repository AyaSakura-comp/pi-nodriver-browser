"""The two search engines: 4get (its DuckDuckGo scraper, local HTTP) and Google in a
temporary tab of the shared browser. Each returns up to 10 (title, url, description)
rows whose URLs are plain http(s), the only URLs research may crawl."""
import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request

import aiohttp


class ProviderError(RuntimeError):
    """A failed search; the message never carries response bodies or transport details."""


def web_url(url):
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port  # raises ValueError for an invalid port
    except ValueError:
        return False
    return (parts.scheme in ('http', 'https') and bool(parts.hostname) and parts.username is None
            and parts.password is None and len(url) <= 8192 and not any(c.isspace() or ord(c) < 32 for c in url))


async def fourget(query, *, port=8088, timeout=10.0, max_bytes=5 * 1024 * 1024):
    """Top 10 results of the local 4get instance, with full descriptions."""
    try:
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False, cookie_jar=aiohttp.DummyCookieJar(),
                headers={'Accept': 'application/json', 'User-Agent': 'pi-agent-web-search/1.0'}) as session:
            async with session.get(f'http://127.0.0.1:{port}/api/v1/web', allow_redirects=False,
                                   params={'s': query, 'scraper': 'ddg', 'country': 'any'}) as response:
                if response.status != 200:
                    raise ProviderError(f'4get HTTP {response.status}')
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body += chunk
                    if len(body) > max_bytes:
                        raise ProviderError('4get response exceeds byte limit')
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise ProviderError('4get network error or timeout') from None
    try:
        items = json.loads(body).get('web')
    except (ValueError, AttributeError):
        items = None
    if not isinstance(items, list):
        raise ProviderError('4get returned no results array')
    rows = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get('url'), str) and web_url(item['url']):
            title, description = item.get('title'), item.get('description')
            rows.append((title.strip() if isinstance(title, str) and title.strip() else 'No Title', item['url'],
                         description if isinstance(description, str) else 'No snippet available.'))
    return rows[:10]


async def google(service, owner, query):
    """Google through the browser service; its opaque /goto links become their targets."""
    result = await service.search_one({'query': query}, 0, owner, asyncio.Semaphore(1))
    if not result['ok']:
        raise ProviderError('google_search_failed')
    rows = result['results'][:10]
    urls = await asyncio.gather(*(asyncio.to_thread(resolve_google_redirect, r['url']) for r in rows))
    return [(r['title'] or 'No Title', url, r['snippet']) for r, url in zip(rows, urls) if web_url(url)]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def resolve_google_redirect(url, timeout=5.0):
    """Google SERP links are opaque /goto or /url tokens. Ask Google for its own
    302 target (one request, no redirect followed) so URL dedup can see that a
    Google result and a 4get result are the same page. Any failure keeps the
    observed URL unchanged."""
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != 'https' or parts.netloc != 'www.google.com' or parts.path not in ('/goto', '/url'):
            return url
        request = urllib.request.Request(url, method='GET', headers={'User-Agent': 'Mozilla/5.0'})
        try:
            with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout):
                return url
        except urllib.error.HTTPError as redirect:
            target = redirect.headers.get('Location', '') if redirect.code in (301, 302, 303, 307, 308) else ''
        target_parts = urllib.parse.urlsplit(target)
        if target_parts.scheme in ('http', 'https') and target_parts.netloc and len(target) <= 2048:
            return target
    except Exception:
        pass
    return url
