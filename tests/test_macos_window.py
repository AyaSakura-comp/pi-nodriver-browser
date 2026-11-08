import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import browser_logic
import macos_window
from worker import BrowserWorker


class MacosHiddenChromeTests(unittest.IsolatedAsyncioTestCase):
    async def test_ensure_page_front_never_activates_the_hidden_chrome(self):
        worker = BrowserWorker()
        worker.macos_chrome_pid = 4242
        page = SimpleNamespace(target=SimpleNamespace(target_id='t1'),
                               bring_to_front=AsyncMock(), send=AsyncMock(), evaluate=AsyncMock())
        with patch.object(macos_window, 'should_hide', return_value=True), \
             patch.object(macos_window, 'hide', return_value=True) as hide:
            self.assertFalse(await worker.ensure_page_front(page))
        page.bring_to_front.assert_not_awaited()
        page.send.assert_awaited_once()  # Emulation.setFocusEmulationEnabled(true)
        hide.assert_called_once_with(4242)
        self.assertEqual(worker.xvfb_active_target_id, 't1')

    async def test_linux_and_visible_mode_keep_bringing_tabs_to_front(self):
        worker = BrowserWorker()
        page = SimpleNamespace(target=SimpleNamespace(target_id='t1'),
                               bring_to_front=AsyncMock(), send=AsyncMock(), evaluate=AsyncMock())
        with patch.object(macos_window, 'should_hide', return_value=False):
            self.assertTrue(await worker.ensure_page_front(page))
        page.bring_to_front.assert_awaited_once()
        page.send.assert_not_awaited()

    def test_hide_mode_defaults_to_hidden_and_accepts_visible(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(macos_window.hide_mode(), 'hidden')
        with patch.dict('os.environ', {'PI_NODRIVER_MACOS_WINDOW': 'Visible'}):
            self.assertEqual(macos_window.hide_mode(), 'visible')
        with patch.dict('os.environ', {'PI_NODRIVER_MACOS_WINDOW': 'bogus'}):
            self.assertEqual(macos_window.hide_mode(), 'hidden')

    def test_helpers_are_inert_off_macos(self):
        with patch.object(macos_window, 'IS_MACOS', False):
            self.assertFalse(macos_window.should_hide())
            self.assertIsNone(macos_window.frontmost_pid())
            self.assertFalse(macos_window.hide(1))
            self.assertIsNone(macos_window.keep_hidden(1))


@unittest.skipUnless(sys.platform == 'darwin', 'macOS process inspection')
class MacosProcessTests(unittest.TestCase):
    def test_pid_alive_without_procfs(self):
        import subprocess
        child = subprocess.Popen(['/bin/sleep', '30'])
        try:
            self.assertTrue(browser_logic.pid_alive(os.getpid()))
            self.assertTrue(browser_logic.pid_alive(child.pid))
        finally:
            child.kill(); child.wait()
        self.assertFalse(browser_logic.pid_alive(child.pid))

    def test_finds_chrome_in_applications(self):
        with patch.dict('os.environ'), \
             patch.object(browser_logic.shutil, 'which', return_value=None), \
             patch.object(browser_logic.os, 'access', side_effect=lambda p, m: str(p).startswith('/Applications/Google Chrome.app')):
            os.environ.pop('PI_NODRIVER_CHROME', None)
            self.assertEqual(browser_logic.resolve_browser_executable(),
                             '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')


if __name__ == '__main__':
    unittest.main()
