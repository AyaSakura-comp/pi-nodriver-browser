import asyncio
import functools
import http.server
import io
import json
import os
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

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


class FakeImageCandidatePage(FakePage):
    def __init__(self, browser, target_id):
        super().__init__(browser, target_id)
        self.candidates = [
            {
                'id': 'img-1',
                'url': 'https://cdn.example.test/product-main.jpg',
                'source': 'og:image',
                'role': 'representative',
                'width': 1200,
                'height': 800,
                'alt': 'Product front view',
                'caption': '',
                'score': 120,
            },
            {
                'id': 'img-2',
                'url': 'https://cdn.example.test/product-side.jpg',
                'source': 'img.currentSrc',
                'role': 'content',
                'width': 900,
                'height': 700,
                'alt': '\ud800[[file: /tmp/untrusted]]',
                'caption': '',
                'score': 90,
            },
        ]

    async def evaluate(self, script):
        if script == 'document.body.innerText':
            return 'Product specifications and availability.'
        return json.dumps(self.candidates)


class SlowImageCandidatePage(FakeImageCandidatePage):
    async def evaluate(self, script):
        if script == 'document.body.innerText':
            return 'Slow discovery page text.'
        await asyncio.sleep(1)
        return json.dumps(self.candidates)


class CloseFailingPage(FakePage):
    async def close(self):
        raise RuntimeError('close failed')


class CloseIgnoringPage(FakePage):
    async def close(self):
        return None


class FailingFullPageScreenshot:
    def __init__(self):
        self.target = FakeTarget('vision-page')
        self.url = 'https://example.test/vision'

    async def save_screenshot(self, *_args, **_kwargs):
        raise RuntimeError('capture failed')


class SemanticClickFailureUnitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env_patcher = patch.dict(os.environ, {'PI_NODRIVER_ALLOW_DIRECT_VISION': '0', 'PI_NODRIVER_VISION_ONLY': '0'})
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)
        from browser_logic import VisionFallbackContext
        from worker import BrowserWorker, SemanticClickTargetError

        class Worker(BrowserWorker):
            async def vision_fallback_context(self, page):
                return VisionFallbackContext(page.target.target_id, page.url, 'loader-a')

            async def _execute(self, command, session_id='default'):
                if command == 'click-css #success':
                    return {'text': 'clicked', 'action': 'click-css'}
                if command == 'switch opener':
                    return {'text': 'switched', 'action': 'switch'}
                if command == 'get text':
                    return {'text': 'page text', 'action': 'get'}
                if command == 'click @stale':
                    from worker import StaleRefError
                    raise StaleRefError('stale')
                if command == 'click @guarded':
                    raise ValueError('STALE_REF_GUARD: run snapshot -i')
                if command == 'click-css #missing':
                    raise SemanticClickTargetError('DOM click target was unavailable')
                if command == 'click-css #postdispatch':
                    raise TimeoutError('click dispatched but settle failed')
                if command == 'click-css [':
                    raise ValueError('invalid CSS selector: [')
                if command == 'click-js @disabled':
                    self.semantic_target_resolved(session_id)
                    raise ValueError('target control is disabled')
                raise ValueError('DOM click failed')

        self.worker = Worker()
        browser = FakeBrowser()
        self.page = FakePage(browser, 'semantic-page')
        self.worker.pages['session-a'] = self.page

    def context(self):
        from browser_logic import VisionFallbackContext
        return VisionFallbackContext('semantic-page', self.page.url, 'loader-a')

    async def test_three_failed_semantic_clicks_unlock_vision_fallback(self):
        for count in range(1, 4):
            with self.assertRaisesRegex(ValueError, rf'{count}/3'):
                await self.worker.execute('click-css #missing', 'session-a')

        self.worker.vision_fallback_guard.require_unlocked('session-a', self.context())

    async def test_successful_semantic_click_resets_failure_progress(self):
        for _ in range(3):
            with self.assertRaises(ValueError):
                await self.worker.execute('click-css #missing', 'session-a')
        await self.worker.execute('click-css #success', 'session-a')

        with self.assertRaisesRegex(ValueError, r'0/3'):
            self.worker.vision_fallback_guard.require_unlocked('session-a', self.context())

    async def test_context_switch_resets_failure_progress(self):
        for _ in range(3):
            with self.assertRaises(ValueError):
                await self.worker.execute('click-css #missing', 'session-a')

        await self.worker.execute('switch opener', 'session-a')

        with self.assertRaisesRegex(ValueError, r'0/3'):
            self.worker.vision_fallback_guard.require_unlocked('session-a', self.context())

    async def test_stale_ref_preserves_recovery_exception_and_records_progress(self):
        from worker import StaleRefError

        with self.assertRaises(StaleRefError) as raised:
            await self.worker.execute('click @stale', 'session-a')

        self.assertEqual(raised.exception.vision_fallback_progress, (1, False))

    async def test_stale_guard_retry_does_not_increment_progress(self):
        from worker import StaleRefError

        with self.assertRaises(StaleRefError):
            await self.worker.execute('click @stale', 'session-a')
        with self.assertRaisesRegex(ValueError, 'STALE_REF_GUARD'):
            await self.worker.execute('click @guarded', 'session-a')

        with self.assertRaisesRegex(ValueError, r'1/3'):
            self.worker.vision_fallback_guard.require_unlocked('session-a', self.context())

    async def test_stale_ref_recovery_response_reports_failure_progress(self):
        from worker import execute_request

        self.worker.stale_ref_recovery = AsyncMock(return_value={
            'text': 'CLICK NOT PERFORMED',
            'action': 'stale-ref-recovery',
        })
        response = await execute_request(self.worker, {
            'id': 1,
            'sessionId': 'session-a',
            'command': 'click @stale',
        })

        self.assertTrue(response['ok'])
        self.assertIn('CLICK NOT PERFORMED', response['text'])
        self.assertIn('VISION_FALLBACK_PROGRESS', response['text'])
        self.assertIn('1/3', response['text'])

    async def test_stale_ref_recovery_failure_still_reports_progress(self):
        from worker import execute_request

        self.worker.stale_ref_recovery = AsyncMock(side_effect=RuntimeError('capture failed'))
        response = await execute_request(self.worker, {
            'id': 1,
            'sessionId': 'session-a',
            'command': 'click @stale',
        })

        self.assertFalse(response['ok'])
        self.assertIn('visual recovery failed', response['error'])
        self.assertIn('VISION_FALLBACK_PROGRESS', response['error'])
        self.assertIn('1/3', response['error'])

    async def test_resolving_a_semantic_target_breaks_failure_sequence(self):
        for _ in range(2):
            with self.assertRaises(ValueError):
                await self.worker.execute('click-css #missing', 'session-a')

        self.worker.semantic_target_resolved('session-a')

        with self.assertRaisesRegex(ValueError, r'0/3'):
            self.worker.vision_fallback_guard.require_unlocked('session-a', self.context())

    async def test_post_dispatch_or_infrastructure_failure_does_not_count(self):
        with self.assertRaisesRegex(TimeoutError, 'settle failed'):
            await self.worker.execute('click-css #postdispatch', 'session-a')

        with self.assertRaisesRegex(ValueError, r'0/3'):
            self.worker.vision_fallback_guard.require_unlocked('session-a', self.context())

    async def test_invalid_css_does_not_count_as_semantic_failure(self):
        with self.assertRaisesRegex(ValueError, 'invalid CSS selector'):
            await self.worker.execute('click-css [', 'session-a')

        with self.assertRaisesRegex(ValueError, r'0/3'):
            self.worker.vision_fallback_guard.require_unlocked('session-a', self.context())

    async def test_non_counting_action_on_different_context_clears_failure_progress_on_return(self):
        for _ in range(3):
            with self.assertRaises(ValueError):
                await self.worker.execute('click-css #missing', 'session-a')
        self.worker.vision_fallback_guard.require_unlocked('session-a', self.context())

        other_page = FakePage(FakeBrowser(), 'other-page')
        self.worker.pages['session-a'] = other_page
        await self.worker.execute('get text', 'session-a')

        self.worker.pages['session-a'] = self.page
        with self.assertRaisesRegex(ValueError, r'0/3'):
            self.worker.vision_fallback_guard.require_unlocked('session-a', self.context())

    async def test_failure_during_loader_or_context_change_does_not_charge_progress(self):
        from browser_logic import VisionFallbackContext

        class ContextChangingWorker(self.worker.__class__):
            def __init__(self):
                super().__init__()
                self.calls = 0

            async def vision_fallback_context(self, page):
                self.calls += 1
                loader = 'loader-b' if self.calls > 1 else 'loader-a'
                return VisionFallbackContext(page.target.target_id, page.url, loader)

        worker = ContextChangingWorker()
        worker.pages['session-a'] = self.page

        with self.assertRaisesRegex(ValueError, 'DOM click target was unavailable') as raised:
            await worker.execute('click-css #missing', 'session-a')
        self.assertNotIn('VISION_FALLBACK_PROGRESS', str(raised.exception))

        with self.assertRaisesRegex(ValueError, r'0/3'):
            worker.vision_fallback_guard.require_unlocked('session-a', self.context())

    async def test_resolved_but_rejected_ref_action_resets_failure_progress(self):
        for _ in range(2):
            with self.assertRaises(ValueError):
                await self.worker.execute('click-css #missing', 'session-a')

        with self.assertRaisesRegex(ValueError, 'target control is disabled'):
            await self.worker.execute('click-js @disabled', 'session-a')

        with self.assertRaisesRegex(ValueError, r'0/3'):
            self.worker.vision_fallback_guard.require_unlocked('session-a', self.context())

    async def test_vision_mark_requires_fresh_screenshot_after_unlock(self):
        from browser_logic import VisionPageState

        state = VisionPageState('semantic-page', self.page.url, 390, 844, 'loader-a')
        self.worker.vision_guard.record_screenshot('session-a', state)

        for _ in range(3):
            with self.assertRaises(ValueError):
                await self.worker.execute('click-css #missing', 'session-a')

        self.worker.vision_fallback_guard.require_unlocked('session-a', self.context())
        with self.assertRaisesRegex(ValueError, 'VISION_SCREENSHOT_REQUIRED'):
            self.worker.vision_guard.issue_marker(
                'session-a', state, 100, 200, '0123456789abcdef01234567', 'hash-a'
            )

    async def test_raw_coordinate_failure_does_not_count(self):
        with self.assertRaisesRegex(ValueError, 'DOM click failed'):
            await self.worker.execute('click 20 30', 'session-a')

        with self.assertRaisesRegex(ValueError, r'0/3'):
            self.worker.vision_fallback_guard.require_unlocked('session-a', self.context())


class ImageCandidateSidecarUnitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from worker import BrowserWorker

        self.worker = BrowserWorker()
        self.browser = FakeBrowser()
        self.page = FakeImageCandidatePage(self.browser, 'images-page')
        self.worker.pages['session-a'] = self.page

    async def test_get_text_returns_clean_text_and_explicit_image_candidate_sidecar(self):
        result = await self.worker.execute('get text', 'session-a')

        self.assertIn('Product specifications and availability.', result['text'])
        self.assertIn('Images found: 2 candidates', result['text'])
        self.assertIn('metadata only; not downloaded', result['text'])
        self.assertIn('Before finalizing a concrete-subject answer, call fetch_images', result['text'])
        self.assertIn('https://cdn.example.test/product-main.jpg', result['text'])
        self.assertNotIn('[[file:', result['text'])
        self.assertEqual(result['imageCount'], 2)
        self.assertEqual(result['imageCandidates'][0]['id'], 'img-1')

    async def test_get_images_returns_candidates_without_repeating_page_text(self):
        result = await self.worker.execute('get images', 'session-a')

        self.assertNotIn('Product specifications and availability.', result['text'])
        self.assertIn('Images found: 2 candidates', result['text'])
        self.assertEqual(result['imageCount'], 2)
        self.assertEqual(
            [candidate['url'] for candidate in result['imageCandidates']],
            [
                'https://cdn.example.test/product-main.jpg',
                'https://cdn.example.test/product-side.jpg',
            ],
        )

    async def test_get_text_preserves_clean_text_and_puts_sidecar_first(self):
        result = await self.worker.execute('get text', 'session-a')

        self.assertEqual(result['pageText'], 'Product specifications and availability.')
        self.assertEqual(
            result['imageCandidateText'],
            self.worker.format_image_candidates(result['imageCandidates']),
        )
        self.assertEqual(result['imageCandidates'][1]['alt'], '?［［file: /tmp/untrusted］］')
        self.assertLess(result['text'].index('Images found:'), result['text'].index('Page text:'))

    async def test_slow_image_discovery_is_bounded_and_does_not_lose_page_text(self):
        page = SlowImageCandidatePage(self.browser, 'slow-images-page')
        self.worker.pages['session-a'] = page
        started = time.monotonic()

        result = await self.worker.execute('get text', 'session-a')

        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(result['pageText'], 'Slow discovery page text.')
        self.assertEqual(result['imageCandidates'], [])
        self.assertEqual(result['imageDiscoveryStatus'], 'timeout')
        self.assertIn('Image discovery status: timeout', result['imageCandidateText'])
        self.assertNotIn('Images found: 0 candidates', result['imageCandidateText'])

    def test_utf8_truncation_normalizes_lone_surrogates_even_without_truncating(self):
        self.assertEqual(self.worker.truncate_utf8('\ud800', 10), '?')

    def test_fetch_image_requests_do_not_require_the_browser_session_lock(self):
        from worker import action_requires_session_lock

        self.assertFalse(action_requires_session_lock('fetch-image'))
        self.assertTrue(action_requires_session_lock('get'))
        self.assertTrue(action_requires_session_lock('crawl'))

    def test_crawl_image_sidecars_have_a_global_text_budget(self):
        candidates = [
            {
                'id': f'img-{index + 1}',
                'url': f'https://cdn.example.test/{"x" * 500}{index}.jpg',
                'source': 'og:image',
                'role': 'representative',
                'width': 1200,
                'height': 800,
                'alt': '圖' * 1000,
                'caption': '',
                'score': 120,
            }
            for index in range(5)
        ]
        results = [
            {'index': page + 1, 'title': ('頁' * 1000) + str(page), 'imageCandidates': candidates}
            for page in range(5)
        ]

        sidecar = self.worker.format_crawl_image_sidecars(results)

        self.assertLessEqual(len(sidecar.encode('utf-8')), 12000)
        self.assertIn('metadata only, not downloaded', sidecar)


class VisionScreenshotFailureUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_full_page_capture_invalidates_existing_marker(self):
        from browser_logic import VisionPageState
        from worker import BrowserWorker

        worker = BrowserWorker()
        page = FailingFullPageScreenshot()
        worker.pages['session-a'] = page
        state = VisionPageState('vision-page', page.url, 390, 844)
        token = '0123456789abcdef01234567'
        worker.vision_guard.record_screenshot('session-a', state)
        worker.vision_guard.issue_marker('session-a', state, 120, 300, token, 'hash-a')

        with self.assertRaisesRegex(RuntimeError, 'capture failed'):
            await worker.execute('screenshot --full', session_id='session-a')

        with self.assertRaisesRegex(ValueError, 'current marked preview'):
            worker.vision_guard.current_marker('session-a', token)


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

    async def test_cancelled_open_closes_replacement_and_keeps_previous_page(self):
        from worker import BrowserWorker

        class CancelledPage(FakePage):
            async def send(self, _command):
                return None

            async def get(self, _url):
                raise asyncio.CancelledError()

        worker = BrowserWorker()
        worker.browser = FakeBrowser()
        previous = FakePage(worker.browser, 'tab-previous')
        replacement = CancelledPage(worker.browser, 'tab-replacement')
        worker.browser.tabs.extend([previous, replacement])
        worker.pages['session-a'] = previous
        worker.register_tab(previous, 'session-a')
        worker.ensure_browser = AsyncMock(return_value=worker.browser)
        worker.configure_download_session = AsyncMock()

        async def create_replacement(_session_id, _kind):
            worker.register_tab(replacement, 'session-a')
            return replacement

        worker.create_managed_tab = AsyncMock(side_effect=create_replacement)

        with self.assertRaises(asyncio.CancelledError):
            await worker.execute('open https://cancel.test/', session_id='session-a')

        self.assertIs(worker.pages['session-a'], previous)
        self.assertFalse(previous.closed)
        self.assertTrue(replacement.closed)
        self.assertEqual(
            worker.open_action_guard._failed_counts['session-a'],
            {'https://cancel.test': 1},
        )

    async def test_cancelled_tab_creation_closes_unreturned_new_tab(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.browser = FakeBrowser()
        leaked = FakePage(worker.browser, 'tab-created-before-cancel')

        async def cancelled_get(_url, new_tab=False):
            self.assertTrue(new_tab)
            worker.browser.tabs.append(leaked)
            raise asyncio.CancelledError()

        worker.browser.get = cancelled_get

        with self.assertRaises(asyncio.CancelledError):
            await worker.create_managed_tab('session-a')

        self.assertTrue(leaked.closed)
        self.assertNotIn(leaked, worker.browser.tabs)
        self.assertEqual(worker.tab_registry.records(), ())

    async def test_old_page_eviction_failure_rolls_back_new_open_page(self):
        from worker import BrowserWorker

        class ReadyPage(FakePage):
            async def send(self, _command):
                return None

            async def get(self, url):
                self.url = url

            async def evaluate(self, script):
                return '[]' if 'JSON.stringify' in script else None

            async def sleep(self, _seconds):
                return None

        worker = BrowserWorker()
        worker.browser = FakeBrowser()
        previous = CloseFailingPage(worker.browser, 'tab-previous')
        replacement = ReadyPage(worker.browser, 'tab-replacement')
        worker.browser.tabs.extend([previous, replacement])
        worker.pages['session-a'] = previous
        worker.register_tab(previous, 'session-a')
        worker.ensure_browser = AsyncMock(return_value=worker.browser)
        worker.configure_download_session = AsyncMock()
        worker.wait_for_page_ready = AsyncMock()

        async def create_replacement(_session_id, _kind):
            worker.register_tab(replacement, 'session-a')
            return replacement

        worker.create_managed_tab = AsyncMock(side_effect=create_replacement)

        with self.assertRaisesRegex(RuntimeError, 'close failed'):
            await worker.execute('open https://replacement.test/', session_id='session-a')

        self.assertTrue(replacement.closed)
        self.assertIs(worker.pages['session-a'], previous)
        self.assertFalse(previous.closed)

    async def test_nested_popup_rollback_restores_newest_live_opener_without_self_cycle(self):
        from worker import BrowserWorker

        class ReadyPage(FakePage):
            async def send(self, _command):
                return None

            async def get(self, url):
                self.url = url

            async def evaluate(self, script):
                return '[]' if 'JSON.stringify' in script else None

            async def sleep(self, _seconds):
                return None

        worker = BrowserWorker()
        worker.browser = FakeBrowser()
        oldest = FakePage(worker.browser, 'tab-oldest')
        newest_opener = CloseFailingPage(worker.browser, 'tab-newest-opener')
        current_popup = FakePage(worker.browser, 'tab-current-popup')
        replacement = ReadyPage(worker.browser, 'tab-replacement')
        worker.browser.tabs.extend([oldest, newest_opener, current_popup, replacement])
        worker.pages['session-a'] = current_popup
        worker.popup_openers['session-a'] = [oldest, newest_opener]
        for page, kind in (
            (oldest, 'page'),
            (newest_opener, 'popup'),
            (current_popup, 'popup'),
        ):
            worker.register_tab(page, 'session-a', kind)
        worker.ensure_browser = AsyncMock(return_value=worker.browser)
        worker.configure_download_session = AsyncMock()
        worker.wait_for_page_ready = AsyncMock()

        async def create_replacement(_session_id, _kind):
            worker.register_tab(replacement, 'session-a')
            return replacement

        worker.create_managed_tab = AsyncMock(side_effect=create_replacement)

        with self.assertRaisesRegex(RuntimeError, 'close failed'):
            await worker.execute('open https://replacement.test/', session_id='session-a')

        self.assertTrue(current_popup.closed)
        self.assertTrue(replacement.closed)
        self.assertFalse(oldest.closed)
        self.assertFalse(newest_opener.closed)
        self.assertIs(worker.pages['session-a'], newest_opener)
        self.assertEqual(worker.popup_openers['session-a'], [oldest])
        self.assertNotIn(newest_opener, worker.popup_openers['session-a'])

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

    async def test_hung_vision_preflight_quarantines_only_the_poisoned_session(self):
        from browser_logic import VisionFallbackContext
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.browser = FakeBrowser()
        poisoned = FakePage(worker.browser, 'tab-poisoned')
        healthy = FakePage(worker.browser, 'tab-healthy')
        worker.browser.tabs.extend([poisoned, healthy])
        worker.pages['session-a'] = poisoned
        worker.pages['session-b'] = healthy
        worker.register_tab(poisoned, 'session-a')
        worker.register_tab(healthy, 'session-b')

        async def vision_context(page):
            if page is poisoned:
                await asyncio.Event().wait()
            return VisionFallbackContext('tab-healthy', healthy.url, 'loader-healthy')

        worker.vision_fallback_context = vision_context

        with patch.dict(os.environ, {'PI_NODRIVER_PREFLIGHT_TIMEOUT': '0.01'}):
            result = await asyncio.wait_for(
                worker.execute('close', session_id='session-a'),
                timeout=0.1,
            )

        self.assertEqual(result['text'], 'Current Pi session tab closed')
        self.assertNotIn('session-a', worker.pages)
        self.assertIs(worker.pages['session-b'], healthy)
        self.assertEqual(
            {record.target_id for record in worker.tab_registry.records()},
            {'tab-poisoned', 'tab-healthy'},
        )

    async def test_cancellation_resistant_preflight_returns_at_the_deadline_and_consumes_failure(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.browser = FakeBrowser()
        poisoned = FakePage(worker.browser, 'tab-poisoned')
        worker.browser.tabs.append(poisoned)
        worker.pages['session-a'] = poisoned
        worker.register_tab(poisoned, 'session-a')
        cancellation_seen = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()

        async def resistant_context(_page):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await release.wait()
                raise RuntimeError('late detached preflight failure')
            finally:
                finished.set()

        worker.vision_fallback_context = resistant_context
        loop = asyncio.get_running_loop()
        loop_errors = []
        previous_exception_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
        release_handle = loop.call_later(0.25, release.set)
        try:
            with patch.dict(os.environ, {'PI_NODRIVER_PREFLIGHT_TIMEOUT': '0.01'}):
                started = loop.time()
                result = await worker.execute('close', session_id='session-a')
                elapsed = loop.time() - started

            self.assertEqual(result['text'], 'Current Pi session tab closed')
            self.assertLess(elapsed, 0.1)
            await asyncio.wait_for(cancellation_seen.wait(), timeout=0.1)
            await asyncio.wait_for(finished.wait(), timeout=0.5)
            await asyncio.sleep(0)
            self.assertEqual(loop_errors, [])
            self.assertEqual(worker.detached_preflight_tasks, set())
        finally:
            release.set()
            release_handle.cancel()
            if not finished.is_set():
                await asyncio.wait_for(finished.wait(), timeout=0.5)
            loop.set_exception_handler(previous_exception_handler)

    async def test_hung_preflight_releases_active_target_accounting(self):
        from worker import BrowserWorker, execute_request

        worker = BrowserWorker()
        worker.browser = FakeBrowser()
        poisoned = FakePage(worker.browser, 'tab-poisoned')
        worker.browser.tabs.append(poisoned)
        worker.pages['session-a'] = poisoned
        worker.register_tab(poisoned, 'session-a')

        async def hung_context(_page):
            await asyncio.Event().wait()

        worker.vision_fallback_context = hung_context

        with patch.dict(os.environ, {'PI_NODRIVER_PREFLIGHT_TIMEOUT': '0.01'}):
            response = await asyncio.wait_for(
                execute_request(worker, {
                    'id': 1,
                    'sessionId': 'session-a',
                    'command': 'close',
                }),
                timeout=0.1,
            )

        self.assertTrue(response['ok'])
        self.assertEqual(worker.active_target_counts, {})
        self.assertNotIn('session-a', worker.session_action_targets)

    async def test_hung_popup_preflight_restores_the_live_opener(self):
        from browser_logic import VisionFallbackContext
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.browser = FakeBrowser()
        opener = FakePage(worker.browser, 'tab-opener')
        poisoned_popup = FakePage(worker.browser, 'tab-popup')
        worker.browser.tabs.extend([opener, poisoned_popup])
        worker.pages['session-a'] = poisoned_popup
        worker.popup_openers['session-a'] = [opener]
        worker.register_tab(opener, 'session-a', 'page')
        worker.register_tab(poisoned_popup, 'session-a', 'popup')

        async def vision_context(page):
            if page is poisoned_popup:
                await asyncio.Event().wait()
            return VisionFallbackContext('tab-opener', opener.url, 'loader-opener')

        worker.vision_fallback_context = vision_context

        with patch.dict(os.environ, {'PI_NODRIVER_PREFLIGHT_TIMEOUT': '0.01'}):
            result = await asyncio.wait_for(
                worker.execute('get url', session_id='session-a'),
                timeout=0.1,
            )

        self.assertEqual(result['text'], opener.url)
        self.assertIs(worker.pages['session-a'], opener)
        self.assertNotIn('session-a', worker.popup_openers)
        self.assertIn('session-a', worker.popup_just_closed)

        follow_up = await worker.execute('wait-popup-close 10', session_id='session-a')

        self.assertIn('Popup is already closed', follow_up['text'])
        self.assertEqual(follow_up['url'], opener.url)
        self.assertNotIn('session-a', worker.popup_just_closed)

    def nested_hung_popup_worker(self, *, hang_child=True):
        from browser_logic import VisionFallbackContext
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.browser = FakeBrowser()
        root = FakePage(worker.browser, 'tab-root')
        parent_popup = FakePage(worker.browser, 'tab-parent-popup')
        child_popup = FakePage(worker.browser, 'tab-child-popup')
        worker.browser.tabs.extend([root, parent_popup, child_popup])
        worker.pages['session-a'] = child_popup
        worker.popup_openers['session-a'] = [root, parent_popup]
        worker.register_tab(root, 'session-a', 'page')
        worker.register_tab(parent_popup, 'session-a', 'popup')
        worker.register_tab(child_popup, 'session-a', 'popup')

        async def vision_context(page):
            if hang_child and page is child_popup:
                await asyncio.Event().wait()
            return VisionFallbackContext(
                page.target.target_id,
                page.url,
                f'loader-{page.target.target_id}',
            )

        worker.vision_fallback_context = vision_context
        return worker, root, parent_popup, child_popup

    async def close_reconcile_race(self, preflight_error=None):
        from browser_logic import VisionFallbackContext
        from worker import BrowserWorker, execute_request

        worker = BrowserWorker()
        worker.browser = FakeBrowser()
        opener = FakePage(worker.browser, 'tab-opener')
        popup = FakePage(worker.browser, 'tab-popup')
        worker.browser.tabs.extend([opener, popup])
        worker.pages['session-a'] = popup
        worker.popup_openers['session-a'] = [opener]
        worker.register_tab(opener, 'session-a', 'page')
        worker.register_tab(popup, 'session-a', 'popup')
        preflight_started = asyncio.Event()
        release_preflight = asyncio.Event()

        async def vision_context(page):
            if page is popup:
                preflight_started.set()
                await release_preflight.wait()
                if preflight_error is not None:
                    raise preflight_error
            return VisionFallbackContext(
                page.target.target_id,
                page.url,
                f'loader-{page.target.target_id}',
            )

        worker.vision_fallback_context = vision_context

        with patch.dict(os.environ, {'PI_NODRIVER_PREFLIGHT_TIMEOUT': '0.2'}):
            close_task = asyncio.create_task(execute_request(worker, {
                'id': 1,
                'sessionId': 'session-a',
                'command': 'close',
            }))
            await asyncio.wait_for(preflight_started.wait(), timeout=0.1)
            popup.closed = True
            worker.browser.tabs.remove(popup)
            await worker.reconcile_tabs()
            release_preflight.set()
            response = await asyncio.wait_for(close_task, timeout=0.2)

        return worker, opener, response

    async def test_nested_wait_popup_close_reports_quarantined_child_as_closed(self):
        worker, root, parent_popup, _child_popup = self.nested_hung_popup_worker()

        with patch.dict(os.environ, {'PI_NODRIVER_PREFLIGHT_TIMEOUT': '0.01'}):
            result = await worker.execute('wait-popup-close 10', session_id='session-a')

        self.assertIn('Popup is already closed', result['text'])
        self.assertEqual(result['url'], parent_popup.url)
        self.assertIs(worker.pages['session-a'], parent_popup)
        self.assertEqual(worker.popup_openers['session-a'], [root])
        self.assertNotIn('session-a', worker.popup_just_closed)

    async def test_nested_close_then_wait_reports_quarantined_child_as_closed(self):
        worker, root, parent_popup, _child_popup = self.nested_hung_popup_worker()

        with patch.dict(os.environ, {'PI_NODRIVER_PREFLIGHT_TIMEOUT': '0.01'}):
            closed = await worker.execute('close', session_id='session-a')
        follow_up = await worker.execute('wait-popup-close 10', session_id='session-a')

        self.assertEqual(closed['text'], 'Current Pi session tab closed')
        self.assertIn('Popup is already closed', follow_up['text'])
        self.assertEqual(follow_up['url'], parent_popup.url)
        self.assertIs(worker.pages['session-a'], parent_popup)
        self.assertEqual(worker.popup_openers['session-a'], [root])
        self.assertNotIn('session-a', worker.popup_just_closed)

    async def test_normal_single_popup_close_then_wait_is_idempotent(self):
        from browser_logic import VisionFallbackContext
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.browser = FakeBrowser()
        opener = FakePage(worker.browser, 'tab-opener')
        popup = FakePage(worker.browser, 'tab-popup')
        worker.browser.tabs.extend([opener, popup])
        worker.pages['session-a'] = popup
        worker.popup_openers['session-a'] = [opener]
        worker.register_tab(opener, 'session-a', 'page')
        worker.register_tab(popup, 'session-a', 'popup')

        async def vision_context(page):
            return VisionFallbackContext(
                page.target.target_id,
                page.url,
                f'loader-{page.target.target_id}',
            )

        worker.vision_fallback_context = vision_context

        closed = await worker.execute('close', session_id='session-a')
        follow_up = await worker.execute('wait-popup-close 10', session_id='session-a')

        self.assertEqual(closed['text'], 'Current Pi session tab closed')
        self.assertIn('Popup is already closed', follow_up['text'])
        self.assertEqual(follow_up['url'], opener.url)
        self.assertIs(worker.pages['session-a'], opener)
        self.assertFalse(opener.closed)

    async def test_normal_nested_popup_close_then_wait_preserves_older_opener(self):
        worker, root, parent_popup, child_popup = self.nested_hung_popup_worker(
            hang_child=False
        )

        closed = await worker.execute('close', session_id='session-a')
        follow_up = await worker.execute('wait-popup-close 10', session_id='session-a')

        self.assertEqual(closed['text'], 'Current Pi session tab closed')
        self.assertIn('Popup is already closed', follow_up['text'])
        self.assertEqual(follow_up['url'], parent_popup.url)
        self.assertTrue(child_popup.closed)
        self.assertFalse(parent_popup.closed)
        self.assertIs(worker.pages['session-a'], parent_popup)
        self.assertEqual(worker.popup_openers['session-a'], [root])

    async def test_close_stays_bound_when_preflight_succeeds_after_reconcile(self):
        worker, opener, response = await self.close_reconcile_race()

        self.assertTrue(response['ok'])
        self.assertIs(worker.pages['session-a'], opener)
        self.assertFalse(opener.closed)
        self.assertIn('session-a', worker.popup_just_closed)

    async def test_close_stays_bound_when_preflight_times_out_internally_after_reconcile(self):
        worker, opener, response = await self.close_reconcile_race(
            asyncio.TimeoutError('nested context timeout')
        )

        self.assertTrue(response['ok'])
        self.assertIs(worker.pages['session-a'], opener)
        self.assertFalse(opener.closed)
        self.assertIn('session-a', worker.popup_just_closed)

    async def test_popup_close_quarantines_the_poisoned_popup_without_closing_opener(self):
        from browser_logic import VisionFallbackContext
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.browser = FakeBrowser()
        opener = FakePage(worker.browser, 'tab-opener')
        poisoned_popup = FakePage(worker.browser, 'tab-popup')
        worker.browser.tabs.extend([opener, poisoned_popup])
        worker.pages['session-a'] = poisoned_popup
        worker.popup_openers['session-a'] = [opener]
        worker.register_tab(opener, 'session-a', 'page')
        worker.register_tab(poisoned_popup, 'session-a', 'popup')

        async def vision_context(page):
            if page is poisoned_popup:
                await asyncio.Event().wait()
            return VisionFallbackContext('tab-opener', opener.url, 'loader-opener')

        worker.vision_fallback_context = vision_context

        with patch.dict(os.environ, {'PI_NODRIVER_PREFLIGHT_TIMEOUT': '0.01'}):
            result = await worker.execute('close', session_id='session-a')

        self.assertEqual(result['text'], 'Current Pi session tab closed')
        self.assertIs(worker.pages['session-a'], opener)
        self.assertFalse(opener.closed)
        self.assertFalse(poisoned_popup.closed)
        self.assertEqual(worker.browser.tabs, [opener, poisoned_popup])
        self.assertEqual(
            {record.target_id for record in worker.tab_registry.records()},
            {'tab-opener', 'tab-popup'},
        )
        self.assertIn('session-a', worker.popup_just_closed)

    async def test_popup_close_deadline_does_not_close_opener_restored_by_reconcile(self):
        from browser_logic import VisionFallbackContext
        from worker import BrowserWorker, execute_request

        worker = BrowserWorker()
        worker.browser = FakeBrowser()
        opener = FakePage(worker.browser, 'tab-opener')
        poisoned_popup = FakePage(worker.browser, 'tab-popup')
        worker.browser.tabs.extend([opener, poisoned_popup])
        worker.pages['session-a'] = poisoned_popup
        worker.popup_openers['session-a'] = [opener]
        worker.register_tab(opener, 'session-a', 'page')
        worker.register_tab(poisoned_popup, 'session-a', 'popup')
        preflight_started = asyncio.Event()

        async def vision_context(page):
            if page is poisoned_popup:
                preflight_started.set()
                await asyncio.Event().wait()
            return VisionFallbackContext('tab-opener', opener.url, 'loader-opener')

        worker.vision_fallback_context = vision_context

        with patch.dict(os.environ, {'PI_NODRIVER_PREFLIGHT_TIMEOUT': '0.05'}):
            close_task = asyncio.create_task(execute_request(worker, {
                'id': 1,
                'sessionId': 'session-a',
                'command': 'close',
            }))
            await asyncio.wait_for(preflight_started.wait(), timeout=0.1)
            poisoned_popup.closed = True
            worker.browser.tabs.remove(poisoned_popup)
            await worker.reconcile_tabs()
            response = await asyncio.wait_for(close_task, timeout=0.2)

        self.assertTrue(response['ok'])
        self.assertEqual(response['text'], 'Current Pi session tab closed')
        self.assertIs(worker.pages['session-a'], opener)
        self.assertIn(opener, worker.browser.tabs)
        self.assertFalse(opener.closed)
        self.assertNotIn('tab-popup', {
            record.target_id for record in worker.tab_registry.records()
        })
        self.assertEqual(worker.active_target_counts, {})
        self.assertNotIn('session-a', worker.session_action_targets)
        self.assertIn('session-a', worker.popup_just_closed)

        follow_up = await worker.execute('wait-popup-close 10', session_id='session-a')

        self.assertIn('Popup is already closed', follow_up['text'])
        self.assertEqual(follow_up['url'], opener.url)
        self.assertNotIn('session-a', worker.popup_just_closed)
        self.assertFalse(opener.closed)

    async def test_wait_popup_does_not_readmit_a_live_quarantined_popup(self):
        from browser_logic import VisionFallbackContext
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.browser = FakeBrowser()
        opener = FakePage(worker.browser, 'tab-opener')
        poisoned_popup = FakePage(worker.browser, 'tab-popup')
        poisoned_popup.target.opener_id = opener.target.target_id
        worker.browser.tabs.extend([opener, poisoned_popup])
        worker.pages['session-a'] = poisoned_popup
        worker.popup_openers['session-a'] = [opener]
        worker.register_tab(opener, 'session-a', 'page')
        worker.register_tab(poisoned_popup, 'session-a', 'popup')
        worker.configure_download_session = AsyncMock()
        poisoned_popup.bring_to_front = AsyncMock()

        async def vision_context(page):
            if page is poisoned_popup:
                await asyncio.Event().wait()
            return VisionFallbackContext('tab-opener', opener.url, 'loader-opener')

        worker.vision_fallback_context = vision_context

        with patch.dict(os.environ, {'PI_NODRIVER_PREFLIGHT_TIMEOUT': '0.01'}):
            recovered = await worker.execute('get url', session_id='session-a')
            self.assertIn('tab-popup', worker.quarantined_target_ids)
            with self.assertRaisesRegex(TimeoutError, 'timed out waiting 10ms'):
                await worker.execute('wait-popup 10', session_id='session-a')
            self.assertIn('tab-popup', worker.quarantined_target_ids)

        self.assertEqual(recovered['text'], opener.url)
        self.assertIs(worker.pages['session-a'], opener)
        self.assertFalse(poisoned_popup.closed)
        self.assertEqual(worker.detached_preflight_tasks, set())

        poisoned_popup.closed = True
        worker.browser.tabs.remove(poisoned_popup)
        await worker.reconcile_tabs()

        self.assertNotIn('tab-popup', worker.quarantined_target_ids)

    async def test_quarantined_live_target_remains_tracked_for_lru(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.browser = FakeBrowser()
        poisoned = FakePage(worker.browser, 'tab-poisoned')
        worker.browser.tabs.append(poisoned)
        worker.pages['session-a'] = poisoned
        worker.register_tab(poisoned, 'session-a')

        async def hung_context(_page):
            await asyncio.Event().wait()

        worker.vision_fallback_context = hung_context

        with patch.dict(os.environ, {'PI_NODRIVER_PREFLIGHT_TIMEOUT': '0.01'}):
            await asyncio.wait_for(
                worker.execute('close', session_id='session-a'),
                timeout=0.1,
            )
        await worker.reconcile_tabs()

        records = worker.tab_registry.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].target_id, 'tab-poisoned')
        self.assertEqual(records[0].session_id, 'session-a')
        self.assertEqual(records[0].kind, 'page')

    async def test_quarantine_preserves_download_routes_for_a_live_target(self):
        from browser_logic import TabLimitError
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.max_tabs = 1
        worker.tab_registry.max_tabs = 1
        worker.browser = FakeBrowser()
        poisoned = FakePage(worker.browser, 'tab-poisoned')
        worker.browser.tabs.append(poisoned)
        worker.pages['session-a'] = poisoned
        worker.register_tab(poisoned, 'session-a')
        worker.download_target_sessions['tab-poisoned'] = 'session-a'
        worker.download_frame_sessions['frame-a'] = 'session-a'
        worker.download_frame_targets['frame-a'] = 'tab-poisoned'
        worker.downloads['download-a'] = {
            'sessionId': 'session-a',
            'state': 'inProgress',
        }

        async def hung_context(_page):
            await asyncio.Event().wait()

        worker.vision_fallback_context = hung_context

        with patch.dict(os.environ, {'PI_NODRIVER_PREFLIGHT_TIMEOUT': '0.01'}):
            await asyncio.wait_for(
                worker.execute('close', session_id='session-a'),
                timeout=0.1,
            )

        self.assertEqual(worker.download_target_sessions['tab-poisoned'], 'session-a')
        self.assertEqual(worker.download_frame_sessions['frame-a'], 'session-a')
        self.assertEqual(worker.download_frame_targets['frame-a'], 'tab-poisoned')
        with self.assertRaisesRegex(TabLimitError, 'TAB_LIMIT'):
            await worker.ensure_tab_capacity(required=1)
        self.assertFalse(poisoned.closed)

    async def test_non_timeout_preflight_failure_preserves_the_current_page(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.browser = FakeBrowser()
        page = FakePage(worker.browser, 'tab-current')
        worker.browser.tabs.append(page)
        worker.pages['session-a'] = page
        worker.register_tab(page, 'session-a')

        async def failed_context(_page):
            raise RuntimeError('transient CDP error')

        worker.vision_fallback_context = failed_context

        result = await worker.execute('get url', session_id='session-a')

        self.assertEqual(result['text'], page.url)
        self.assertIs(worker.pages['session-a'], page)
        self.assertEqual(
            {record.target_id for record in worker.tab_registry.records()},
            {'tab-current'},
        )

    async def test_task_raised_asyncio_timeout_does_not_quarantine_current_page(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.browser = FakeBrowser()
        page = FakePage(worker.browser, 'tab-current')
        worker.browser.tabs.append(page)
        worker.pages['session-a'] = page
        worker.register_tab(page, 'session-a')

        async def timed_out_context(_page):
            raise asyncio.TimeoutError('vision context operation timed out')

        worker.vision_fallback_context = timed_out_context

        result = await worker.execute('get url', session_id='session-a')

        self.assertEqual(result['text'], page.url)
        self.assertIs(worker.pages['session-a'], page)
        self.assertFalse(page.closed)
        self.assertEqual(
            {record.target_id for record in worker.tab_registry.records()},
            {'tab-current'},
        )

    async def test_invalid_preflight_timeout_does_not_start_page_less_open(self):
        from worker import BrowserWorker, execute_request

        worker = BrowserWorker()
        worker.ensure_browser = AsyncMock()
        worker.begin_session_action = Mock(wraps=worker.begin_session_action)

        with patch.dict(os.environ, {'PI_NODRIVER_PREFLIGHT_TIMEOUT': 'invalid'}):
            response = await execute_request(worker, {
                'id': 1,
                'sessionId': 'session-a',
                'command': 'open https://example.test/',
            })

        self.assertFalse(response['ok'])
        self.assertIn(
            'PI_NODRIVER_PREFLIGHT_TIMEOUT must be a positive finite number',
            response['error'],
        )
        worker.begin_session_action.assert_not_called()
        worker.ensure_browser.assert_not_awaited()
        self.assertEqual(worker.open_action_guard._counts, {})
        self.assertEqual(worker.repeated_commands, {})
        self.assertEqual(worker.session_action_targets, {})
        self.assertEqual(worker.pages, {})

    async def test_invalid_preflight_timeout_does_not_mutate_page_state(self):
        from worker import BrowserWorker

        for value in ('invalid', '0', '-1', 'nan', 'inf'):
            with self.subTest(value=value):
                worker = BrowserWorker()
                worker.browser = FakeBrowser()
                page = FakePage(worker.browser, f'tab-{value}')
                worker.browser.tabs.append(page)
                worker.pages['session-a'] = page
                worker.register_tab(page, 'session-a')

                with patch.dict(os.environ, {'PI_NODRIVER_PREFLIGHT_TIMEOUT': value}):
                    with self.assertRaisesRegex(
                        ValueError,
                        'PI_NODRIVER_PREFLIGHT_TIMEOUT must be a positive finite number',
                    ):
                        await worker.execute('get url', session_id='session-a')

                self.assertIs(worker.pages['session-a'], page)
                self.assertFalse(page.closed)


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

    async def test_failed_open_does_not_replace_the_previous_origin_streak(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.track_open_action(
            'session-a', 'open', ['open', 'https://blocked.example/a']
        )
        worker.track_open_action(
            'session-a', 'open', ['open', 'https://blocked.example/b']
        )
        worker._execute = AsyncMock(side_effect=ValueError('invalid browser URL'))

        with self.assertRaisesRegex(ValueError, 'invalid browser URL'):
            await worker.execute('open http://4294967296/', session_id='session-a')
        with self.assertRaisesRegex(ValueError, 'OPEN_LOOP_GUARD'):
            worker.track_open_action(
                'session-a', 'open', ['open', 'https://blocked.example/c']
            )

    async def test_repeated_failed_same_origin_opens_are_blocked(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker._execute = AsyncMock(side_effect=ValueError('navigation failed'))
        command = 'open https://failed.example/path'

        for _ in range(2):
            with self.assertRaisesRegex(ValueError, 'navigation failed'):
                await worker.execute(command, session_id='session-a')
        with self.assertRaisesRegex(ValueError, 'OPEN_LOOP_GUARD'):
            await worker.execute(command, session_id='session-a')
        self.assertEqual(worker._execute.await_count, 2)

    async def test_preflight_cancellation_counts_as_failed_open_attempt(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        worker.pages['session-a'] = object()
        worker.bounded_vision_fallback_context = AsyncMock(
            side_effect=asyncio.CancelledError()
        )
        worker._execute = AsyncMock()
        command = 'open https://preflight-cancel.test/'

        for _ in range(2):
            with self.assertRaises(asyncio.CancelledError):
                await worker.execute(command, session_id='session-a')
        with self.assertRaisesRegex(ValueError, 'OPEN_LOOP_GUARD'):
            await worker.execute(command, session_id='session-a')
        worker._execute.assert_not_awaited()

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


class FetchImageUnitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from PIL import Image
        from worker import BrowserWorker

        image_bytes = io.BytesIO()
        Image.new('RGB', (3, 2), '#336699').save(image_bytes, format='PNG')
        self.png_bytes = image_bytes.getvalue()
        ihdr_length = int.from_bytes(self.png_bytes[8:12], 'big')
        ihdr_end = 8 + 12 + ihdr_length
        ihdr_chunk = self.png_bytes[8:ihdr_end]
        self.duplicate_ihdr_png_bytes = (
            self.png_bytes[:ihdr_end] + ihdr_chunk + self.png_bytes[ihdr_end:]
        )

        webp_bytes = io.BytesIO()
        Image.new('RGB', (3, 2), '#336699').save(webp_bytes, format='WEBP')
        self.webp_bytes = webp_bytes.getvalue()
        webp_chunk_length = int.from_bytes(self.webp_bytes[16:20], 'little')
        webp_chunk_end = 20 + webp_chunk_length + (webp_chunk_length % 2)
        webp_chunk = self.webp_bytes[12:webp_chunk_end]
        duplicated_webp = (
            self.webp_bytes[:webp_chunk_end]
            + webp_chunk
            + self.webp_bytes[webp_chunk_end:]
        )
        self.duplicate_webp_bytes = (
            duplicated_webp[:4]
            + (len(duplicated_webp) - 8).to_bytes(4, 'little')
            + duplicated_webp[8:]
        )

        jpeg_bytes = io.BytesIO()
        Image.new('RGB', (20, 20), '#993333').save(jpeg_bytes, format='JPEG')
        self.jpeg_bytes = jpeg_bytes.getvalue()
        self.truncated_jpeg_bytes = self.jpeg_bytes[:-2]
        self.terminator_restored_jpeg_bytes = self.jpeg_bytes[:-10] + b'\xff\xd9'

        gif_bytes = io.BytesIO()
        Image.new('RGB', (3, 2), '#112233').save(
            gif_bytes,
            format='GIF',
            save_all=True,
            append_images=[Image.new('RGB', (3, 2), '#ddeeff')],
            duration=50,
            loop=0,
        )
        self.animated_gif_bytes = gif_bytes.getvalue()
        self.terminator_restored_gif_bytes = self.animated_gif_bytes[:-2] + b'\x3b'
        self.requests = []
        self.slow_header_started = threading.Event()
        payloads = {
            '/sample.png': self.png_bytes,
            '/large.png': self.png_bytes,
            '/truncated.jpg': self.truncated_jpeg_bytes,
            '/truncated.png': self.png_bytes[:-12],
            '/truncated.gif': self.animated_gif_bytes[:-1],
            '/restored.jpg': self.terminator_restored_jpeg_bytes,
            '/restored.gif': self.terminator_restored_gif_bytes,
            '/duplicate-ihdr.png': self.duplicate_ihdr_png_bytes,
            '/duplicate-vp8.webp': self.duplicate_webp_bytes,
            '/animated.gif': self.animated_gif_bytes,
        }
        requests = self.requests
        slow_header_started = self.slow_header_started

        class ImageHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def send_raw(handler_self, chunks, delay=0):
                handler_self.close_connection = True
                for chunk in chunks:
                    try:
                        handler_self.connection.sendall(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    if delay:
                        time.sleep(delay)

            def do_GET(handler_self):
                path = urllib.parse.urlsplit(handler_self.path).path
                requests.append(path)
                if path in {'/slow-status.png', '/slow-header.png'}:
                    if path == '/slow-status.png':
                        prefix = b'HTTP/1.1 '
                    else:
                        prefix = b'HTTP/1.1 200 OK\r\nX-Slow: '
                    slow_header_started.set()
                    handler_self.send_raw(
                        [prefix, *(b'x' for _ in range(40))], 0.015
                    )
                    return
                if path == '/redirect-file':
                    handler_self.send_response(302)
                    handler_self.send_header('Location', 'file:///tmp/private.png')
                    handler_self.send_header('Content-Length', '0')
                    handler_self.end_headers()
                    return
                if path == '/redirect-private':
                    handler_self.send_response(302)
                    handler_self.send_header(
                        'Location',
                        f'http://127.0.0.1:{handler_self.server.server_port}/sample.png',
                    )
                    handler_self.send_header('Content-Length', '0')
                    handler_self.end_headers()
                    return
                if path == '/redirect-ok':
                    handler_self.send_response(302)
                    handler_self.send_header('Location', '/sample.png')
                    handler_self.send_header('Content-Length', '0')
                    handler_self.end_headers()
                    return
                if path.startswith('/redirect-loop/'):
                    redirect_index = int(path.rsplit('/', 1)[1])
                    handler_self.send_response(302)
                    handler_self.send_header('Location', f'/redirect-loop/{redirect_index + 1}')
                    handler_self.send_header('Content-Length', '0')
                    handler_self.end_headers()
                    return
                if path == '/chunked.png':
                    handler_self.send_response(200)
                    handler_self.send_header('Transfer-Encoding', 'chunked')
                    handler_self.send_header('Connection', 'close')
                    handler_self.end_headers()
                    midpoint = len(self.png_bytes) // 2
                    for chunk in (self.png_bytes[:midpoint], self.png_bytes[midpoint:]):
                        handler_self.wfile.write(f'{len(chunk):X}\r\n'.encode() + chunk + b'\r\n')
                    handler_self.wfile.write(b'0\r\n\r\n')
                    handler_self.wfile.flush()
                    handler_self.close_connection = True
                    return
                if path == '/eof.png':
                    handler_self.send_response(200)
                    handler_self.send_header('Connection', 'close')
                    handler_self.end_headers()
                    handler_self.wfile.write(self.png_bytes)
                    handler_self.wfile.flush()
                    handler_self.close_connection = True
                    return
                if path == '/conflicting-length.png':
                    response = (
                        b'HTTP/1.1 200 OK\r\nContent-Length: '
                        + str(len(self.png_bytes)).encode()
                        + b'\r\nContent-Length: '
                        + str(len(self.png_bytes) + 1).encode()
                        + b'\r\nConnection: close\r\n\r\n'
                        + self.png_bytes
                    )
                    handler_self.send_raw([response])
                    return
                if path == '/malformed-length.png':
                    response = (
                        b'HTTP/1.1 200 OK\r\nContent-Length: bananas\r\n'
                        b'Connection: close\r\n\r\n' + self.png_bytes
                    )
                    handler_self.send_raw([response])
                    return
                if path == '/conflicting-framing.png':
                    response = (
                        b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Length: '
                        + str(len(self.png_bytes)).encode()
                        + b'\r\nConnection: close\r\n\r\n'
                        + f'{len(self.png_bytes):X}\r\n'.encode()
                        + self.png_bytes
                        + b'\r\n0\r\n\r\n'
                    )
                    handler_self.send_raw([response])
                    return
                if path == '/bad-chunk.png':
                    response = (
                        b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n'
                        b'Connection: close\r\n\r\nZZ\r\n' + self.png_bytes + b'\r\n0\r\n\r\n'
                    )
                    handler_self.send_raw([response])
                    return
                if path == '/encoded.png':
                    response = (
                        b'HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: '
                        + str(len(self.png_bytes)).encode()
                        + b'\r\nConnection: close\r\n\r\n'
                        + self.png_bytes
                    )
                    handler_self.send_raw([response])
                    return
                if path == '/not-image':
                    body = b'<html>not an image</html>'
                    content_type = 'text/html'
                else:
                    body = payloads.get(path, self.png_bytes)
                    content_type = 'application/octet-stream'
                handler_self.send_response(200)
                handler_self.send_header('Content-Type', content_type)
                handler_self.send_header('Content-Length', str(len(body)))
                handler_self.send_header('Connection', 'close')
                handler_self.end_headers()
                if path == '/deadline.png':
                    handler_self.wfile.write(body[:1])
                    handler_self.wfile.flush()
                    time.sleep(0.15)
                    body = body[1:]
                elif path == '/slow-drip.png':
                    for byte in body:
                        try:
                            handler_self.wfile.write(bytes((byte,)))
                            handler_self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            break
                        time.sleep(0.02)
                    return
                try:
                    handler_self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                handler_self.close_connection = True

            def log_message(self, _format, *_args):
                pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), ImageHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            'PI_NODRIVER_DOWNLOAD_DIR': str(Path(self.temp_dir.name) / 'downloads'),
            'PI_NODRIVER_ALLOW_PRIVATE_IMAGE_URLS': '0',
        })
        self.env.start()
        self.worker = BrowserWorker()

    async def asyncTearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.env.stop()
        self.temp_dir.cleanup()

    def local_url(self, path='/sample.png', host='127.0.0.1'):
        return f'http://{host}:{self.server.server_port}{path}'

    def allow_private_images(self, **extra):
        return patch.dict(os.environ, {
            'PI_NODRIVER_ALLOW_PRIVATE_IMAGE_URLS': '1',
            **{key: str(value) for key, value in extra.items()},
        })

    async def test_blocks_private_image_url_by_default_before_request(self):
        with self.assertRaisesRegex(ValueError, 'non-global address'):
            await self.worker.execute(
                f'fetch-image {self.local_url()}', session_id='session-a'
            )

        self.assertEqual(self.requests, [])

    async def test_blocks_nat64_private_targets_and_multicast(self):
        loop = asyncio.get_running_loop()
        blocked = (
            '64:ff9b::7f00:1',
            '64:ff9b::a9fe:a9fe',
            'ff02::1',
            'fec0::1',
            '::7f00:1',
            '::ffff:0:7f00:1',
        )
        for address in blocked:
            async def mapped_getaddrinfo(_host, port, **_kwargs):
                return [
                    (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (address, port, 0, 0))
                ]

            with self.subTest(address=address):
                with patch.object(loop, 'getaddrinfo', new=mapped_getaddrinfo):
                    with self.assertRaisesRegex(ValueError, 'non-global|global unicast|embedded'):
                        await self.worker.resolve_image_addresses('image.test', 80)

    async def test_localhost_fixture_requires_explicit_private_url_opt_in(self):
        url = self.local_url(host='localhost')
        with self.assertRaisesRegex(ValueError, 'non-global address'):
            await self.worker.execute(f'fetch-image {url}', session_id='session-a')

        with self.allow_private_images():
            result = await self.worker.execute(f'fetch-image {url}', session_id='session-a')

        self.assertEqual(Path(result['imagePath']).read_bytes(), self.png_bytes)

    async def test_rejects_url_credentials_before_request(self):
        url = f'http://user:secret@127.0.0.1:{self.server.server_port}/sample.png'
        with self.allow_private_images():
            with self.assertRaisesRegex(ValueError, 'credentials'):
                await self.worker.execute(f'fetch-image {url}', session_id='session-a')

        self.assertEqual(self.requests, [])

    async def test_rejects_non_http_redirect_without_following_it(self):
        with self.allow_private_images():
            with self.assertRaisesRegex(ValueError, 'redirect.*http'):
                await self.worker.execute(
                    f'fetch-image {self.local_url("/redirect-file")}',
                    session_id='session-a',
                )

        self.assertEqual(self.requests, ['/redirect-file'])

    async def test_rejects_private_redirect_before_following_it(self):
        loop = asyncio.get_running_loop()
        real_open_connection = asyncio.open_connection

        async def mapped_getaddrinfo(host, port, **_kwargs):
            address = '93.184.216.34' if host == 'public.test' else '127.0.0.1'
            return [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (address, port))
            ]

        async def route_public_fixture(_host, port, **kwargs):
            return await real_open_connection('127.0.0.1', port, **kwargs)

        public_url = f'http://public.test:{self.server.server_port}/redirect-private'
        with patch.object(loop, 'getaddrinfo', new=mapped_getaddrinfo):
            with patch('worker.asyncio.open_connection', new=route_public_fixture):
                with self.assertRaisesRegex(ValueError, 'non-global address'):
                    await self.worker.execute(
                        f'fetch-image {public_url}', session_id='session-a'
                    )

        self.assertEqual(self.requests, ['/redirect-private'])

    async def test_fetches_image_without_open_browser_page_and_returns_sendable_path(self):
        with self.allow_private_images():
            result = await self.worker.execute(
                f'fetch-image {self.local_url()}', session_id='session-a'
            )

        image_path = Path(result['imagePath'])
        self.assertTrue(image_path.is_file())
        self.assertEqual(image_path.read_bytes(), self.png_bytes)
        self.assertEqual(result['mimeType'], 'image/png')
        self.assertEqual(result['width'], 3)
        self.assertEqual(result['height'], 2)
        self.assertIn(f'[[image: {image_path}]]', result['text'])

    async def test_rejects_non_image_content_without_saving_a_file(self):
        with self.allow_private_images():
            with self.assertRaisesRegex(ValueError, 'not a valid image'):
                await self.worker.execute(
                    f'fetch-image {self.local_url("/not-image")}',
                    session_id='session-a',
                )

        self.assertEqual(self.worker.list_downloads(session_id='session-a'), [])

    async def test_enforces_configured_byte_limit_before_saving(self):
        with self.allow_private_images(PI_NODRIVER_IMAGE_MAX_BYTES=10):
            with self.assertRaisesRegex(ValueError, 'exceeds the 10 byte limit'):
                await self.worker.execute(
                    f'fetch-image {self.local_url("/large.png")}',
                    session_id='session-a',
                )

        self.assertEqual(self.worker.list_downloads(session_id='session-a'), [])

    async def test_enforces_total_fetch_deadline_while_reading(self):
        with self.allow_private_images(PI_NODRIVER_IMAGE_FETCH_TIMEOUT=0.05):
            with self.assertRaisesRegex(ValueError, 'fetch deadline'):
                await self.worker.execute(
                    f'fetch-image {self.local_url("/deadline.png")}',
                    session_id='session-a',
                )

        self.assertEqual(self.worker.list_downloads(session_id='session-a'), [])

    async def test_total_deadline_stops_slow_status_and_headers_promptly(self):
        for path in ('/slow-status.png', '/slow-header.png'):
            with self.subTest(path=path):
                started = time.monotonic()
                with self.allow_private_images(PI_NODRIVER_IMAGE_FETCH_TIMEOUT=0.05):
                    with self.assertRaisesRegex(ValueError, 'fetch deadline'):
                        await self.worker.execute(
                            f'fetch-image {self.local_url(path)}',
                            session_id='session-a',
                        )
                elapsed = time.monotonic() - started

                self.assertLess(elapsed, 0.3)
                self.assertEqual(self.worker.list_downloads(session_id='session-a'), [])

    async def test_total_deadline_stops_a_slow_drip_body_promptly(self):
        started = time.monotonic()
        with self.allow_private_images(PI_NODRIVER_IMAGE_FETCH_TIMEOUT=0.05):
            with self.assertRaisesRegex(ValueError, 'fetch deadline'):
                await self.worker.execute(
                    f'fetch-image {self.local_url("/slow-drip.png")}',
                    session_id='session-a',
                )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.3)
        self.assertEqual(self.worker.list_downloads(session_id='session-a'), [])

    async def test_content_length_chunked_and_eof_bodies_succeed(self):
        for path in ('/sample.png', '/chunked.png', '/eof.png'):
            with self.subTest(path=path):
                with self.allow_private_images():
                    result = await self.worker.execute(
                        f'fetch-image {self.local_url(path)}', session_id='session-a'
                    )
                self.assertEqual(Path(result['imagePath']).read_bytes(), self.png_bytes)

    async def test_rejects_malformed_or_conflicting_response_framing(self):
        cases = {
            '/conflicting-length.png': 'conflicting Content-Length',
            '/malformed-length.png': 'malformed Content-Length',
            '/conflicting-framing.png': 'conflicting response framing',
            '/bad-chunk.png': 'malformed chunked response',
        }
        for path, message in cases.items():
            with self.subTest(path=path):
                with self.allow_private_images():
                    with self.assertRaisesRegex(ValueError, message):
                        await self.worker.execute(
                            f'fetch-image {self.local_url(path)}', session_id='session-a'
                        )
        self.assertEqual(self.worker.list_downloads(session_id='session-a'), [])

    async def test_rejects_unsupported_content_encoding(self):
        with self.allow_private_images():
            with self.assertRaisesRegex(ValueError, 'unsupported Content-Encoding'):
                await self.worker.execute(
                    f'fetch-image {self.local_url("/encoded.png")}', session_id='session-a'
                )

    async def test_manual_redirect_success_and_limit(self):
        with self.allow_private_images():
            result = await self.worker.execute(
                f'fetch-image {self.local_url("/redirect-ok")}', session_id='session-a'
            )
        self.assertEqual(Path(result['imagePath']).read_bytes(), self.png_bytes)
        self.assertTrue(result['url'].endswith('/sample.png'))

        self.requests.clear()
        with self.allow_private_images():
            with self.assertRaisesRegex(ValueError, 'exceeded the 3 redirect limit'):
                await self.worker.execute(
                    f'fetch-image {self.local_url("/redirect-loop/0")}',
                    session_id='session-a',
                )
        self.assertEqual(
            self.requests,
            ['/redirect-loop/0', '/redirect-loop/1', '/redirect-loop/2', '/redirect-loop/3'],
        )

    async def test_dns_is_used_once_and_environment_proxies_are_ignored(self):
        loop = asyncio.get_running_loop()
        dns_calls = []

        async def mapped_getaddrinfo(host, port, **kwargs):
            dns_calls.append((host, port, kwargs))
            return [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', port))
            ]

        proxy_env = {
            'http_proxy': 'http://127.0.0.1:1',
            'HTTP_PROXY': 'http://127.0.0.1:1',
            'https_proxy': 'http://127.0.0.1:1',
            'HTTPS_PROXY': 'http://127.0.0.1:1',
            'all_proxy': 'http://127.0.0.1:1',
            'ALL_PROXY': 'http://127.0.0.1:1',
            'no_proxy': '',
            'NO_PROXY': '',
        }
        with patch.object(loop, 'getaddrinfo', new=mapped_getaddrinfo):
            with self.allow_private_images(**proxy_env):
                result = await self.worker.execute(
                    f'fetch-image http://image.test:{self.server.server_port}/sample.png',
                    session_id='session-a',
                )

        self.assertEqual(Path(result['imagePath']).read_bytes(), self.png_bytes)
        self.assertEqual([(host, port) for host, port, _ in dns_calls], [
            ('image.test', self.server.server_port)
        ])

    async def test_connection_uses_only_validated_numeric_address(self):
        loop = asyncio.get_running_loop()
        real_open_connection = asyncio.open_connection
        connect_calls = []

        async def mapped_getaddrinfo(host, port, **kwargs):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', port))
            ]

        async def open_connection_spy(host, port, **kwargs):
            connect_calls.append((host, port, kwargs))
            return await real_open_connection(host, port, **kwargs)

        with patch.object(loop, 'getaddrinfo', new=mapped_getaddrinfo):
            with patch('worker.asyncio.open_connection', new=open_connection_spy):
                with self.allow_private_images():
                    result = await self.worker.execute(
                        f'fetch-image http://pin.test:{self.server.server_port}/sample.png',
                        session_id='session-a',
                    )

        self.assertEqual(Path(result['imagePath']).read_bytes(), self.png_bytes)
        self.assertTrue(connect_calls)
        for host, port, kwargs in connect_calls:
            self.assertEqual(host, '127.0.0.1')
            self.assertEqual(port, self.server.server_port)
            self.assertEqual(kwargs['family'], socket.AF_INET)
            self.assertEqual(kwargs['flags'], socket.AI_NUMERICHOST)

    async def test_async_dns_delay_obeys_absolute_deadline_promptly(self):
        loop = asyncio.get_running_loop()
        resolver_started = asyncio.Event()

        async def delayed_getaddrinfo(_host, _port, **_kwargs):
            resolver_started.set()
            await asyncio.sleep(10)
            return []

        started = time.monotonic()
        with patch.object(loop, 'getaddrinfo', new=delayed_getaddrinfo):
            with self.allow_private_images(PI_NODRIVER_IMAGE_FETCH_TIMEOUT=0.05):
                with self.assertRaisesRegex(ValueError, 'fetch deadline'):
                    await self.worker.execute(
                        'fetch-image http://slow-dns.test/sample.png', session_id='session-a'
                    )
        elapsed = time.monotonic() - started

        self.assertTrue(resolver_started.is_set())
        self.assertLess(elapsed, 0.3)
        self.assertEqual(self.worker.list_downloads(session_id='session-a'), [])

    async def test_cancellation_during_async_dns_is_prompt(self):
        loop = asyncio.get_running_loop()
        resolver_started = asyncio.Event()

        async def delayed_getaddrinfo(_host, _port, **_kwargs):
            resolver_started.set()
            await asyncio.sleep(10)
            return []

        with patch.object(loop, 'getaddrinfo', new=delayed_getaddrinfo):
            with self.allow_private_images(PI_NODRIVER_IMAGE_FETCH_TIMEOUT=5):
                task = asyncio.create_task(self.worker.execute(
                    'fetch-image http://cancel-dns.test/sample.png', session_id='session-a'
                ))
                await asyncio.wait_for(resolver_started.wait(), timeout=0.2)
                started = time.monotonic()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.3)
        self.assertEqual(self.worker.list_downloads(session_id='session-a'), [])

    async def test_rejects_excessive_image_width_before_saving(self):
        with self.allow_private_images(PI_NODRIVER_IMAGE_MAX_WIDTH=2):
            with self.assertRaisesRegex(ValueError, 'width.*2'):
                await self.worker.execute(
                    f'fetch-image {self.local_url()}', session_id='session-a'
                )

    async def test_rejects_excessive_cumulative_frame_pixels_before_saving(self):
        with self.allow_private_images(PI_NODRIVER_IMAGE_MAX_TOTAL_PIXELS=11):
            with self.assertRaisesRegex(ValueError, 'cumulative frame pixels.*11'):
                await self.worker.execute(
                    f'fetch-image {self.local_url("/animated.gif")}',
                    session_id='session-a',
                )

    async def test_rejects_excessive_frame_count_before_saving(self):
        with self.allow_private_images(PI_NODRIVER_IMAGE_MAX_FRAMES=1):
            with self.assertRaisesRegex(ValueError, 'more than 1 frame'):
                await self.worker.execute(
                    f'fetch-image {self.local_url("/animated.gif")}',
                    session_id='session-a',
                )

    async def test_rejects_truncated_image_containers_before_saving(self):
        for path in ('/truncated.jpg', '/truncated.png', '/truncated.gif'):
            with self.subTest(path=path):
                with self.allow_private_images():
                    with self.assertRaisesRegex(ValueError, 'not a valid image'):
                        await self.worker.execute(
                            f'fetch-image {self.local_url(path)}',
                            session_id='session-a',
                        )

        self.assertEqual(self.worker.list_downloads(session_id='session-a'), [])

    async def test_canonicalizes_terminator_restored_jpeg_and_gif(self):
        from PIL import Image

        malformed = {
            '/restored.jpg': self.terminator_restored_jpeg_bytes,
            '/restored.gif': self.terminator_restored_gif_bytes,
            '/duplicate-ihdr.png': self.duplicate_ihdr_png_bytes,
            '/duplicate-vp8.webp': self.duplicate_webp_bytes,
        }
        for path, original_bytes in malformed.items():
            with self.subTest(path=path):
                with self.allow_private_images():
                    result = await self.worker.execute(
                        f'fetch-image {self.local_url(path)}', session_id='session-a'
                    )
                output = Path(result['imagePath']).read_bytes()
                self.assertNotEqual(output, original_bytes)
                with Image.open(io.BytesIO(output)) as image:
                    for frame_index in range(getattr(image, 'n_frames', 1)):
                        image.seek(frame_index)
                        image.load()

    async def test_existing_filename_is_not_overwritten(self):
        destination_dir = self.worker.session_download_dir('session-a')
        existing = destination_dir / 'sample.png'
        existing.write_bytes(b'keep me')

        with self.allow_private_images():
            result = await self.worker.execute(
                f'fetch-image {self.local_url()}', session_id='session-a'
            )

        self.assertEqual(existing.read_bytes(), b'keep me')
        self.assertNotEqual(Path(result['imagePath']), existing)
        self.assertEqual(Path(result['imagePath']).read_bytes(), self.png_bytes)

    async def test_cancelled_decodes_keep_their_global_decode_slots_until_threads_finish(self):
        active = 0
        maximum_active = 0
        lock = threading.Lock()

        def slow_decode(_data, _limits):
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                time.sleep(0.1)
                return 'image/png', '.png', 3, 2, self.png_bytes
            finally:
                with lock:
                    active -= 1

        self.worker.inspect_and_load_image = slow_decode
        tasks = [
            asyncio.create_task(self.worker.decode_fetched_image(self.png_bytes, {}))
            for _ in range(12)
        ]
        await asyncio.sleep(0.02)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0.15)

        self.assertEqual(maximum_active, 4)

    async def test_global_fetch_concurrency_is_bounded_but_still_parallel(self):
        output = Path(self.temp_dir.name) / 'bounded.png'
        output.write_bytes(self.png_bytes)
        active = 0
        maximum_active = 0

        async def fake_fetch(url, _session_id):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            try:
                await asyncio.sleep(0.05)
                return output, 'image/png', 3, 2, url
            finally:
                active -= 1

        self.worker.run_fetch_image = fake_fetch
        await asyncio.gather(*(
            self.worker.execute(
                f'fetch-image https://example.test/{index}.png',
                session_id=f'session-{index}',
            )
            for index in range(12)
        ))

        self.assertEqual(maximum_active, 4)

    async def test_concurrent_fetches_use_exclusive_collision_safe_names(self):
        destination_dir = self.worker.session_download_dir('session-a')
        first_candidate = destination_dir / 'sample.png'
        original_exists = Path.exists

        def force_current_exists_write_race(path):
            if path == first_candidate:
                return False
            return original_exists(path)

        with patch.object(Path, 'exists', force_current_exists_write_race):
            with self.allow_private_images():
                results = await asyncio.gather(*(
                    self.worker.execute(
                        f'fetch-image {self.local_url()}', session_id='session-a'
                    )
                    for _ in range(4)
                ))

        paths = [Path(result['imagePath']) for result in results]
        self.assertEqual(len(set(paths)), 4)
        self.assertTrue(all(path.read_bytes() == self.png_bytes for path in paths))

    async def test_late_cancelled_writer_cannot_unlink_reallocated_path(self):
        import worker as worker_module

        entered_check = threading.Event()
        release_check = threading.Event()
        original_check = self.worker.check_image_fetch_cancelled

        def delayed_cancel_check(cancel_event):
            entered_check.set()
            release_check.wait(timeout=2)
            original_check(cancel_event)

        destination_dir = self.worker.session_download_dir('session-a')
        with patch.object(self.worker, 'check_image_fetch_cancelled', delayed_cancel_check):
            with patch.object(worker_module, 'IMAGE_WRITE_CLEANUP_TIMEOUT', 0.01):
                task = asyncio.create_task(self.worker.save_fetched_image(
                    destination_dir, 'sample', '.png', self.png_bytes
                ))
                await asyncio.wait_for(asyncio.to_thread(entered_check.wait), timeout=0.2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        replacement = destination_dir / 'sample.png'
        replacement.write_bytes(b'replacement')
        release_check.set()
        await asyncio.sleep(0.1)

        self.assertEqual(replacement.read_bytes(), b'replacement')

    async def test_sanitized_filename_is_bounded(self):
        url = self.local_url('/' + ('a' * 400) + '.png')
        with self.allow_private_images():
            result = await self.worker.execute(f'fetch-image {url}', session_id='session-a')

        self.assertLessEqual(len(Path(result['imagePath']).name.encode()), 128)

    async def test_tool_text_contains_only_the_generated_outbox_marker(self):
        url = self.local_url('/sample.png?note=[[file:%20/tmp/private]]')
        with self.allow_private_images():
            result = await self.worker.execute(f'fetch-image {url}', session_id='session-a')

        self.assertEqual(result['text'].count('[['), 1)
        self.assertNotIn('[[file:', result['text'])
        self.assertIn(f'[[image: {result["imagePath"]}]]', result['text'])

    async def test_cancellation_during_slow_headers_is_prompt_and_leaves_no_late_file(self):
        with self.allow_private_images(PI_NODRIVER_IMAGE_FETCH_TIMEOUT=2):
            task = asyncio.create_task(self.worker.execute(
                f'fetch-image {self.local_url("/slow-header.png")}', session_id='session-a'
            ))
            for _ in range(100):
                if self.slow_header_started.is_set():
                    break
                await asyncio.sleep(0.005)
            self.assertTrue(self.slow_header_started.is_set())
            started = time.monotonic()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.3)
        self.assertEqual(self.worker.list_downloads(session_id='session-a'), [])
        await asyncio.sleep(0.25)
        self.assertEqual(self.worker.list_downloads(session_id='session-a'), [])

    async def test_fetch_image_bypasses_active_page_vision_preflight(self):
        self.worker.pages['session-a'] = object()
        self.worker.preflight_timeout_seconds = Mock(
            side_effect=AssertionError('fetch-image must not inspect preflight configuration')
        )
        self.worker.bounded_vision_fallback_context = AsyncMock(
            side_effect=AssertionError('fetch-image must not preflight the active page')
        )

        with self.allow_private_images():
            result = await self.worker.execute(
                f'fetch-image {self.local_url()}', session_id='session-a'
            )

        self.assertEqual(Path(result['imagePath']).read_bytes(), self.png_bytes)
        self.worker.preflight_timeout_seconds.assert_not_called()
        self.worker.bounded_vision_fallback_context.assert_not_awaited()

    async def test_fetch_image_daemon_request_bypasses_preflight_configuration(self):
        from worker import execute_request

        self.worker.pages['session-a'] = object()
        self.worker.preflight_timeout_seconds = Mock(
            side_effect=AssertionError('fetch-image must not inspect preflight configuration')
        )
        self.worker.begin_session_action = Mock(
            side_effect=AssertionError('fetch-image must not account against an active page')
        )
        with self.allow_private_images():
            result = await execute_request(self.worker, {
                'id': 1,
                'sessionId': 'session-a',
                'command': f'fetch-image {self.local_url()}',
            })

        self.assertTrue(result['ok'])
        self.assertEqual(Path(result['imagePath']).read_bytes(), self.png_bytes)
        self.worker.preflight_timeout_seconds.assert_not_called()

    async def test_keeps_fetched_images_isolated_by_pi_session(self):
        with self.allow_private_images():
            first = await self.worker.execute(
                f'fetch-image {self.local_url()}', session_id='session-a'
            )
            second = await self.worker.execute(
                f'fetch-image {self.local_url()}', session_id='session-b'
            )

        first_path = Path(first['imagePath'])
        second_path = Path(second['imagePath'])
        self.assertNotEqual(first_path.parent, second_path.parent)
        self.assertEqual(first_path.read_bytes(), second_path.read_bytes())

    async def test_rejects_non_http_image_urls_and_port_zero(self):
        with self.assertRaisesRegex(ValueError, 'http or https'):
            await self.worker.execute('fetch-image file:///tmp/private.png', session_id='session-a')
        with self.assertRaisesRegex(ValueError, 'port zero'):
            await self.worker.execute('fetch-image http://example.com:0/image.png', session_id='session-a')


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

    def test_get_text_and_crawl_expose_ranked_image_candidates_from_rendered_dom(self):
        handler = functools.partial(
            QuietSimpleHTTPRequestHandler,
            directory=str(ROOT / 'tests'),
        )
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            fixture_url = f'http://127.0.0.1:{server.server_port}/fixture_images.html'
            self.command(f'open {fixture_url}')

            current = self.command('get text')
            self.assertIn('Product specifications and availability.', current['text'])
            self.assertIn('Images found:', current['text'])
            self.assertGreaterEqual(current['imageCount'], 3)
            current_urls = [candidate['url'] for candidate in current['imageCandidates']]
            self.assertTrue(any(url.endswith('/images/product-hero-secure.jpg') for url in current_urls))
            self.assertFalse(any(url.endswith('/images/product-hero.jpg') for url in current_urls))
            self.assertFalse(any(url.endswith('/images/twitter-only.jpg') for url in current_urls))
            self.assertLess(
                next(index for index, url in enumerate(current_urls) if url.endswith('/images/product-hero-secure.jpg')),
                next(index for index, url in enumerate(current_urls) if url.endswith('/images/product-secondary.jpg')),
            )
            hero = next(
                candidate for candidate in current['imageCandidates']
                if candidate['url'].endswith('/images/product-hero-secure.jpg')
            )
            self.assertEqual((hero['width'], hero['height']), (1200, 800))
            self.assertEqual(hero['alt'], 'DGX fixture hero')
            self.assertTrue(any(url.endswith('/images/product-main.jpg') for url in current_urls))
            self.assertTrue(any(url.endswith('/images/product-side.jpg') for url in current_urls))
            self.assertTrue(any(url.endswith('/images/product-back.jpg') for url in current_urls))
            self.assertTrue(any(url.endswith('/images/product-top.jpg') for url in current_urls))
            self.assertFalse(any('opaque-loading-resource' in url for url in current_urls))
            self.assertFalse(any('tiny.svg' in url for url in current_urls))
            self.assertFalse(any('tracking' in url for url in current_urls))
            self.assertFalse(any('placeholder' in url for url in current_urls))
            self.assertFalse(any('tracking-pixel' in url for url in current_urls))
            self.assertFalse(any('brand-logo' in url for url in current_urls))
            self.assertEqual(len(current_urls), len(set(current_urls)))

            images_only = self.command('get images')
            self.assertIn('metadata only; not downloaded', images_only['text'])
            self.assertEqual(images_only['imageCount'], current['imageCount'])

            crawled = self.command(f'crawl {fixture_url}')
            self.assertEqual(crawled['successCount'], 1)
            self.assertGreaterEqual(crawled['imageCount'], 3)
            self.assertIn('Images found:', crawled['text'])
            self.assertGreaterEqual(len(crawled['results'][0]['imageCandidates']), 3)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

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

    def open_form_safety_fixture(self):
        fixture_url = (ROOT / 'tests/fixture_form_safety.html').as_uri()
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

        self.command('scroll down 1000')
        middle_snapshot = self.command('snapshot -i')['text']
        self.assertNotIn('Top viewport action', middle_snapshot)
        self.assertIn('Middle viewport action', middle_snapshot)
        self.assertNotIn('Bottom viewport action', middle_snapshot)
        middle_ref = next(line for line in middle_snapshot.splitlines() if 'Middle viewport action' in line).split()[0]
        self.command(f'click {middle_ref}')
        self.assertIn('middle-clicked', self.command('get text')['text'])

        self.command('scroll down 1000')
        bottom_snapshot = self.command('snapshot -i')['text']
        self.assertNotIn('Top viewport action', bottom_snapshot)
        self.assertNotIn('Middle viewport action', bottom_snapshot)
        self.assertIn('Bottom viewport action', bottom_snapshot)
        bottom_ref = next(line for line in bottom_snapshot.splitlines() if 'Bottom viewport action' in line).split()[0]
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

    def test_fill_rejects_a_label_ref_without_mutating_the_focused_input(self):
        self.open_form_safety_fixture()
        snapshot = self.command('snapshot -i')['text']
        phone_ref = next(
            line for line in snapshot.splitlines()
            if '<input>' in line and 'Phone number' in line
        ).split()[0]
        label_ref = next(
            line for line in snapshot.splitlines()
            if '<label>' in line and 'Profile name' in line
        ).split()[0]
        self.command(f'fill {phone_ref} 0000000000')

        rejected = self.command_raw(f'fill {label_ref} "Pi Qwen"')

        self.assertFalse(rejected['ok'])
        self.assertIn('not text-editable', rejected['error'])
        updated = self.command('snapshot -i')['text']
        phone_line = next(line for line in updated.splitlines() if 'Phone number' in line)
        self.assertIn('"0000000000"', phone_line)
        self.assertNotIn('Pi Qwen', phone_line)

    def test_password_fill_and_snapshot_do_not_echo_the_secret(self):
        self.open_form_safety_fixture()
        snapshot = self.command('snapshot -i')['text']
        password_ref = next(
            line for line in snapshot.splitlines()
            if '<input>' in line and 'placeholder="Password"' in line
        ).split()[0]
        secret = 'private-test-password'

        filled = self.command(f'fill {password_ref} "{secret}"')
        updated = self.command('snapshot -i')['text']
        clicked = self.command(f'click {password_ref}')
        js_clicked = self.command(f'click-js {password_ref}')

        self.assertNotIn(secret, filled['text'])
        self.assertNotIn(secret, updated)
        self.assertNotIn(secret, clicked['text'])
        self.assertNotIn(secret, js_clicked['text'])
        password_line = next(
            line for line in updated.splitlines() if 'placeholder="Password"' in line
        )
        self.assertIn('value-set="true"', password_line)

    def test_password_redaction_survives_page_changing_the_input_type(self):
        self.open_form_safety_fixture()
        snapshot = self.command('snapshot -i')['text']
        password_ref = next(
            line for line in snapshot.splitlines()
            if '<input>' in line and 'placeholder="Mutating password"' in line
        ).split()[0]
        secret = 'dynamic-private-test-password'

        filled = self.command(f'fill {password_ref} "{secret}"')
        updated = self.command('snapshot -i')['text']
        clicked = self.command(f'click {password_ref}')
        js_clicked = self.command(f'click-js {password_ref}')

        for output in (filled['text'], updated, clicked['text'], js_clicked['text']):
            self.assertNotIn(secret, output)
        password_line = next(
            line for line in updated.splitlines()
            if 'placeholder="Mutating password"' in line
        )
        self.assertIn('value-set="true"', password_line)

    def test_password_redaction_survives_handler_replacing_the_input_node(self):
        self.open_form_safety_fixture()
        snapshot = self.command('snapshot -i')['text']
        password_ref = next(
            line for line in snapshot.splitlines()
            if '<input>' in line and 'placeholder="Replacement password"' in line
        ).split()[0]
        secret = 'replacement-private-test-password'

        filled = self.command(f'fill {password_ref} "{secret}"')
        updated = self.command('snapshot -i')['text']

        self.assertNotIn(secret, filled['text'])
        self.assertNotIn(secret, updated)
        replacement_line = next(
            line for line in updated.splitlines()
            if 'placeholder="Replacement password"' in line
        )
        self.assertIn('value-set="true"', replacement_line)

    def test_password_redaction_survives_click_handler_changing_prefilled_input_type(self):
        self.open_form_safety_fixture()
        snapshot = self.command('snapshot -i')['text']
        password_ref = next(
            line for line in snapshot.splitlines()
            if '<input>' in line and 'placeholder="Click-mutating password"' in line
        ).split()[0]
        secret = 'prefilled-private-test-password'

        clicked = self.command(f'click {password_ref}')
        updated = self.command('snapshot -i')['text']

        self.assertNotIn(secret, snapshot)
        self.assertNotIn(secret, clicked['text'])
        self.assertNotIn(secret, updated)
        password_line = next(
            line for line in updated.splitlines()
            if 'placeholder="Click-mutating password"' in line
        )
        self.assertIn('value-set="true"', password_line)

    def test_click_text_never_matches_a_password_value(self):
        self.open_form_safety_fixture()

        rejected = self.command_raw('click-text "prefilled-private-test-password"')

        self.assertFalse(rejected['ok'])
        self.assertIn('click target not found', rejected['error'])
        updated = self.command('snapshot -i')['text']
        password_line = next(
            line for line in updated.splitlines()
            if 'placeholder="Click-mutating password"' in line
        )
        self.assertIn('type="password"', password_line)

    def test_dynamic_editability_and_canceled_input_events_are_honored(self):
        self.open_form_safety_fixture()
        snapshot = self.command('snapshot -i')['text']
        refs = {
            label: next(
                line for line in snapshot.splitlines()
                if '<input>' in line and label in line
            ).split()[0]
            for label in ('Focus-lock input', 'Keydown-block input', 'Beforeinput-block input')
        }

        focus_rejected = self.command_raw(f'fill {refs["Focus-lock input"]} mutation')
        keydown_result = self.command(f'fill {refs["Keydown-block input"]} mutation')
        beforeinput_result = self.command(f'fill {refs["Beforeinput-block input"]} mutation')

        self.assertFalse(focus_rejected['ok'])
        self.assertIn('not text-editable', focus_rejected['error'])
        self.assertIn('with ""', keydown_result['text'])
        self.assertIn('"beforeinput original"', beforeinput_result['text'])
        self.assertNotIn('mutation', keydown_result['text'])
        self.assertNotIn('mutation', beforeinput_result['text'])

        fresh = self.command('snapshot -i')['text']
        clear_ref = next(
            line for line in fresh.splitlines()
            if '<input>' in line and 'Clear-block input' in line
        ).split()[0]
        clear_result = self.command(f'fill {clear_ref} mutation')
        self.assertIn('"clear original"', clear_result['text'])
        self.assertNotIn('mutation', clear_result['text'])

        fresh = self.command('snapshot -i')['text']
        partial_ref = next(
            line for line in fresh.splitlines()
            if '<input>' in line and 'Partial-lock input' in line
        ).split()[0]
        partial = self.command_raw(f'fill {partial_ref} mutation')
        self.assertFalse(partial['ok'])
        self.assertIn('not text-editable', partial['error'])
        restored = self.command('snapshot -i')['text']
        partial_line = next(line for line in restored.splitlines() if 'Partial-lock input' in line)
        self.assertIn('"partial original"', partial_line)
        self.assertNotIn('mutation', partial_line)

    def test_fill_submit_does_not_submit_canceled_or_partial_text(self):
        self.open_form_safety_fixture()
        snapshot = self.command('snapshot -i')['text']
        keydown_ref = next(
            line for line in snapshot.splitlines()
            if '<input>' in line and 'Keydown-block input' in line
        ).split()[0]
        enter_ref = next(
            line for line in snapshot.splitlines()
            if '<input>' in line and 'Enter-block input' in line
        ).split()[0]
        partial_ref = next(
            line for line in snapshot.splitlines()
            if '<input>' in line and 'Partial-cancel input' in line
        ).split()[0]
        clear_ref = next(
            line for line in snapshot.splitlines()
            if '<input>' in line and 'Clear-block input' in line
        ).split()[0]
        sanitizing_ref = next(
            line for line in snapshot.splitlines()
            if '<input>' in line and 'Sanitizing input' in line
        ).split()[0]
        change_ref = next(
            line for line in snapshot.splitlines()
            if '<input>' in line and 'Change-mutating input' in line
        ).split()[0]
        enter_mutating_ref = next(
            line for line in snapshot.splitlines()
            if '<input>' in line and 'Enter-mutating input' in line
        ).split()[0]

        canceled_text = self.command_raw(f'fill-submit {keydown_ref} mutation')
        canceled_enter = self.command_raw(f'fill-submit {enter_ref} accepted')
        partial_text = self.command_raw(f'fill-submit {partial_ref} mutation')
        clear_blocked_text = self.command_raw(f'fill-submit {clear_ref} mutation')
        sanitized_text = self.command_raw(f'fill-submit {sanitizing_ref} mutation')
        changed_text = self.command_raw(f'fill-submit {change_ref} accepted')
        enter_mutated_text = self.command_raw(
            f'fill-submit {enter_mutating_ref} accepted'
        )

        self.assertFalse(canceled_text['ok'])
        self.assertIn('canceled', canceled_text['error'])
        self.assertFalse(canceled_enter['ok'])
        self.assertIn('canceled', canceled_enter['error'])
        self.assertFalse(partial_text['ok'])
        self.assertIn('canceled', partial_text['error'])
        self.assertFalse(clear_blocked_text['ok'])
        self.assertIn('canceled', clear_blocked_text['error'])
        self.assertFalse(sanitized_text['ok'])
        self.assertIn('not text-editable', sanitized_text['error'])
        self.assertFalse(changed_text['ok'])
        self.assertIn('not text-editable', changed_text['error'])
        self.assertFalse(enter_mutated_text['ok'])
        self.assertIn('changed before submission', enter_mutated_text['error'])
        self.assertIn('idle', self.status())

    def test_fill_submit_requires_an_associated_working_form(self):
        self.open_form_safety_fixture()
        snapshot = self.command('snapshot -i')['text']
        phone_ref = next(
            line for line in snapshot.splitlines()
            if '<input>' in line and 'Phone number' in line
        ).split()[0]
        throwing_ref = next(
            line for line in snapshot.splitlines()
            if '<input>' in line and 'Throwing submit input' in line
        ).split()[0]

        no_form = self.command_raw(f'fill-submit {phone_ref} accepted')
        throwing_form = self.command_raw(f'fill-submit {throwing_ref} accepted')

        self.assertFalse(no_form['ok'])
        self.assertIn('associated form', no_form['error'])
        self.assertFalse(throwing_form['ok'])
        self.assertIn('requestSubmit failed', throwing_form['error'])
        self.assertIn('idle', self.status())

    def test_fill_submit_uses_authoritative_form_association_and_validates_constraints(self):
        self.open_form_safety_fixture()
        snapshot = self.command('snapshot -i')['text']
        override_ref = next(
            line for line in snapshot.splitlines()
            if '<input>' in line and 'Override form input' in line
        ).split()[0]
        invalid_ref = next(
            line for line in snapshot.splitlines()
            if '<input>' in line and 'Invalid form input' in line
        ).split()[0]
        handler_ref = next(
            line for line in snapshot.splitlines()
            if '<input>' in line and 'Enter handler input' in line
        ).split()[0]
        unsafe_handler_ref = next(
            line for line in snapshot.splitlines()
            if '<input>' in line and 'Unsafe enter handler input' in line
        ).split()[0]
        late_mutating_ref = next(
            line for line in snapshot.splitlines()
            if '<input>' in line and 'Late-mutating input' in line
        ).split()[0]
        default_submitter_ref = next(
            line for line in snapshot.splitlines()
            if '<input>' in line and 'Default submitter input' in line
        ).split()[0]
        unsupported_target_ref = next(
            line for line in snapshot.splitlines()
            if '<input>' in line and 'Unsupported target input' in line
        ).split()[0]
        base_target_ref = next(
            line for line in snapshot.splitlines()
            if '<input>' in line and 'Base target input' in line
        ).split()[0]
        whitespace_target_ref = next(
            line for line in snapshot.splitlines()
            if '<input>' in line and 'Whitespace target input' in line
        ).split()[0]
        disabled_default_ref = next(
            line for line in snapshot.splitlines()
            if '<input>' in line and 'Disabled default input' in line
        ).split()[0]
        image_default_ref = next(
            line for line in snapshot.splitlines()
            if '<input>' in line and 'Image default input' in line
        ).split()[0]
        shadow_submit_ref = next(
            line for line in snapshot.splitlines()
            if '<input>' in line and 'Shadow submit input' in line
        ).split()[0]

        submitted = self.command(f'fill-submit {override_ref} accepted')
        override_status = self.status()
        invalid = self.command_raw(f'fill-submit {invalid_ref} accepted')
        handler_submitted = self.command(f'fill-submit {handler_ref} accepted')
        handler_status = self.status()
        unsafe_handler = self.command_raw(
            f'fill-submit {unsafe_handler_ref} accepted'
        )
        late_mutating = self.command_raw(
            f'fill-submit {late_mutating_ref} accepted'
        )
        default_submitted = self.command(
            f'fill-submit {default_submitter_ref} accepted'
        )
        default_status = self.status()
        unsupported_target = self.command_raw(
            f'fill-submit {unsupported_target_ref} accepted'
        )
        whitespace_target = self.command_raw(
            f'fill-submit {whitespace_target_ref} accepted'
        )
        disabled_default = self.command_raw(
            f'fill-submit {disabled_default_ref} accepted'
        )
        image_default = self.command(
            f'fill-submit {image_default_ref} accepted'
        )
        image_status = self.status()
        shadow_submitted = self.command(
            f'fill-submit {shadow_submit_ref} accepted'
        )
        shadow_status = self.status()
        base_target = self.command_raw(
            f'fill-submit {base_target_ref} accepted'
        )

        self.assertTrue(submitted['ok'])
        self.assertIn('override-form-submitted', override_status)
        self.assertFalse(invalid['ok'])
        self.assertIn('constraint validation', invalid['error'])
        self.assertTrue(handler_submitted['ok'])
        self.assertIn('handler-submit-count:1', handler_status)
        self.assertNotIn('handler-submit-count:2', handler_status)
        self.assertFalse(unsafe_handler['ok'])
        self.assertIn('form was not submitted', unsafe_handler['error'])
        self.assertFalse(late_mutating['ok'])
        self.assertIn('submission context changed', late_mutating['error'])
        self.assertTrue(default_submitted['ok'])
        self.assertIn('default-submitter:default-submit-button:clicks=1', default_status)
        self.assertFalse(unsupported_target['ok'])
        self.assertIn('unsupported form target', unsupported_target['error'])
        self.assertFalse(whitespace_target['ok'])
        self.assertIn('unsupported form target', whitespace_target['error'])
        self.assertFalse(disabled_default['ok'])
        self.assertIn('disabled default submitter', disabled_default['error'])
        self.assertTrue(image_default['ok'])
        self.assertIn('image-default-submitted:image-default-submitter', image_status)
        self.assertTrue(shadow_submitted['ok'])
        self.assertIn('shadow-submitter:shadow-default-submitter:clicks=1', shadow_status)
        self.assertFalse(base_target['ok'])
        self.assertIn('submission context changed', base_target['error'])
        status = self.status()
        self.assertNotIn('handler-submit-count:2', status)
        self.assertNotIn('invalid-form-submitted', status)
        self.assertNotIn('unsupported-target-submitted', status)

    def test_text_entry_actions_reject_readonly_inputs_and_textareas(self):
        self.open_form_safety_fixture()
        snapshot = self.command('snapshot -i')['text']
        readonly_refs = [
            next(
                line for line in snapshot.splitlines()
                if tag in line and label in line
            ).split()[0]
            for tag, label in (
                ('<input>', 'Readonly input'),
                ('<textarea>', 'Readonly textarea'),
            )
        ]

        for ref in readonly_refs:
            for action in ('fill', 'type', 'fill-submit'):
                with self.subTest(ref=ref, action=action):
                    rejected = self.command_raw(f'{action} {ref} mutation')
                    self.assertFalse(rejected['ok'])
                    self.assertIn('not text-editable', rejected['error'])
        page_text = self.status()
        self.assertIn('idle', page_text)
        self.assertNotIn('mutation', page_text)

    def test_snapshot_exposes_checkbox_state_through_visible_label_proxies(self):
        self.open_form_safety_fixture()

        snapshot = self.command('snapshot -i')['text']

        terms_line = next(line for line in snapshot.splitlines() if 'Required terms' in line)
        marketing_line = next(line for line in snapshot.splitlines() if 'Receive marketing email' in line)
        self.assertIn('control="checkbox"', terms_line)
        self.assertIn('checked="false"', terms_line)
        self.assertIn('required="true"', terms_line)
        self.assertIn('control="checkbox"', marketing_line)
        self.assertIn('checked="true"', marketing_line)
        disabled_line = next(
            line for line in snapshot.splitlines() if 'Disabled preference' in line
        )
        self.assertIn('control="checkbox"', disabled_line)
        self.assertIn('checked="true"', disabled_line)
        self.assertIn('disabled="true"', disabled_line)
        disabled_click = self.command_raw(f'click {disabled_line.split()[0]}')
        self.assertFalse(disabled_click['ok'])
        self.assertIn('disabled', disabled_click['error'])

        self.command(f'click {terms_line.split()[0]}')
        updated = self.command('snapshot -i')['text']
        updated_terms = next(line for line in updated.splitlines() if 'Required terms' in line)
        self.assertIn('checked="true"', updated_terms)

    def test_text_entry_actions_reject_a_contenteditable_label(self):
        self.open_form_safety_fixture()
        snapshot = self.command('snapshot -i')['text']
        label_ref = next(
            line for line in snapshot.splitlines()
            if '<label>' in line and 'Editable label' in line
        ).split()[0]

        for action in ('fill', 'type', 'fill-submit'):
            with self.subTest(action=action):
                rejected = self.command_raw(f'{action} {label_ref} mutation')
                self.assertFalse(rejected['ok'])
                self.assertIn('not text-editable', rejected['error'])
        page_text = self.status()
        self.assertIn('Editable label', page_text)
        self.assertNotIn('mutation', page_text)
        self.assertIn('idle', page_text)

    def test_empty_fill_dispatches_input_to_clear_framework_model_state(self):
        self.open_form_safety_fixture()
        snapshot = self.command('snapshot -i')['text']
        model_ref = next(
            line for line in snapshot.splitlines()
            if '<input>' in line and 'Empty fill model' in line
        ).split()[0]

        cleared = self.command(f'fill {model_ref} ""')

        self.assertIn('with ""', cleared['text'])
        self.assertIn('input-events:1', self.status())

    def test_type_uses_cleared_textarea_live_value_not_html_default(self):
        self.open_form_safety_fixture()
        snapshot = self.command('snapshot -i')['text']
        textarea_ref = next(
            line for line in snapshot.splitlines()
            if '<textarea>' in line and 'Default textarea' in line
        ).split()[0]

        cleared = self.command(f'fill {textarea_ref} ""')
        typed = self.command(f'type {textarea_ref} Z')

        self.assertIn('with ""', cleared['text'])
        self.assertIn('with "Z"', typed['text'])
        self.assertNotIn('server default', typed['text'])

    def test_press_rejects_arbitrary_text_without_mutating_focus(self):
        self.open_form_safety_fixture()
        snapshot = self.command('snapshot -i')['text']
        phone_ref = next(
            line for line in snapshot.splitlines()
            if '<input>' in line and 'Phone number' in line
        ).split()[0]
        self.command(f'fill {phone_ref} 0000000000')

        rejected = self.command_raw('press MUTATE')

        self.assertFalse(rejected['ok'])
        self.assertIn('Enter, Tab, Space, or Backspace', rejected['error'])
        updated = self.command('snapshot -i')['text']
        phone_line = next(line for line in updated.splitlines() if 'Phone number' in line)
        self.assertIn('"0000000000"', phone_line)
        self.assertNotIn('MUTATE', phone_line)

    def test_short_click_text_does_not_substring_match_next(self):
        self.open_form_safety_fixture()

        rejected = self.command_raw('click-text "X"')

        self.assertFalse(rejected['ok'])
        self.assertIn('click target not found', rejected['error'])
        self.assertIn('idle', self.status())

    def test_click_text_rejects_an_empty_query(self):
        self.open_form_safety_fixture()

        rejected = self.command_raw('click-text ""')

        self.assertFalse(rejected['ok'])
        self.assertIn('non-empty', rejected['error'])
        self.assertIn('idle', self.status())

    def test_click_text_rejects_a_javascript_blank_bom_query(self):
        self.open_form_safety_fixture()

        rejected = self.command_raw('click-text "\ufeff"')

        self.assertFalse(rejected['ok'])
        self.assertIn('non-empty', rejected['error'])
        self.assertIn('idle', self.status())

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

    def test_main_frame_same_url_fill_submit_waits_for_new_document(self):
        handler = functools.partial(
            QuietSimpleHTTPRequestHandler,
            directory=str(ROOT / 'tests'),
        )
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            fixture_url = (
                f'http://127.0.0.1:{server.server_port}/fixture_main_same_url_submit.html'
            )
            opened = self.command(f'open {fixture_url}')['text']
            submit_ref = next(
                line for line in opened.splitlines()
                if '<input>' in line and 'Main reload 1' in line
            ).split()[0]

            submitted = self.command(f'fill-submit {submit_ref} accepted')['text']

            self.assertIn('Main reload 2', submitted)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

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

            ajax_ref = next(
                line for line in updated.splitlines()
                if 'Iframe AJAX submit query' in line
            ).split()[0]
            ajax_submitted = self.command(f'fill-submit {ajax_ref} ready')['text']
            self.assertIn('Iframe AJAX submitted', ajax_submitted)

            after_ajax = self.command('snapshot -i')['text']
            same_url_ref = next(
                line for line in after_ajax.splitlines()
                if 'Iframe same URL submit query' in line
            ).split()[0]
            same_url_submitted = self.command(f'fill-submit {same_url_ref} ready')['text']
            self.assertIn('Iframe same URL submit query', same_url_submitted)

            after_same_url = self.command('snapshot -i')['text']
            submit_ref = next(
                line for line in after_same_url.splitlines()
                if 'Iframe submit query' in line
            ).split()[0]
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
        self.assertIn('vision-mark', result['text'])
        self.assertNotIn('Use click <x> <y>', result['text'])
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

    def unlock_vision_fallback(self):
        for count in range(1, 4):
            blocked = self.command_raw('click-css "#pi-nodriver-missing-target"')
            self.assertFalse(blocked['ok'])
            self.assertIn(f'{count}/3', blocked['error'])
        self.assertIn('VISION_FALLBACK_UNLOCKED', blocked['error'])

    def test_vision_correctness_marks_retries_and_confirms_before_coordinate_click(self):
        fixture_url = (ROOT / 'tests/fixture_vision_canvas.html').as_uri()
        self.command(f'open {fixture_url}')
        self.assertEqual(self.command('snapshot -i')['text'], '(no interactive elements)')

        self.command('screenshot')
        locked = self.command_raw('vision-mark 300 330')
        self.assertFalse(locked['ok'])
        self.assertIn('VISION_FALLBACK_LOCKED', locked['error'])
        self.assertIn('0/3', locked['error'])

        raw_click = self.command_raw('click 300 330')
        self.assertFalse(raw_click['ok'])
        self.assertIn('VISION_CLICK_GUARD', raw_click['error'])
        self.assertIn('vision-idle', self.command('get text')['text'])

        still_locked = self.command_raw('vision-mark 300 330')
        self.assertFalse(still_locked['ok'])
        self.assertIn('0/3', still_locked['error'])

        for _ in range(3):
            invalid_css = self.command_raw('click-css "["')
            self.assertFalse(invalid_css['ok'])
            self.assertIn('invalid CSS selector', invalid_css['error'])
            self.assertNotIn('VISION_FALLBACK_PROGRESS', invalid_css['error'])
        still_locked = self.command_raw('vision-mark 300 330')
        self.assertIn('0/3', still_locked['error'])

        self.unlock_vision_fallback()
        clean = self.command('screenshot')
        clean_bytes = Path(clean['screenshotPath']).read_bytes()
        wrong = self.command('vision-mark 60 180')
        self.assertEqual(wrong['action'], 'vision-mark')
        self.assertTrue(Path(wrong['screenshotPath']).is_file())
        self.assertNotEqual(clean_bytes, Path(wrong['screenshotPath']).read_bytes())
        self.assertIn('NO CLICK PERFORMED', wrong['text'])
        self.assertIn('vision-idle', self.command('get text')['text'])

        corrected = self.command('vision-mark 300 330')
        self.assertNotEqual(wrong['previewToken'], corrected['previewToken'])
        stale_confirmation = self.command_raw(f"vision-click {wrong['previewToken']}")
        self.assertFalse(stale_confirmation['ok'])
        self.assertIn('current marked preview', stale_confirmation['error'])

        clicked = self.command(f"vision-click {corrected['previewToken']}")
        self.assertEqual(clicked['action'], 'vision-click')
        self.assertIn('vision-clicked', self.command('get text')['text'])

        reused = self.command_raw(f"vision-click {corrected['previewToken']}")
        self.assertFalse(reused['ok'])
        self.assertIn('current marked preview', reused['error'])

    def test_successful_semantic_click_locks_vision_fallback_again(self):
        self.open_fixture()
        self.unlock_vision_fallback()
        self.command('click-css "#custom"')
        self.command('screenshot')

        blocked = self.command_raw('vision-mark 300 330')

        self.assertFalse(blocked['ok'])
        self.assertIn('VISION_FALLBACK_LOCKED', blocked['error'])
        self.assertIn('0/3', blocked['error'])

    def test_full_page_images_invalidate_existing_viewport_marker(self):
        fixture_url = (ROOT / 'tests/fixture_vision_canvas.html').as_uri()
        self.command(f'open {fixture_url}')
        self.unlock_vision_fallback()
        self.command('screenshot')
        first = self.command('vision-mark 60 180')

        self.command('snapshot -i --full')
        snapshot_blocked = self.command_raw(f"vision-click {first['previewToken']}")
        self.assertFalse(snapshot_blocked['ok'])
        self.assertIn('current marked preview', snapshot_blocked['error'])

        self.command('screenshot')
        second = self.command('vision-mark 60 180')
        self.command('screenshot --full')
        screenshot_blocked = self.command_raw(f"vision-click {second['previewToken']}")
        marker_blocked = self.command_raw('vision-mark 300 330')

        self.assertFalse(screenshot_blocked['ok'])
        self.assertIn('current marked preview', screenshot_blocked['error'])
        self.assertFalse(marker_blocked['ok'])
        self.assertIn('VISION_SCREENSHOT_REQUIRED', marker_blocked['error'])

    def test_invalid_css_on_hidden_empty_document_does_not_unlock_vision_fallback(self):
        fixture_url = (ROOT / 'tests/fixture_hidden_empty.html').as_uri()
        self.command(f'open {fixture_url}')
        for _ in range(3):
            invalid_css = self.command_raw('click-css "["')
            self.assertFalse(invalid_css['ok'])
            self.assertIn('invalid CSS selector', invalid_css['error'])
            self.assertNotIn('VISION_FALLBACK_PROGRESS', invalid_css['error'])
        self.command('screenshot')
        still_locked = self.command_raw('vision-mark 300 330')
        self.assertFalse(still_locked['ok'])
        self.assertIn('VISION_FALLBACK_LOCKED', still_locked['error'])
        self.assertIn('0/3', still_locked['error'])

    def test_context_navigation_clears_failure_progress_on_return(self):
        fixture_a = (ROOT / 'tests/fixture_vision_canvas.html').as_uri()
        fixture_b = (ROOT / 'tests/fixture.html').as_uri()
        self.command(f'open {fixture_a}')
        self.unlock_vision_fallback()
        self.command(f'open {fixture_b}')
        self.command('get url')
        self.command(f'open {fixture_a}')
        self.command('screenshot')
        locked = self.command_raw('vision-mark 300 330')
        self.assertFalse(locked['ok'])
        self.assertIn('VISION_FALLBACK_LOCKED', locked['error'])
        self.assertIn('0/3', locked['error'])

    def test_resolved_but_disabled_ref_resets_fallback_progress(self):
        self.open_fixture()
        for count in range(1, 3):
            blocked = self.command_raw('click-css "#pi-nodriver-missing-target"')
            self.assertFalse(blocked['ok'])
            self.assertIn(f'{count}/3', blocked['error'])

        snapshot = self.command('snapshot -i')['text']
        go_ref = next(line for line in snapshot.splitlines() if 'Go now' in line).split()[0]
        disable_ref = next(line for line in snapshot.splitlines() if 'Disable go' in line).split()[0]
        self.command(f'click {disable_ref}')

        disabled_result = self.command_raw(f'click-js {go_ref}')
        self.assertFalse(disabled_result['ok'])
        self.assertIn('disabled', disabled_result['error'])

        self.command('screenshot')
        locked = self.command_raw('vision-mark 300 330')
        self.assertFalse(locked['ok'])
        self.assertIn('VISION_FALLBACK_LOCKED', locked['error'])
        self.assertIn('0/3', locked['error'])

    def test_vision_mark_requires_fresh_screenshot_after_unlocking_fallback(self):
        fixture_url = (ROOT / 'tests/fixture_vision_canvas.html').as_uri()
        self.command(f'open {fixture_url}')
        self.command('screenshot')
        self.unlock_vision_fallback()
        stale_shot_blocked = self.command_raw('vision-mark 300 330')
        self.assertFalse(stale_shot_blocked['ok'])
        self.assertIn('VISION_SCREENSHOT_REQUIRED', stale_shot_blocked['error'])

        self.command('screenshot')
        fresh_shot_ok = self.command('vision-mark 300 330')
        self.assertEqual(fresh_shot_ok['action'], 'vision-mark')


if __name__ == '__main__':
    unittest.main()
