import os
import asyncio
import socket
import unittest
from unittest.mock import AsyncMock, Mock, patch
from worker import BrowserWorker

class SharedBrowserAttachTests(unittest.IsolatedAsyncioTestCase):
    async def test_attach_uses_existing_browser_instead_of_launching_chrome(self):
        browser = Mock()
        browser.send = AsyncMock()
        # A local listener stands in for the shared Chrome's DevTools port (never a real one).
        listener = await asyncio.start_server(lambda reader, writer: writer.close(), '127.0.0.1', 0)
        self.addAsyncCleanup(listener.wait_closed)
        self.addCleanup(listener.close)
        port = listener.sockets[0].getsockname()[1]
        with patch.dict(os.environ, {'PI_NODRIVER_ATTACH_PORT': str(port)}), patch('worker.uc.start', new_callable=AsyncMock, return_value=browser) as start:
            worker = BrowserWorker()
            await worker.ensure_browser()
            start.assert_awaited_once_with(host='127.0.0.1', port=port)
            self.assertIsNone(worker.launched_browser)

    async def test_attach_to_exited_chrome_fails_fast_and_requests_worker_exit(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]  # closed again: nothing listens there
        reasons = []
        with patch.dict(os.environ, {'PI_NODRIVER_ATTACH_PORT': str(port)}), patch('worker.uc.start', new_callable=AsyncMock) as start:
            worker = BrowserWorker()
            worker.on_shared_browser_lost = reasons.append
            with self.assertRaisesRegex(RuntimeError, 'shared Chrome'):
                await asyncio.wait_for(worker.ensure_browser(), timeout=1.5)
            start.assert_not_awaited()
        self.assertEqual(len(reasons), 1)

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
