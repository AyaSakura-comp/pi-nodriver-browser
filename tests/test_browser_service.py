"""BrowserService with a fake Chrome: tab hygiene, research engine API, self-healing."""
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import worker
from pibrowser.cdp import CDPTimeout


class FakeTab:
    def __init__(self, chrome, target_id):
        self.chrome = chrome
        self.target_id = target_id
        self.created = time.monotonic()
        self.dialogs = []
        self.world = None
        self.url = 'about:blank'
        self.chooser = None
        self.other_apps = []
        self.auth_asked = None
        self.accept_dialogs = False

    async def watch_outside(self):
        pass

    async def print_calls(self):
        return 0

    async def send(self, method, params=None, *, timeout=10.0):
        return {}

    async def goto(self, url, *, timeout=20.0):
        self.url = url
        await self.chrome.navigate_hook(self, url)

    async def watch_network(self):
        pass

    async def ready_state(self, timeout=3.0):
        return ['complete', 0, 0, self.url]

    async def wait_ready(self, timeout=10.0, *, quiet=0.5, min_chars=0, blank_ok=True, **more):
        return True

    async def eval(self, expression, *, timeout=10.0):
        return self.chrome.eval_hook(self, expression)

    async def http_status(self):
        return self.chrome.status

    async def snapshot(self, *, base=0):
        self.chrome.bases.append(base)
        return {'url': self.url, 'title': 'T', 'text': f'page {self.url}', 'elements': 0, 'doc': 'd' + self.url[8],
                'lastRef': base + 5, 'links': {f'e{base + 1}': self.url}}

    async def back(self):
        return False  # the tab's first page

    async def close(self):
        await self.chrome.close_target(self.target_id)


class FakeConn:
    async def send(self, method, params=None, *, session=None, timeout=10.0):
        return {}


