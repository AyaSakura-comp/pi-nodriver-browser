import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
from worker import BrowserWorker


class AutoScreenshotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.worker = BrowserWorker()
        self.worker.pages['test'] = SimpleNamespace(url='https://example.test/')
        self.worker.save_viewport_screenshot = AsyncMock(return_value=Path('/tmp/auto.jpg'))

    async def test_open_and_state_changes_attach_without_changing_text(self):
        for action in ('open', 'activate', 'select', 'scroll', 'fill-submit', 'vision-click'):
            result = {'text': 'DOM refs', 'action': action}
            await self.worker.attach_action_screenshot('test', action, result)
            self.assertEqual(result.get('screenshotPath'), '/tmp/auto.jpg')
            self.assertTrue(result.get('autoScreenshot'))
            self.assertEqual(result['text'], 'DOM refs')

    async def test_observations_and_typing_do_not_attach(self):
        for action in ('snapshot', 'get', 'find-option', 'crawl', 'google-search', 'fill', 'type', 'session-cleanup'):
            result = {'text': 'unchanged'}
            await self.worker.attach_action_screenshot('test', action, result)
            self.assertNotIn('screenshotPath', result)
        self.worker.save_viewport_screenshot.assert_not_awaited()

    async def test_existing_image_and_disable_setting_are_respected(self):
        result = {'screenshotPath': '/tmp/existing.png'}
        await self.worker.attach_action_screenshot('test', 'open', result)
        self.assertEqual(result['screenshotPath'], '/tmp/existing.png')
        with patch.dict('os.environ', {'PI_NODRIVER_AUTO_SCREENSHOT': '0'}):
            await self.worker.attach_action_screenshot('test', 'open', {})
        self.worker.save_viewport_screenshot.assert_not_awaited()

    async def test_capture_failure_does_not_fail_completed_action(self):
        self.worker.save_viewport_screenshot.side_effect = RuntimeError('capture failed')
        result = {'text': 'Activated'}
        await self.worker.attach_action_screenshot('test', 'activate', result)
        self.assertEqual(result['text'], 'Activated')
        self.assertEqual(result.get('autoScreenshot'), False)
        self.assertNotIn('screenshotPath', result)

    async def test_cancellation_propagates(self):
        self.worker.save_viewport_screenshot.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.worker.attach_action_screenshot('test', 'open', {})

    async def test_execute_connects_attachment_to_successful_result(self):
        self.worker._execute = AsyncMock(return_value={'text': 'Activated'})
        self.worker.bounded_vision_fallback_context = AsyncMock(side_effect=RuntimeError())
        result = await self.worker.execute('activate @e1', 'test')
        self.assertEqual(result.get('screenshotPath'), '/tmp/auto.jpg')
