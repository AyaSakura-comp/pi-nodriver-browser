import os
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch
from worker import BrowserWorker

class SharedBrowserAttachTests(unittest.IsolatedAsyncioTestCase):
    async def test_attach_uses_existing_browser_instead_of_launching_chrome(self):
        browser = Mock()
        browser.send = AsyncMock()
        with patch.dict(os.environ, {'PI_NODRIVER_ATTACH_PORT': '45661'}), patch('worker.uc.start', new_callable=AsyncMock, return_value=browser) as start:
            worker = BrowserWorker()
            await worker.ensure_browser()
            start.assert_awaited_once_with(host='127.0.0.1', port=45661)
            self.assertIsNone(worker.launched_browser)

    async def test_unresponsive_detach_has_a_deadline_without_closing_chrome(self):
        browser = Mock()
        browser.send = AsyncMock()
        browser.aclose = AsyncMock(side_effect=asyncio.Event().wait)
        worker = BrowserWorker()
        worker.browser = browser
        worker.shared_browser_attached = True
        with patch.dict(os.environ, {'PI_NODRIVER_DETACH_TIMEOUT': '0.01'}):
            await asyncio.wait_for(worker.shutdown_browser(), timeout=0.2)
        browser.send.assert_not_awaited()
        browser.stop.assert_not_called()
        self.assertIsNone(worker.browser)

    async def test_shutdown_of_attached_worker_does_not_close_shared_chrome(self):
        browser = Mock()
        browser.send = AsyncMock()
        browser.aclose = AsyncMock()
        worker = BrowserWorker()
        worker.browser = browser
        worker.shared_browser_attached = True
        await worker.shutdown_browser()
        browser.send.assert_not_awaited()
        browser.stop.assert_not_called()
        browser.aclose.assert_awaited_once()
        self.assertIsNone(worker.browser)