class FakeChrome:
    made = []

    def __init__(self, profile, log_path, downloads=None):
        self.conn = FakeConn()
        self.downloads = {}
        self.bases = []
        self.targets = {}
        self.closed = []
        self.probe_ok = True
        self.alive_flag = True
        self.stopped = False
        self.proc = SimpleNamespace(pid=4242)
        self.port = 9222
        self.counter = 0
        self.body = 'Readable body text that is long enough to count as content.'
        self.status = 200
        self.navigate_hook = self._navigate
        FakeChrome.made.append(self)

    async def _navigate(self, tab, url):
        return None

    def eval_hook(self, tab, expression):
        if expression == 'document.title':
            return 'Fixture title'
        if expression == 'document.contentType':
            return 'text/html'
        if 'sourceChars' in expression or 'capturedChars' in expression:
            return {'text': self.body, 'sourceChars': len(self.body), 'capturedChars': len(self.body),
                    'captureLimit': 1000, 'captureUnits': 'codepoints'}
        if expression is worker.IMAGE_CANDIDATES_JS:
            return []
        if expression is worker.GOOGLE_RESULTS_JS:
            return [{'title': 'Result', 'url': 'https://example.com/a', 'snippet': 's' * 600}]
        if 'innerText' in expression:
            return self.body
        return None

    async def start(self):
        return None

    def alive(self):
        return self.alive_flag

    async def probe(self, timeout=5.0):
        return self.probe_ok

    async def new_tab(self, *, new_window=False):
        self.counter += 1
        target_id = f't{self.counter}'
        self.targets[target_id] = {'targetId': target_id, 'type': 'page', '_seen': time.monotonic()}
        return FakeTab(self, target_id)

    async def close_target(self, target_id, timeout=3.0):
        self.targets.pop(target_id, None)
        self.closed.append(target_id)

    def pages(self):
        return list(self.targets.values())

    async def stop(self):
        self.stopped = True
        self.alive_flag = False


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeChrome.made.clear()
        self.patches = [mock.patch.object(worker, 'Chrome', FakeChrome),
                        mock.patch.object(worker, 'log_event', lambda *a, **k: None)]
        for patch in self.patches:
            patch.start()
        self.service = worker.BrowserService()
        await self.service.ensure_browser()
        self.chrome = self.service.browser

    async def asyncTearDown(self):
        for patch in self.patches:
            patch.stop()

    async def test_research_crawl_returns_capture_metadata_and_closes_its_tab(self):
        result = await self.service.crawl_one('https://example.com/', 0, 'owner', asyncio.Semaphore(1),
                                              max_text_units=1000, fetch_timeout=3.0)
        self.assertTrue(result['ok'])
        self.assertEqual(result['title'], 'Fixture title')
        self.assertEqual(result['captureUnits'], 'codepoints')
        self.assertEqual(result['sourceChars'], len(self.chrome.body))
        self.assertEqual(self.chrome.targets, {})
        self.assertEqual(self.service.temp_tabs, {})

    async def test_cancelled_crawl_closes_tab_and_releases_slots(self):
        entered = asyncio.Event()

        async def hang(tab, url):
            entered.set()
            await asyncio.sleep(3600)
        self.chrome.navigate_hook = hang
        slots = asyncio.Semaphore(1)
        task = asyncio.create_task(self.service.crawl_one('https://slow.example/', 0, 'owner', slots,
                                                          max_text_units=1000, fetch_timeout=3.0))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.chrome.targets, {})
        self.assertFalse(slots.locked())
        self.assertEqual(self.service.crawl_slots._value, worker.CRAWL_TABS)

    async def test_slow_page_times_out_as_browser_suspect(self):
        async def hang(tab, url):
            await asyncio.sleep(3600)
        self.chrome.navigate_hook = hang
        result = await self.service.crawl_one('https://slow.example/', 0, 'owner', asyncio.Semaphore(1),
                                              max_text_units=1000, fetch_timeout=1.0)
        self.assertFalse(result['ok'])
        self.assertTrue(result['browserSuspect'])
        self.assertEqual(self.chrome.targets, {})

    async def test_challenge_page_is_reported_not_returned_as_content(self):
        self.chrome.body = 'Just a moment... Verify you are human by completing the action below.'
        result = await self.service.crawl_one('https://cf.example/', 0, 'owner', asyncio.Semaphore(1))
        self.assertFalse(result['ok'])
        self.assertIn('challenge', result['error'])

    async def test_error_status_pages_are_failures(self):
        self.chrome.status = 404
        result = await self.service.crawl_one('https://x.example/missing', 0, 'owner', asyncio.Semaphore(1))
        self.assertFalse(result['ok'])
        self.assertIn('HTTP 404', result['error'])

    async def test_google_search_parses_results_and_closes_its_tab(self):
        result = await self.service.search_one({'query': 'q', 'direction': 'official'}, 0, 'owner',
                                               asyncio.Semaphore(1))
        self.assertTrue(result['ok'])
        self.assertEqual(result['results'][0]['url'], 'https://example.com/a')
        self.assertEqual(self.chrome.targets, {})

    async def test_owner_cleanup_closes_only_that_owners_temp_tabs(self):
        mine = await self.service._temp_tab('job-a')
        other = await self.service._temp_tab('job-b')
        await self.service.cleanup_research_owner('job-a')
        self.assertNotIn(mine.target_id, self.chrome.targets)
        self.assertIn(other.target_id, self.chrome.targets)

    async def test_failed_probe_replaces_the_browser(self):
        first = self.service.browser
        first.probe_ok = False
        await self.service.ensure_browser()
        self.assertTrue(first.stopped)
        self.assertIsNot(self.service.browser, first)
        self.assertEqual(self.service.restarts, 1)

    async def test_a_click_adopts_its_popup_but_never_chrome_ui_or_other_tabs(self):
        tab = await self.service.session_tab('s1', create=True)
        started = time.monotonic() - 1
        self.chrome.targets['ui'] = {'targetId': 'ui', 'type': 'page', '_seen': time.monotonic(),
                                     'url': 'chrome://omnibox-popup.top-chrome/'}
        crawl = await self.service._temp_tab('job-a')
        self.chrome.targets[crawl.target_id]['url'] = 'https://example.com/crawl'
        self.assertIsNone(self.service._new_popup(tab.target_id, started))
        self.chrome.targets['pop'] = {'targetId': 'pop', 'type': 'page', '_seen': time.monotonic(),
                                      'url': 'about:blank', 'openerId': tab.target_id}
        self.assertEqual(self.service._new_popup(tab.target_id, started)['targetId'], 'pop')

    async def test_a_slow_but_working_browser_is_kept(self):
        slow = self.service.browser
        timeouts = []

        async def probe(timeout=5.0):
            timeouts.append(timeout)
            return timeout > 5.0  # misses 5 s on a busy machine, answers the long probe
        slow.probe = probe
        await self.service.ensure_browser()
        self.assertIs(self.service.browser, slow)
        self.assertEqual(self.service.restarts, 0)
        self.assertEqual(timeouts, [5.0, worker.SLOW_PROBE_SECONDS])

    async def test_recent_probe_is_reused_within_max_age(self):
        with mock.patch.object(FakeChrome, 'probe', side_effect=AssertionError('probed')):
            await self.service.ensure_browser(max_age=60)

    async def test_idle_health_check_replaces_a_wedged_browser(self):
        wedged = self.service.browser
        wedged.probe_ok = False
        self.service.last_activity -= 60
        with mock.patch.object(worker, 'HEALTH_INTERVAL', 0.01), \
             mock.patch.object(worker, 'IDLE_PROBE_AGE', 0.0):
            loop = asyncio.create_task(self.service.health_loop())
            for _ in range(100):
                await asyncio.sleep(0.01)
                if self.service.browser is not wedged:
                    break
            loop.cancel()
        self.assertIsNot(self.service.browser, wedged)

    async def test_open_retries_once_on_a_fresh_browser_after_a_page_timeout(self):
        first = self.service.browser

        async def wedge(tab, url):
            first.probe_ok = False
            raise CDPTimeout('Page.navigate timed out after 20s')
        first.navigate_hook = wedge
        result = await self.service.execute('open https://example.com/', 's1')
        self.assertEqual(result['action'], 'open')
        self.assertIsNot(self.service.browser, first)

    async def test_session_reuses_its_tab_and_cleanup_closes_it(self):
        await self.service.execute('open https://a.example/', 's1')
        await self.service.execute('open https://b.example/', 's1')
        self.assertEqual(len(self.chrome.targets), 1)
        await self.service.execute('session-cleanup', 's1')
        self.assertEqual(self.chrome.targets, {})

    async def test_idle_session_tabs_are_closed(self):
        await self.service.execute('open https://a.example/', 's1')
        self.service.sessions['s1'].last_used -= worker.IDLE_TAB_SECONDS + 1
        await self.service.close_idle_tabs()
        self.assertEqual(self.chrome.targets, {})

    async def test_session_tab_cap_evicts_least_recently_used_session(self):
        with mock.patch.object(worker, 'MAX_SESSION_TABS', 2):
            for session in ('s1', 's2', 's3'):
                await self.service.execute('open https://a.example/', session)
                self.service.sessions[session].last_used = time.monotonic()
                await asyncio.sleep(0.01)
        self.assertEqual(len(self.chrome.targets), 2)
        self.assertEqual(self.service.sessions['s1'].stack, [])

    async def test_page_comes_back_after_its_tab_died(self):
        await self.service.execute('open https://a.example/', 's1')
        self.chrome.targets.clear()  # e.g. browser restart or the page closed itself
        result = await self.service.execute('snapshot', 's1')
        self.assertIn('https://a.example/', result['text'])

    async def test_refs_go_on_counting_over_a_sessions_pages_and_the_result_has_the_parts(self):
        first = await self.service.execute('open https://a.example/', 's1')
        second = await self.service.execute('open https://b.example/', 's1')
        other = await self.service.execute('open https://c.example/', 's2')
        self.assertEqual(self.chrome.bases, [0, 5, 0], 'a ref never names two elements in one session')
        self.assertEqual(first['body'], 'page https://a.example/')
        self.assertEqual(first['links'], {'e1': 'https://a.example/'})
        self.assertNotEqual(first['doc'], second['doc'])
        self.assertEqual(second['links'], {'e6': 'https://b.example/'})
        self.assertEqual(other['notes'], [])
        self.assertIn('---\npage https://a.example/', first['snapshot'])

    async def test_back_on_a_popups_first_page_closes_it_and_returns_to_its_opener(self):
        await self.service.execute('open https://a.example/', 's1')
        state = self.service.sessions['s1']
        opener = state.stack[-1]
        popup = await self.chrome.new_tab()  # adopted as a click's popup
        state.stack.append(popup)
        result = await self.service.execute('back', 's1')
        self.assertIn('back on the previous one', result['text'])
        self.assertEqual(state.stack, [opener])
        self.assertNotIn(popup.target_id, self.chrome.targets)

    async def test_unknown_command_is_refused_without_starting_chrome(self):
        service = worker.BrowserService()
        with self.assertRaisesRegex(ValueError, 'unsupported browser command: forward'):
            await service.execute('forward', 's1')
        self.assertIsNone(service.browser)


class RequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_deadline_is_reported_and_inner_timeouts_are_errors(self):
        class Service:
            async def execute(self, command, session_id):
                if command == 'hang':
                    await asyncio.sleep(3600)
                raise TimeoutError('inner wait')
        with mock.patch.object(worker, 'COMMAND_TIMEOUT', 0.05), mock.patch.object(worker, 'log_event'):
            hung = await worker.execute_request(Service(), {'id': 1, 'command': 'hang', 'sessionId': 's'})
            inner = await worker.execute_request(Service(), {'id': 2, 'command': 'other', 'sessionId': 's'})
        self.assertEqual(hung, {'id': 1, 'sessionId': 's', 'ok': False,
                                'error': 'Browser command timed out after 0.05 seconds'})
        self.assertEqual(inner['error'], 'TimeoutError: inner wait')


class HelperTests(unittest.TestCase):
    def test_snapshot_header_flags_error_pages_and_dialogs(self):
        text = worker.format_snapshot({'url': 'https://x/', 'title': 'Missing', 'text': 'Not found',
                                       'scrollHeight': 900, 'scrollY': 0, 'viewportHeight': 600},
                                      status=404, dialogs=['alert: hi'])
        self.assertIn('HTTP status: 404', text)
        self.assertIn('Dialog closed automatically: alert: hi', text)
        self.assertTrue(text.endswith('Not found'))


if __name__ == '__main__':
    unittest.main()
