import asyncio
import functools
import http.server
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
MARKER = '__PI_NODRIVER__'


class QuietSimpleHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, _format, *_args):
        pass


class FakeTarget:
    def __init__(self, target_id):
        self.target_id = target_id


class FakeBrowser:
    def __init__(self):
        self.tabs = []

    async def update_targets(self):
        return None


class FakePage:
    def __init__(self, browser, target_id):
        self.browser = browser
        self.target = FakeTarget(target_id)
        self.url = f'https://{target_id}.test/'
        self.closed = False

    async def close(self):
        self.closed = True
        if self in self.browser.tabs:
            self.browser.tabs.remove(self)


class CloseFailingPage(FakePage):
    async def close(self):
        raise RuntimeError('close failed')


class CloseIgnoringPage(FakePage):
    async def close(self):
        return None


class FakeLongPressPage:
    url = 'https://example.test/ordinary-control'

    def __init__(self, fail_during_hold=False, fail_touch_start=False, cancel_during_hold=False):
        self.fail_during_hold = fail_during_hold
        self.fail_touch_start = fail_touch_start
        self.cancel_during_hold = cancel_during_hold
        self.commands = []
        self.sleep_seconds = []

    async def bring_to_front(self):
        return None

    async def evaluate(self, _script):
        return json.dumps({
            'inspectionComplete': True,
            'matches': [],
            'indicators': '',
            'crossOriginHit': False,
        })

    async def send(self, command):
        request = next(command)
        self.commands.append(request)
        if self.fail_touch_start and len(self.commands) == 1:
            raise RuntimeError('touch start interrupted')
        try:
            command.send({})
        except StopIteration as result:
            return result.value
        raise AssertionError('CDP command did not finish')

    async def sleep(self, seconds):
        self.sleep_seconds.append(seconds)
        if self.fail_during_hold:
            raise RuntimeError('hold interrupted')
        if self.cancel_during_hold:
            raise asyncio.CancelledError()


class FailingChallengeInspectionPage:
    url = 'https://example.test/ordinary-looking-path'

    async def evaluate(self, _script):
        raise RuntimeError('execution context disappeared')


class LongPressInputUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_mobile_touch_start_and_end_for_requested_duration(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        page = FakeLongPressPage()

        await worker.native_long_press(page, 120.5, 300, 750)

        self.assertEqual(page.sleep_seconds, [0.75])
        self.assertEqual(
            [command['method'] for command in page.commands],
            ['Input.dispatchTouchEvent', 'Input.dispatchTouchEvent'],
        )
        self.assertEqual(page.commands[0]['params']['type'], 'touchStart')
        self.assertEqual(page.commands[0]['params']['touchPoints'][0]['x'], 120.5)
        self.assertEqual(page.commands[1]['params'], {'type': 'touchEnd', 'touchPoints': []})

    async def test_challenge_inspection_fails_closed(self):
        from worker import BrowserWorker

        worker = BrowserWorker()

        with self.assertRaisesRegex(ValueError, 'LONG_PRESS_CHALLENGE_GUARD'):
            await worker.assert_long_press_allowed(FailingChallengeInspectionPage())

    async def test_attempts_release_when_touch_start_request_is_interrupted(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        page = FakeLongPressPage(fail_touch_start=True)

        with self.assertRaisesRegex(RuntimeError, 'touch start interrupted'):
            await worker.native_long_press(page, 10, 20, 500)

        self.assertEqual(page.commands[-1]['params'], {'type': 'touchEnd', 'touchPoints': []})

    async def test_releases_touch_when_command_is_cancelled(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        page = FakeLongPressPage(cancel_during_hold=True)

        with self.assertRaises(asyncio.CancelledError):
            await worker.native_long_press(page, 10, 20, 500)

        self.assertEqual(page.commands[-1]['params'], {'type': 'touchEnd', 'touchPoints': []})

    async def test_releases_touch_when_hold_is_interrupted(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        page = FakeLongPressPage(fail_during_hold=True)

        with self.assertRaisesRegex(RuntimeError, 'hold interrupted'):
            await worker.native_long_press(page, 10, 20, 500)

        self.assertEqual(page.commands[-1]['params'], {'type': 'touchEnd', 'touchPoints': []})


class WorkerTabCapacityUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_opening_thirty_tabs_evicts_old_inactive_tabs_but_keeps_recently_touched_tabs(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.browser = FakeBrowser()
        pages = []

        for index in range(30):
            await worker.ensure_tab_capacity(required=1, protected_session_id=f'session-{index}')
            page = FakePage(worker.browser, f'tab-{index}')
            worker.browser.tabs.append(page)
            worker.pages[f'session-{index}'] = page
            worker.register_tab(page, f'session-{index}')
            pages.append(page)
            if index == 19:
                worker.touch_tab(pages[0])
                worker.touch_tab(pages[1])

        remaining = {page.target.target_id for page in worker.browser.tabs}
        self.assertEqual(len(remaining), 20)
        self.assertIn('tab-0', remaining)
        self.assertIn('tab-1', remaining)
        self.assertTrue({f'tab-{index}' for index in range(2, 12)}.isdisjoint(remaining))
        self.assertTrue({f'tab-{index}' for index in range(12, 30)}.issubset(remaining))
        for index in range(2, 12):
            self.assertTrue(pages[index].closed)
            self.assertNotIn(f'session-{index}', worker.pages)

    async def test_failed_eviction_keeps_live_tab_registered_and_mapped(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.max_tabs = 1
        worker.tab_registry.max_tabs = 1
        worker.browser = FakeBrowser()
        page = CloseFailingPage(worker.browser, 'tab-stuck')
        worker.browser.tabs.append(page)
        worker.pages['session-a'] = page
        worker.register_tab(page, 'session-a')

        with self.assertRaisesRegex(RuntimeError, 'close failed'):
            await worker.ensure_tab_capacity(required=1)

        self.assertIn(page, worker.browser.tabs)
        self.assertIs(worker.pages['session-a'], page)
        self.assertEqual(
            {record.target_id for record in worker.tab_registry.records()},
            {'tab-stuck'},
        )

    async def test_eviction_refuses_capacity_when_chrome_ignores_close(self):
        from browser_logic import TabLimitError
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.max_tabs = 1
        worker.tab_registry.max_tabs = 1
        worker.browser = FakeBrowser()
        page = CloseIgnoringPage(worker.browser, 'tab-stuck')
        worker.browser.tabs.append(page)
        worker.pages['session-a'] = page
        worker.register_tab(page, 'session-a')

        with self.assertRaisesRegex(TabLimitError, 'did not close'):
            await worker.ensure_tab_capacity(required=1)

        self.assertIn(page, worker.browser.tabs)
        self.assertIs(worker.pages['session-a'], page)
        self.assertEqual(
            {record.target_id for record in worker.tab_registry.records()},
            {'tab-stuck'},
        )

    async def test_successful_eviction_removes_only_that_tabs_download_routes(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.browser = FakeBrowser()
        first = FakePage(worker.browser, 'tab-first')
        second = FakePage(worker.browser, 'tab-second')
        worker.browser.tabs.extend([first, second])
        first_record = worker.register_tab(first, 'shared-session')
        worker.register_tab(second, 'shared-session')
        worker.download_target_sessions.update({
            'tab-first': 'shared-session',
            'tab-second': 'shared-session',
        })
        worker.download_frame_sessions.update({
            'frame-first': 'shared-session',
            'frame-second': 'shared-session',
        })
        worker.download_frame_targets.update({
            'frame-first': 'tab-first',
            'frame-second': 'tab-second',
        })

        await worker.evict_tab(first_record)

        self.assertNotIn('tab-first', worker.download_target_sessions)
        self.assertNotIn('frame-first', worker.download_frame_sessions)
        self.assertNotIn('frame-first', worker.download_frame_targets)
        self.assertEqual(worker.download_target_sessions['tab-second'], 'shared-session')
        self.assertEqual(worker.download_frame_sessions['frame-second'], 'shared-session')
        self.assertEqual(worker.download_frame_targets['frame-second'], 'tab-second')

    async def test_reconcile_removes_records_for_tabs_closed_outside_the_worker(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.browser = FakeBrowser()
        closed = FakePage(worker.browser, 'tab-closed')
        alive = FakePage(worker.browser, 'tab-alive')
        worker.browser.tabs.extend([closed, alive])
        worker.register_tab(closed, 'session-closed')
        worker.register_tab(alive, 'session-alive')
        worker.browser.tabs.remove(closed)

        await worker.reconcile_tabs()

        self.assertEqual(
            {record.target_id for record in worker.tab_registry.records()},
            {'tab-alive'},
        )

    async def test_reconcile_restores_live_opener_when_current_popup_was_closed_externally(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.browser = FakeBrowser()
        opener = FakePage(worker.browser, 'tab-opener')
        popup = FakePage(worker.browser, 'tab-popup')
        worker.browser.tabs.append(opener)
        worker.pages['session-a'] = popup
        worker.popup_openers['session-a'] = [opener]
        worker.register_tab(opener, 'session-a', 'page')
        worker.register_tab(popup, 'session-a', 'popup')

        await worker.reconcile_tabs()

        self.assertIs(worker.pages['session-a'], opener)
        self.assertNotIn('session-a', worker.popup_openers)
        self.assertNotIn('tab-popup', {record.target_id for record in worker.tab_registry.records()})

    async def test_reconcile_removes_session_mapping_when_current_tab_was_closed_externally(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.browser = FakeBrowser()
        closed = FakePage(worker.browser, 'tab-closed')
        worker.pages['session-a'] = closed
        worker.register_tab(closed, 'session-a')

        await worker.reconcile_tabs()

        self.assertNotIn('session-a', worker.pages)

    async def test_capacity_protects_only_the_active_target_not_every_tab_in_its_session(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.max_tabs = 2
        worker.tab_registry.max_tabs = 2
        worker.browser = FakeBrowser()
        active = FakePage(worker.browser, 'tab-active')
        idle = FakePage(worker.browser, 'tab-idle')
        worker.browser.tabs.extend([active, idle])
        worker.register_tab(active, 'shared-session')
        worker.register_tab(idle, 'shared-session')
        worker.begin_tab_activity(active)

        victims = await worker.ensure_tab_capacity(required=1)

        self.assertEqual([victim.target_id for victim in victims], ['tab-idle'])
        self.assertFalse(active.closed)

    async def test_evicting_current_popup_restores_its_live_opener(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.browser = FakeBrowser()
        opener = FakePage(worker.browser, 'tab-opener')
        popup = FakePage(worker.browser, 'tab-popup')
        worker.browser.tabs.extend([opener, popup])
        worker.pages['session-a'] = popup
        worker.popup_openers['session-a'] = [opener]
        worker.register_tab(opener, 'session-a', 'page')
        popup_record = worker.register_tab(popup, 'session-a', 'popup')

        await worker.evict_tab(popup_record)

        self.assertIs(worker.pages['session-a'], opener)
        self.assertNotIn('session-a', worker.popup_openers)
        self.assertIn('session-a', worker.popup_just_closed)

    async def test_popup_admission_rolls_back_when_existing_tabs_are_all_active(self):
        from browser_logic import TabLimitError
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.max_tabs = 2
        worker.tab_registry.max_tabs = 2
        worker.browser = FakeBrowser()
        opener = FakePage(worker.browser, 'tab-opener')
        other = FakePage(worker.browser, 'tab-other')
        popup = FakePage(worker.browser, 'tab-popup')
        worker.browser.tabs.extend([opener, other, popup])
        worker.pages['session-a'] = opener
        worker.pages['session-b'] = other
        worker.register_tab(opener, 'session-a')
        worker.register_tab(other, 'session-b')
        worker.begin_tab_activity(opener)
        worker.begin_tab_activity(other)

        with self.assertRaisesRegex(TabLimitError, 'TAB_LIMIT'):
            await worker.admit_popup('session-a', opener, popup)

        self.assertTrue(popup.closed)
        self.assertIs(worker.pages['session-a'], opener)
        self.assertNotIn('session-a', worker.popup_openers)
        self.assertNotIn('tab-popup', {record.target_id for record in worker.tab_registry.records()})

    def test_crawl_concurrency_reserves_capacity_for_active_tabs(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.max_tabs = 20
        worker.begin_tab_activity(FakePage(FakeBrowser(), 'tab-active'))

        self.assertEqual(worker.available_crawl_slots(), 19)

    def test_unique_pages_deduplicates_unhashable_tabs_by_identity(self):
        from worker import BrowserWorker

        class UnhashablePage(FakePage):
            __hash__ = None

        browser = FakeBrowser()
        first = UnhashablePage(browser, 'tab-first')
        second = UnhashablePage(browser, 'tab-second')

        self.assertEqual(
            BrowserWorker.unique_pages([first, first, second]),
            [first, second],
        )

    async def test_failed_open_keeps_the_previous_session_page(self):
        from worker import BrowserWorker

        class FailingPage(FakePage):
            async def send(self, _command):
                return None

            async def get(self, _url):
                raise RuntimeError('navigation failed')

        worker = BrowserWorker()
        worker.browser = FakeBrowser()
        previous = FakePage(worker.browser, 'tab-previous')
        replacement = FailingPage(worker.browser, 'tab-replacement')
        worker.browser.tabs.extend([previous, replacement])
        worker.pages['session-a'] = previous
        worker.register_tab(previous, 'session-a')
        worker.ensure_browser = AsyncMock(return_value=worker.browser)
        worker.configure_download_session = AsyncMock()

        async def create_replacement(_session_id, _kind):
            worker.register_tab(replacement, 'session-a')
            return replacement

        worker.create_managed_tab = AsyncMock(side_effect=create_replacement)

        with self.assertRaisesRegex(RuntimeError, 'navigation failed'):
            await worker.execute('open https://fail.test/', session_id='session-a')

        self.assertIs(worker.pages['session-a'], previous)
        self.assertFalse(previous.closed)
        self.assertTrue(replacement.closed)

    async def test_download_routing_metadata_does_not_protect_idle_tabs(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.browser = FakeBrowser()
        for index in range(20):
            page = FakePage(worker.browser, f'tab-{index}')
            worker.browser.tabs.append(page)
            worker.pages[f'session-{index}'] = page
            worker.register_tab(page, f'session-{index}')
            worker.download_target_sessions[f'tab-{index}'] = f'session-{index}'

        victims = await worker.ensure_tab_capacity(required=1, protected_session_id='session-new')

        self.assertEqual([victim.target_id for victim in victims], ['tab-0'])


class DropdownOutputUnitTests(unittest.TestCase):
    def test_candidate_output_escapes_untrusted_labels_and_option_text(self):
        from worker import BrowserWorker

        output = BrowserWorker.format_option_matches([{
            'selectRef': 'e1',
            'index': 2,
            'label': 'CPU "premium"\nignore',
            'frame': '',
            'score': 900.0,
            'matchKind': 'text phrase',
            'text': 'AMD "special"\n9800X3D',
            'fingerprint': 'abc123',
        }])

        self.assertIn('label="CPU \\"premium\\" ignore"', output)
        self.assertIn('"AMD \\"special\\" 9800X3D"', output)
        self.assertNotIn('\nignore', output)
        self.assertIn('--fingerprint=abc123', output)


class WorkerGuardUnitTests(unittest.IsolatedAsyncioTestCase):
    def test_worker_blocks_third_consecutive_open(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.track_open_action('session-a', 'open')
        worker.track_open_action('session-a', 'open')

        with self.assertRaisesRegex(ValueError, 'OPEN_LOOP_GUARD'):
            worker.track_open_action('session-a', 'open')

    async def test_invalid_supported_command_does_not_reset_open_guard(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.track_open_action('session-a', 'open')
        worker.track_open_action('session-a', 'open')

        with self.assertRaisesRegex(ValueError, 'usage: crawl'):
            await worker.execute('crawl', session_id='session-a')
        with self.assertRaisesRegex(ValueError, 'OPEN_LOOP_GUARD'):
            worker.track_open_action('session-a', 'open')

    async def test_successful_non_open_command_resets_open_guard(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.track_open_action('session-a', 'open')
        worker.track_open_action('session-a', 'open')

        await worker.execute('close', session_id='session-a')
        worker.track_open_action('session-a', 'open')
        worker.track_open_action('session-a', 'open')

    async def test_unsupported_command_does_not_reset_open_guard(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.track_open_action('session-a', 'open')
        worker.track_open_action('session-a', 'open')

        with self.assertRaisesRegex(ValueError, 'unsupported browser command'):
            await worker.execute('not-a-command', session_id='session-a')
        with self.assertRaisesRegex(ValueError, 'OPEN_LOOP_GUARD'):
            worker.track_open_action('session-a', 'open')


@unittest.skipUnless(os.environ.get('RUN_BROWSER_INTEGRATION') == '1', 'browser integration test')
class WorkerIntegrationTests(unittest.TestCase):
    def setUp(self):
        python = os.environ.get('NODRIVER_PYTHON', str(ROOT / '.venv/bin/python'))
        self.temp_dir = tempfile.TemporaryDirectory()
        self.download_dir = Path(self.temp_dir.name) / 'downloads'
        env = {
            **os.environ,
            'PI_NODRIVER_PROFILE': str(Path(self.temp_dir.name) / 'profile'),
            'PI_NODRIVER_DOWNLOAD_DIR': str(self.download_dir),
        }
        self.proc = subprocess.Popen(
            ['xvfb-run', '-a', '-s', '-screen 0 1280x900x24', python, str(ROOT / 'worker.py')],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
            env=env,
        )

    def tearDown(self):
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        if self.proc.poll() is None:
            self.proc.wait(timeout=10)
        for _ in range(20):
            try:
                os.killpg(self.proc.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.killpg(self.proc.pid, signal.SIGKILL)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if stream:
                stream.close()
        self.temp_dir.cleanup()

    def command(self, command):
        request_id = getattr(self, '_request_id', 0) + 1
        self._request_id = request_id
        self.proc.stdin.write(json.dumps({'id': request_id, 'command': command}) + '\n')
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                stderr = self.proc.stderr.read()
                self.fail(f'worker exited before response: {stderr}')
            if line.startswith(MARKER):
                response = json.loads(line[len(MARKER):])
                self.assertEqual(response['id'], request_id)
                if not response.get('ok'):
                    self.fail(response.get('error', 'worker command failed'))
                return response

    def command_raw(self, command):
        request_id = getattr(self, '_request_id', 0) + 1
        self._request_id = request_id
        self.proc.stdin.write(json.dumps({'id': request_id, 'command': command}) + '\n')
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                stderr = self.proc.stderr.read()
                self.fail(f'worker exited before response: {stderr}')
            if line.startswith(MARKER):
                response = json.loads(line[len(MARKER):])
                self.assertEqual(response['id'], request_id)
                return response

    def test_dismisses_cookie_and_marketing_overlays_safely(self):
        fixture_url = (ROOT / 'tests/fixture_overlays.html').as_uri()
        self.command(f'open {fixture_url}')

        result = self.command('dismiss overlays --cookies=accept')['text']
        self.assertIn('cookie', result)
        self.assertIn('同意', result)
        self.assertIn('不用，謝謝', result)

        page_text = self.command('get text')['text']
        self.assertNotIn('網站使用了 Cookie', page_text)
        self.assertNotIn('9 折優惠', page_text)
        self.assertIn('Product survey', page_text)
        self.assertIn('Next step', page_text)
        self.assertNotIn('Next step', result)
        self.assertIn('Buy product', page_text)

    def open_fixture(self):
        fixture_url = (ROOT / 'tests/fixture.html').as_uri()
        self.command(f'open {fixture_url}')

    def open_select_fixture(self):
        fixture_url = (ROOT / 'tests/fixture_select.html').as_uri()
        self.command(f'open {fixture_url}')

    def status(self):
        return self.command('get text')['text']

    def test_opens_snapshots_clicks_and_reads_page(self):
        fixture_url = (ROOT / 'tests/fixture.html').as_uri()
        self.command(f'open {fixture_url}')
        snapshot = self.command('snapshot -i')['text']
        self.assertIn('@e1', snapshot)
        self.assertIn('Search term', snapshot)
        self.assertIn('Go now', snapshot)

        button_line = next(line for line in snapshot.splitlines() if 'Go now' in line)
        button_ref = button_line.split()[0]
        self.command(f'click {button_ref}')
        page_text = self.command('get text')['text']
        self.assertIn('clicked', page_text)

        self.command(f'open {fixture_url}')
        snapshot = self.command('snapshot -i')['text']
        button_ref = next(line for line in snapshot.splitlines() if 'Go now' in line).split()[0]
        self.command(f'click-js {button_ref}')
        time.sleep(0.2)
        page_text = self.command('get text')['text']
        self.assertIn('clicked', page_text)

    def test_long_press_holds_a_semantic_ref_with_touch_input(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        hold_ref = next(line for line in snapshot.splitlines() if 'Hold for action' in line).split()[0]

        result = self.command(f'long-press {hold_ref} --ms=600')

        self.assertIn('Long-pressed', result['text'])
        self.assertIn('long-pressed', self.command('get text')['text'])

    def test_long_press_rejects_ref_reassigned_after_snapshot(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        hold_ref = next(line for line in snapshot.splitlines() if 'Hold for action' in line).split()[0]
        replace_ref = next(
            line for line in snapshot.splitlines() if 'Replace hold target' in line
        ).split()[0]

        self.command(f'click {replace_ref}')
        stale = self.command_raw(f'long-press {hold_ref} --ms=600')

        self.assertTrue(stale['ok'])
        self.assertEqual(stale['action'], 'stale-ref-recovery')
        self.assertIn('CLICK NOT PERFORMED', stale['text'])

    def test_long_press_maps_nested_scaled_iframe_ref_to_mobile_viewport(self):
        handler = functools.partial(QuietSimpleHTTPRequestHandler, directory=str(ROOT / 'tests'))
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            self.command(
                f'open http://127.0.0.1:{server.server_port}/fixture_iframe_long_press.html'
            )
            snapshot = self.command('snapshot -i')['text']
            hold_ref = next(
                line for line in snapshot.splitlines() if 'Nested hold target' in line
            ).split()[0]

            self.command(f'long-press {hold_ref} --ms=600')

            updated = self.command('snapshot -i')['text']
            self.assertIn('Nested long-pressed', updated)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    def test_long_press_refuses_reflected_iframe_coordinates(self):
        handler = functools.partial(QuietSimpleHTTPRequestHandler, directory=str(ROOT / 'tests'))
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            self.command(
                f'open http://127.0.0.1:{server.server_port}/fixture_iframe_reflected.html'
            )
            snapshot = self.command('snapshot -i')['text']
            hold_ref = next(
                line for line in snapshot.splitlines() if 'Nested hold target' in line
            ).split()[0]

            blocked = self.command_raw(f'long-press {hold_ref} --ms=600')

            self.assertFalse(blocked['ok'])
            self.assertIn('unsafe iframe coordinate transform', blocked['error'])
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    def test_long_press_refuses_iframe_with_transformed_ancestor(self):
        handler = functools.partial(QuietSimpleHTTPRequestHandler, directory=str(ROOT / 'tests'))
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            self.command(
                f'open http://127.0.0.1:{server.server_port}/fixture_iframe_ancestor_transformed.html'
            )
            snapshot = self.command('snapshot -i')['text']
            hold_ref = next(
                line for line in snapshot.splitlines() if 'Nested hold target' in line
            ).split()[0]

            blocked = self.command_raw(f'long-press {hold_ref} --ms=600')

            self.assertFalse(blocked['ok'])
            self.assertIn('unsafe iframe coordinate transform', blocked['error'])
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    def test_long_press_refuses_challenge_pages(self):
        challenge_url = (ROOT / 'tests/fixture_challenge.html').as_uri()
        self.command(f'open {challenge_url}')
        snapshot = self.command('snapshot -i')['text']
        hold_ref = next(line for line in snapshot.splitlines() if 'Press and hold' in line).split()[0]

        blocked = self.command_raw(f'long-press {hold_ref} --ms=600')

        self.assertFalse(blocked['ok'])
        self.assertIn('LONG_PRESS_CHALLENGE_GUARD', blocked['error'])
        self.assertIn('challenge-idle', self.command('get text')['text'])

    def test_long_press_detects_challenge_text_beyond_large_page_content(self):
        fixture_url = (ROOT / 'tests/fixture_deep_challenge.html').as_uri()
        self.command(f'open {fixture_url}')
        snapshot = self.command('snapshot -i')['text']
        hold_ref = next(line for line in snapshot.splitlines() if 'Continue deep form' in line).split()[0]

        blocked = self.command_raw(f'long-press {hold_ref} --ms=600')

        self.assertFalse(blocked['ok'])
        self.assertIn('LONG_PRESS_CHALLENGE_GUARD', blocked['error'])

    def test_long_press_refuses_challenge_text_inside_same_origin_iframe(self):
        handler = functools.partial(QuietSimpleHTTPRequestHandler, directory=str(ROOT / 'tests'))
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            self.command(
                f'open http://127.0.0.1:{server.server_port}/fixture_embedded_form.html'
            )
            snapshot = self.command('snapshot -i')['text']
            hold_ref = next(line for line in snapshot.splitlines() if 'Continue' in line).split()[0]

            blocked = self.command_raw(f'long-press {hold_ref} --ms=600')

            self.assertFalse(blocked['ok'])
            self.assertIn('LONG_PRESS_CHALLENGE_GUARD', blocked['error'])
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    def test_long_press_refuses_coordinates_over_cross_origin_iframe(self):
        handler = functools.partial(QuietSimpleHTTPRequestHandler, directory=str(ROOT / 'tests'))
        outer_server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        frame_server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        outer_thread = threading.Thread(target=outer_server.serve_forever, daemon=True)
        frame_thread = threading.Thread(target=frame_server.serve_forever, daemon=True)
        outer_thread.start()
        frame_thread.start()
        try:
            frame_url = f'http://127.0.0.1:{frame_server.server_port}/fixture.html'
            self.command(
                f'open http://127.0.0.1:{outer_server.server_port}/fixture_cross_origin_frame.html?src={frame_url}'
            )

            blocked = self.command_raw('long-press 100 100 --ms=600')

            self.assertFalse(blocked['ok'])
            self.assertIn('LONG_PRESS_CHALLENGE_GUARD', blocked['error'])
            self.assertIn('cross-origin iframe', blocked['error'])
        finally:
            outer_server.shutdown()
            frame_server.shutdown()
            outer_server.server_close()
            frame_server.server_close()
            outer_thread.join(timeout=2)
            frame_thread.join(timeout=2)

    def test_long_press_refuses_coordinates_through_transformed_nested_iframe(self):
        handler = functools.partial(QuietSimpleHTTPRequestHandler, directory=str(ROOT / 'tests'))
        outer_server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        frame_server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        outer_thread = threading.Thread(target=outer_server.serve_forever, daemon=True)
        frame_thread = threading.Thread(target=frame_server.serve_forever, daemon=True)
        outer_thread.start()
        frame_thread.start()
        try:
            frame_url = f'http://127.0.0.1:{frame_server.server_port}/fixture.html'
            self.command(
                f'open http://127.0.0.1:{outer_server.server_port}/fixture_transformed_nested_frame.html?src={frame_url}'
            )

            blocked = self.command_raw('long-press 100 100 --ms=600')

            self.assertFalse(blocked['ok'])
            self.assertIn('LONG_PRESS_CHALLENGE_GUARD', blocked['error'])
            self.assertIn('iframe', blocked['error'])
        finally:
            outer_server.shutdown()
            frame_server.shutdown()
            outer_server.server_close()
            frame_server.server_close()
            outer_thread.join(timeout=2)
            frame_thread.join(timeout=2)

    def test_long_press_refuses_cross_origin_iframe_inside_open_shadow_root(self):
        handler = functools.partial(QuietSimpleHTTPRequestHandler, directory=str(ROOT / 'tests'))
        outer_server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        frame_server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        outer_thread = threading.Thread(target=outer_server.serve_forever, daemon=True)
        frame_thread = threading.Thread(target=frame_server.serve_forever, daemon=True)
        outer_thread.start()
        frame_thread.start()
        try:
            frame_url = f'http://127.0.0.1:{frame_server.server_port}/fixture.html'
            self.command(
                f'open http://127.0.0.1:{outer_server.server_port}/fixture_shadow_cross_origin_frame.html?src={frame_url}'
            )

            blocked = self.command_raw('long-press 100 100 --ms=600')

            self.assertFalse(blocked['ok'])
            self.assertIn('LONG_PRESS_CHALLENGE_GUARD', blocked['error'])
            self.assertIn('cross-origin iframe', blocked['error'])
        finally:
            outer_server.shutdown()
            frame_server.shutdown()
            outer_server.server_close()
            frame_server.server_close()
            outer_thread.join(timeout=2)
            frame_thread.join(timeout=2)

    def test_snapshot_lists_only_interactive_objects_in_the_current_viewport(self):
        fixture_url = (ROOT / 'tests/fixture_viewport.html').as_uri()
        self.command(f'open {fixture_url}')

        top_snapshot = self.command('snapshot -i')['text']
        self.assertIn('Top viewport action', top_snapshot)
        self.assertNotIn('Middle viewport action', top_snapshot)
        self.assertNotIn('Bottom viewport action', top_snapshot)
        top_ref = next(line for line in top_snapshot.splitlines() if 'Top viewport action' in line).split()[0]
        self.command(f'click {top_ref}')
        self.assertIn('top-clicked', self.command('get text')['text'])

        self.command('scroll down 650')
        middle_snapshot = self.command('snapshot -i')['text']
        self.assertNotIn('Top viewport action', middle_snapshot)
        self.assertNotIn('Middle viewport action', middle_snapshot)
        self.assertIn('Bottom viewport action', middle_snapshot)
        bottom_ref = next(line for line in middle_snapshot.splitlines() if 'Bottom viewport action' in line).split()[0]
        self.command(f'click {bottom_ref}')
        self.assertIn('bottom-clicked', self.command('get text')['text'])

    def test_main_frame_fill_preserves_per_character_input_semantics(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        query_ref = next(line for line in snapshot.splitlines() if 'Search term' in line).split()[0]

        self.command(f'fill {query_ref} 9800X3D')

        text = self.command('get text')['text']
        self.assertGreaterEqual(text.count('beforeinput'), len('9800X3D'))
        self.assertGreaterEqual(text.count('input'), len('9800X3D'))

    def test_find_option_searches_dropdowns_by_fuzzy_tokens_without_opening_them(self):
        self.open_select_fixture()
        snapshot = self.command('snapshot -i')['text']
        self.assertNotIn('<select> label="Disabled CPU"', snapshot)
        self.assertNotIn('Secret disabled processor', snapshot)
        self.assertNotIn('Secret fieldset processor', snapshot)

        result = self.command('find-option "ryzen 9800x3d"')['text']

        self.assertIn('Main CPU', result)
        self.assertIn('AMD Ryzen 7 9800X3D', result)
        self.assertIn('select @', result)
        self.assertIn('--fingerprint=', result)
        self.assertNotIn('Intel first option', result)
        self.assertNotIn('Secret disabled processor', result)
        self.assertNotIn('Secret fieldset processor', result)

        exact_command = next(
            line.split('Select exactly: ', 1)[1]
            for line in result.splitlines()
            if 'Select exactly: ' in line
        )
        selected = self.command(exact_command)['text']
        self.assertIn('AMD Ryzen 7 9800X3D', selected)

        relaxed = self.command('find-option "ryzen 9999"')['text']
        self.assertIn('No full-token option matched', relaxed)
        self.assertIn('relaxed family suggestion', relaxed)
        self.assertIn('AMD Ryzen 7 9800X3D', relaxed)

    def test_exact_option_candidate_rejects_reordered_dropdown(self):
        self.open_select_fixture()
        snapshot = self.command('snapshot -i')['text']
        reorder_ref = next(line for line in snapshot.splitlines() if 'Reorder CPU options' in line).split()[0]
        found = self.command('find-option "ryzen 9800x3d"')['text']
        exact_command = next(
            line.split('Select exactly: ', 1)[1]
            for line in found.splitlines()
            if 'Select exactly: ' in line
        )

        self.command(f'click {reorder_ref}')
        stale = self.command_raw(exact_command)

        self.assertFalse(stale['ok'])
        self.assertIn('STALE_OPTION', stale['error'])
        self.assertIn('find-option', stale['error'])

    def test_main_frame_select_prefers_visible_text_over_duplicate_value(self):
        self.open_select_fixture()
        snapshot = self.command('snapshot -i')['text']
        cpu_ref = next(line for line in snapshot.splitlines() if '<select>' in line and 'label="Main CPU"' in line).split()[0]

        selected = self.command(f'select {cpu_ref} 9800X3D')['text']

        self.assertIn('AMD Ryzen 7 9800X3D', selected)
        updated = self.command('snapshot -i')['text']
        self.assertIn('selected="AMD Ryzen 7 9800X3D"', updated)

    def test_same_origin_iframe_supports_semantic_fill_select_and_click(self):
        class DelayedIframeHandler(QuietSimpleHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith('/fixture_iframe_result.html'):
                    time.sleep(0.4)
                super().do_GET()

        handler = functools.partial(
            DelayedIframeHandler,
            directory=str(ROOT / 'tests'),
        )
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            self.command(f'open http://127.0.0.1:{server.server_port}/fixture_iframe.html')
            snapshot = self.command('snapshot -i')['text']

            search_ref = next(line for line in snapshot.splitlines() if 'Search components' in line).split()[0]
            cpu_ref = next(line for line in snapshot.splitlines() if '<select>' in line and 'label="CPU"' in line and 'frame="PC configurator"' in line).split()[0]
            apply_ref = next(line for line in snapshot.splitlines() if 'Apply iframe selection' in line).split()[0]
            self.assertIn('frame="PC configurator"', snapshot)
            sensitive_line = next(line for line in snapshot.splitlines() if 'Sensitive frame input' in line)
            self.assertIn('frame="http://127.0.0.1:', sensitive_line)
            self.assertNotIn('do-not-leak-this', sensitive_line)
            self.assertNotIn('Concealed 9800X3D control', snapshot)
            self.assertNotIn('Offscreen 9950X3D control', snapshot)
            hidden_search = self.command_raw('find-option "Concealed 9800X3D"')
            if hidden_search['ok']:
                self.assertNotIn('Concealed 9800X3D control', hidden_search['text'])
            else:
                self.assertIn('no dropdown option matched', hidden_search['error'])

            self.command(f'fill {search_ref} 9800X3D')
            selected = self.command(f'select {cpu_ref} 9800X3D')['text']
            self.assertIn('9800X3D', selected)
            self.command(f'click-js {apply_ref}')

            updated = self.command('snapshot -i')['text']
            search_line = next(line for line in updated.splitlines() if 'Search components' in line)
            self.assertIn('"9800X3D"', search_line)
            self.assertIn('keydown', search_line)
            self.assertIn('beforeinput', search_line)
            self.assertIn('selected="AMD Ryzen 7 9800X3D"', updated)
            self.assertIn('Iframe selection applied', updated)

            submit_ref = next(line for line in updated.splitlines() if 'Iframe submit query' in line).split()[0]
            submitted = self.command(f'fill-submit {submit_ref} ready')['text']
            self.assertIn('Iframe submitted result', submitted)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    def test_iframe_child_offscreen_after_resize_blocks_stale_ref_click(self):
        handler = functools.partial(
            QuietSimpleHTTPRequestHandler,
            directory=str(ROOT / 'tests'),
        )
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            self.command(
                f'open http://127.0.0.1:{server.server_port}/fixture_iframe_resizable.html'
            )
            snapshot = self.command('snapshot -i')['text']
            shrink_ref = next(line for line in snapshot.splitlines() if 'Shrink configurator' in line).split()[0]
            child_ref = next(line for line in snapshot.splitlines() if 'Offscreen 9950X3D control' in line).split()[0]

            self.command(f'click {shrink_ref}')
            blocked = self.command_raw(f'click {child_ref}')

            self.assertTrue(blocked['ok'])
            self.assertEqual(blocked['action'], 'stale-ref-recovery')
            self.assertIn('CLICK NOT PERFORMED', blocked['text'])
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    def test_full_snapshot_is_visual_only_and_prompts_scroll_exploration(self):
        fixture_url = (ROOT / 'tests/fixture_viewport.html').as_uri()
        self.command(f'open {fixture_url}')

        result = self.command('snapshot -i --full')

        self.assertEqual(result['action'], 'snapshot-full-vision')
        self.assertEqual(result['count'], 0)
        self.assertNotIn('@e', result['text'])
        self.assertIn('Visual overview only', result['text'])
        self.assertIn('scroll down', result['text'])
        self.assertIn('Do not click coordinates', result['text'])
        self.assertIn('snapshot -i', result['text'])
        self.assertTrue(Path(result['screenshotPath']).is_file())
        self.assertGreater(Path(result['screenshotPath']).stat().st_size, 0)

    def test_download_info_describes_a_snapshot_target_without_clicking(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        download_ref = next(line for line in snapshot.splitlines() if 'Download sample report' in line).split()[0]

        result = self.command(f'download-info {download_ref}')

        self.assertIn('sample-report.txt', result['text'])
        self.assertIn('text/plain', result['text'])
        self.assertIn('download_sample.txt', result['url'])

    def test_downloads_lists_recent_files_without_opening_a_page(self):
        self.download_dir.mkdir(parents=True)
        (self.download_dir / 'older.csv').write_text('old')
        time.sleep(0.01)
        (self.download_dir / 'latest.pdf').write_bytes(b'%PDF-fixture')

        result = self.command('downloads 5')

        self.assertIn('latest.pdf', result['text'])
        self.assertIn('older.csv', result['text'])
        self.assertLess(result['text'].index('latest.pdf'), result['text'].index('older.csv'))

    def test_download_latest_returns_the_newest_completed_file(self):
        self.download_dir.mkdir(parents=True)
        (self.download_dir / 'older.csv').write_text('old')
        time.sleep(0.01)
        expected = self.download_dir / 'latest.pdf'
        expected.write_bytes(b'%PDF-fixture')

        result = self.command('download-latest')

        self.assertEqual(Path(result['downloadPath']), expected)
        self.assertIn('latest.pdf', result['text'])
        self.assertEqual(result['mimeType'], 'application/pdf')

    def test_wait_download_reports_a_file_started_by_a_normal_click(self):
        handler = functools.partial(
            QuietSimpleHTTPRequestHandler,
            directory=str(ROOT / 'tests'),
        )
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            self.command(f'open http://127.0.0.1:{server.server_port}/fixture.html')
            snapshot = self.command('snapshot -i')['text']
            download_ref = next(line for line in snapshot.splitlines() if 'Download sample report' in line).split()[0]
            self.command(f'click {download_ref}')

            result = self.command('wait-download 5000')

            self.assertEqual(Path(result['downloadPath']).name, 'sample-report.txt')
            self.assertIn('completed', result['text'].lower())
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    def test_click_js_configures_download_tracking_before_dispatch(self):
        handler = functools.partial(
            QuietSimpleHTTPRequestHandler,
            directory=str(ROOT / 'tests'),
        )
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            self.command(f'open http://127.0.0.1:{server.server_port}/fixture.html')
            snapshot = self.command('snapshot -i')['text']
            download_ref = next(line for line in snapshot.splitlines() if 'Download sample report' in line).split()[0]

            self.command(f'click-js {download_ref}')
            result = self.command('wait-download 5000')

            self.assertEqual(Path(result['downloadPath']).name, 'sample-report.txt')
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    def test_download_click_waits_for_a_completed_file(self):
        handler = functools.partial(
            QuietSimpleHTTPRequestHandler,
            directory=str(ROOT / 'tests'),
        )
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            self.command(f'open http://127.0.0.1:{server.server_port}/fixture.html')
            snapshot = self.command('snapshot -i')['text']
            download_ref = next(line for line in snapshot.splitlines() if 'Download sample report' in line).split()[0]

            result = self.command(f'download {download_ref} 5000')

            output = Path(result['downloadPath'])
            self.assertEqual(output.parent, self.download_dir)
            self.assertEqual(output.name, 'sample-report.txt')
            self.assertEqual(output.read_text(), 'Pi Nodriver download fixture\n')
            self.assertIn('completed', result['text'].lower())
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    def test_click_returns_quickly_after_synchronous_update(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        button_ref = next(line for line in snapshot.splitlines() if 'Go now' in line).split()[0]

        started = time.monotonic()
        self.command(f'click {button_ref}')

        self.assertLess(time.monotonic() - started, 0.75)
        self.assertIn('clicked', self.status())

    def test_click_caps_wait_for_continuously_mutating_pages(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        button_ref = next(line for line in snapshot.splitlines() if 'Start noisy updates' in line).split()[0]

        started = time.monotonic()
        self.command(f'click {button_ref}')

        self.assertLess(time.monotonic() - started, 0.9)

    def test_click_switches_to_a_new_tab_without_fixed_two_second_wait(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        link_ref = next(line for line in snapshot.splitlines() if 'Open report' in line).split()[0]

        started = time.monotonic()
        result = self.command(f'click {link_ref}')

        self.assertLess(time.monotonic() - started, 1.25)
        self.assertIn('fixture_new_tab.html', result['url'])
        self.assertIn('New tab report ready', self.status())

    def test_popup_close_automatically_returns_to_its_opener(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        popup_ref = next(line for line in snapshot.splitlines() if 'Open OAuth login' in line).split()[0]
        self.command(f'click {popup_ref}')
        popup_snapshot = self.command('snapshot -i')['text']
        complete_ref = next(line for line in popup_snapshot.splitlines() if 'Complete login' in line).split()[0]

        result = self.command(f'click {complete_ref}')

        self.assertIn('fixture.html', result['url'])
        self.assertIn('oauth-complete', self.status())

    def test_wait_popup_is_idempotent_after_click_already_switched(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        popup_ref = next(line for line in snapshot.splitlines() if 'Open OAuth login' in line).split()[0]
        self.command(f'click {popup_ref}')

        result = self.command('wait-popup 100')

        self.assertIn('fixture_new_tab.html', result['url'])

    def test_wait_popup_detects_a_delayed_child_of_an_active_popup(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        popup_ref = next(line for line in snapshot.splitlines() if 'Open OAuth login' in line).split()[0]
        self.command(f'click {popup_ref}')
        popup_snapshot = self.command('snapshot -i')['text']
        nested_ref = next(line for line in popup_snapshot.splitlines() if 'Open nested OAuth' in line).split()[0]
        self.command(f'click-js {nested_ref}')

        result = self.command('wait-popup 2000')

        self.assertIn('nested=1', result['url'])

    def test_wait_popup_switches_to_a_delayed_oauth_window(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        popup_ref = next(line for line in snapshot.splitlines() if 'Open delayed report' in line).split()[0]
        self.command(f'click-js {popup_ref}')

        result = self.command('wait-popup 2000')

        self.assertIn('fixture_new_tab.html', result['url'])
        self.assertIn('New tab report ready', self.status())

    def test_switch_opener_returns_to_source_without_closing_popup(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        popup_ref = next(line for line in snapshot.splitlines() if 'Open OAuth login' in line).split()[0]
        self.command(f'click {popup_ref}')

        result = self.command('switch opener')

        self.assertIn('fixture.html', result['url'])
        self.assertIn('Go now', self.status())

    def test_next_command_recovers_after_popup_closes_between_commands(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        popup_ref = next(line for line in snapshot.splitlines() if 'Open OAuth login' in line).split()[0]
        self.command(f'click {popup_ref}')
        popup_snapshot = self.command('snapshot -i')['text']
        complete_ref = next(line for line in popup_snapshot.splitlines() if 'Complete delayed login' in line).split()[0]
        self.command(f'click {complete_ref}')
        time.sleep(1)

        page_text = self.status()

        self.assertIn('oauth-complete', page_text)

    def test_wait_popup_close_is_idempotent_after_automatic_recovery(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        popup_ref = next(line for line in snapshot.splitlines() if 'Open OAuth login' in line).split()[0]
        self.command(f'click {popup_ref}')
        popup_snapshot = self.command('snapshot -i')['text']
        complete_ref = next(line for line in popup_snapshot.splitlines() if 'Complete delayed login' in line).split()[0]
        self.command(f'click {complete_ref}')
        time.sleep(1)

        result = self.command('wait-popup-close 100')

        self.assertIn('fixture.html', result['url'])

    def test_wait_popup_close_returns_to_opener_after_oauth_finishes(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        popup_ref = next(line for line in snapshot.splitlines() if 'Open OAuth login' in line).split()[0]
        self.command(f'click {popup_ref}')
        popup_snapshot = self.command('snapshot -i')['text']
        complete_ref = next(line for line in popup_snapshot.splitlines() if 'Complete delayed login' in line).split()[0]
        self.command(f'click {complete_ref}')

        result = self.command('wait-popup-close 2000')

        self.assertIn('fixture.html', result['url'])
        self.assertIn('oauth-complete', self.status())

    def test_click_waits_for_a_scripted_delayed_new_tab(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        button_ref = next(line for line in snapshot.splitlines() if 'Open delayed report' in line).split()[0]

        result = self.command(f'click {button_ref}')

        self.assertIn('fixture_new_tab.html', result['url'])
        self.assertIn('New tab report ready', self.status())

    def test_click_detects_a_delayed_new_tab_handler_on_an_ancestor(self):
        self.open_fixture()
        result = self.command('click-css "#nested-delayed-new-tab span"')

        self.assertIn('fixture_new_tab.html', result['url'])
        self.assertIn('New tab report ready', self.status())

    def test_click_waits_for_a_delayed_named_form_target(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        button_ref = next(line for line in snapshot.splitlines() if 'Open named report' in line).split()[0]

        result = self.command(f'click {button_ref}')

        self.assertIn('fixture_new_tab.html', result['url'])
        self.assertIn('New tab report ready', self.status())

    def test_click_switches_to_an_existing_named_form_target(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        seed_ref = next(line for line in snapshot.splitlines() if 'Seed named report' in line).split()[0]
        self.command(f'click-js {seed_ref}')
        time.sleep(0.3)
        snapshot = self.command('snapshot -i')['text']
        form_ref = next(line for line in snapshot.splitlines() if 'Open named report' in line).split()[0]

        result = self.command(f'click {form_ref}')

        self.assertIn('fixture_new_tab.html', result['url'])
        self.assertIn('New tab report ready', self.status())

    def test_snapshot_and_ref_click_support_custom_div_controls(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        custom_lines = [line for line in snapshot.splitlines() if 'Custom checkout' in line]
        self.assertEqual(len(custom_lines), 1)
        self.command(f'click {custom_lines[0].split()[0]}')
        self.assertIn('custom-clicked', self.status())

    def test_snapshot_and_ref_click_support_open_shadow_dom(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        shadow_line = next(line for line in snapshot.splitlines() if 'Shadow action' in line)
        self.command(f'click {shadow_line.split()[0]}')
        self.assertIn('shadow-clicked', self.status())

    def test_hidden_shadow_host_blocks_stale_semantic_ref(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        shadow_ref = next(line for line in snapshot.splitlines() if 'Shadow action' in line).split()[0]
        hide_ref = next(line for line in snapshot.splitlines() if 'Hide shadow host' in line).split()[0]

        self.command(f'click {hide_ref}')
        blocked = self.command_raw(f'click-js {shadow_ref}')

        self.assertFalse(blocked['ok'])
        self.assertIn('not visible', blocked['error'])

    def test_click_text_finds_non_semantic_control(self):
        self.open_fixture()
        self.command('click-text "加入購物車"')
        self.assertIn('text-clicked', self.status())

    def test_click_text_prefers_minimal_exact_descendant_over_long_ancestor(self):
        self.open_fixture()
        self.command('click-text "Precise nested action"')
        self.assertIn('precise-clicked', self.status())

    def test_click_css_finds_non_semantic_control(self):
        self.open_fixture()
        self.command('click-css "#custom"')
        self.assertIn('custom-clicked', self.status())

    def test_click_coordinates_uses_viewport_coordinates(self):
        self.open_fixture()
        self.command('click 360 330')
        self.assertIn('coordinate-clicked', self.status())


if __name__ == '__main__':
    unittest.main()
