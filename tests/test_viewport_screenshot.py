import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image
from worker import BrowserWorker


class ViewportScreenshotTests(unittest.IsolatedAsyncioTestCase):
    def test_is_empty_screenshot_detects_pure_black_and_passes_content(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            black_path = Path(temp_dir) / 'black.png'
            img = Image.new('RGB', (100, 100), color=(0, 0, 0))
            img.save(black_path)
            self.assertTrue(BrowserWorker.is_empty_screenshot(black_path))

            # 1-bit black
            black_1bit = Path(temp_dir) / 'black1bit.png'
            img_1bit = Image.new('1', (100, 100), color=0)
            img_1bit.save(black_1bit)
            self.assertTrue(BrowserWorker.is_empty_screenshot(black_1bit))

            # Image with real content
            content_path = Path(temp_dir) / 'content.png'
            img_content = Image.new('RGB', (100, 100), color=(255, 255, 255))
            img_content.putpixel((50, 50), (255, 0, 0))
            img_content.save(content_path)
            self.assertFalse(BrowserWorker.is_empty_screenshot(content_path))

    async def test_save_viewport_screenshot_brings_page_to_front(self):
        worker = BrowserWorker()
        page = SimpleNamespace(
            bring_to_front=AsyncMock(),
            save_screenshot=AsyncMock(),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            async def fake_save(output, format='png', full_page=False):
                img = Image.new('RGB', (100, 100), color=(200, 200, 200))
                img.save(output)

            page.save_screenshot.side_effect = fake_save

            with patch.dict(os.environ, {'PI_NODRIVER_XVFB_FORWARD_CLICK': '0'}):
                output = await worker.save_viewport_screenshot(page, 'test-prefix-')
                self.assertTrue(output.is_file())
                page.bring_to_front.assert_awaited_once()
                page.save_screenshot.assert_awaited_once()

    async def test_save_viewport_screenshot_falls_back_to_cdp_if_xvfb_empty(self):
        worker = BrowserWorker()
        page = SimpleNamespace(
            bring_to_front=AsyncMock(),
            save_screenshot=AsyncMock(),
        )

        async def fake_save(output, format='png', full_page=False):
            img = Image.new('RGB', (100, 100), color=(128, 64, 32))
            img.save(output)

        page.save_screenshot.side_effect = fake_save

        with patch.dict(os.environ, {'PI_NODRIVER_XVFB_FORWARD_CLICK': '1', 'DISPLAY': ':999'}):
            async def fake_create_subprocess(*args, **kwargs):
                out_file = Path(args[3])
                img = Image.new('1', (100, 100), color=0)
                img.save(out_file)

                proc = SimpleNamespace()
                proc.wait = AsyncMock(return_value=0)
                return proc

            with patch('asyncio.create_subprocess_exec', side_effect=fake_create_subprocess):
                output = await worker.save_viewport_screenshot(page, 'test-fallback-')
                self.assertTrue(output.is_file())
                page.save_screenshot.assert_awaited_once()
                self.assertFalse(BrowserWorker.is_empty_screenshot(output))

    async def test_save_viewport_screenshot_switches_session_pages(self):
        worker = BrowserWorker()
        page_a = SimpleNamespace(bring_to_front=AsyncMock(), save_screenshot=AsyncMock())
        page_b = SimpleNamespace(bring_to_front=AsyncMock(), save_screenshot=AsyncMock())

        async def fake_save(output, format='png', full_page=False):
            img = Image.new('RGB', (100, 100), color=(10, 20, 30))
            img.save(output)

        page_a.save_screenshot.side_effect = fake_save
        page_b.save_screenshot.side_effect = fake_save

        with patch.dict(os.environ, {'PI_NODRIVER_XVFB_FORWARD_CLICK': '0'}):
            await worker.save_viewport_screenshot(page_a, 'session-a-')
            page_a.bring_to_front.assert_awaited_once()

            await worker.save_viewport_screenshot(page_b, 'session-b-')
            page_b.bring_to_front.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
