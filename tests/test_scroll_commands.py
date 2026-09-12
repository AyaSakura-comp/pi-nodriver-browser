import unittest
from unittest.mock import AsyncMock, MagicMock
from worker import BrowserWorker


class ScrollCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.worker = BrowserWorker()
        self.mock_page = MagicMock()
        self.mock_page.evaluate = AsyncMock()
        self.worker.require_page = AsyncMock(return_value=self.mock_page)
        self.worker.wait_for_page_ready = AsyncMock()

    async def test_scroll_to_absolute(self):
        self.mock_page.evaluate.return_value = (
            '{"targetName": "Page Window", "scrollY": 1500, "maxY": 5000, "percentY": 30, '
            '"atBottom": false, "atTop": false, "moved": true}'
        )
        res = await self.worker.execute('scroll to 1500', 'sess1')
        self.assertEqual(res['action'], 'scroll')
        self.assertEqual(res['direction'], 'to')
        self.assertEqual(res['scrollY'], 1500)
        self.assertIn('Scrolled directly to 1500px', res['text'])

    async def test_scroll_to_percentage(self):
        self.mock_page.evaluate.return_value = (
            '{"targetName": "Page Window", "scrollY": 2500, "maxY": 5000, "percentY": 50, '
            '"atBottom": false, "atTop": false, "moved": true}'
        )
        res = await self.worker.execute('scroll to 50%', 'sess1')
        self.assertEqual(res['direction'], 'to-percent')
        self.assertIn('Scrolled directly to 50%', res['text'])

    async def test_scroll_shorthand_percentage(self):
        self.mock_page.evaluate.return_value = (
            '{"targetName": "Page Window", "scrollY": 2500, "maxY": 5000, "percentY": 50, '
            '"atBottom": false, "atTop": false, "moved": true}'
        )
        res = await self.worker.execute('scroll 50%', 'sess1')
        self.assertEqual(res['direction'], 'to-percent')
        self.assertIn('Scrolled directly to 50%', res['text'])

    async def test_scroll_to_text(self):
        self.mock_page.evaluate.return_value = (
            '{"targetName": "Page Window", "scrollY": 4030, "maxY": 9000, "percentY": 45, '
            '"atBottom": false, "atTop": false, "moved": true}'
        )
        res = await self.worker.execute('scroll to-text "暢銷排行榜"', 'sess1')
        self.assertEqual(res['direction'], 'to-text')
        self.assertIn('Scrolled to text "暢銷排行榜"', res['text'])

    async def test_scroll_loop_guard_still_triggers_at_3(self):
        self.mock_page.evaluate.return_value = (
            '{"targetName": "Page Window", "scrollY": 600, "maxY": 5000, "percentY": 12, '
            '"atBottom": false, "atTop": false, "moved": true}'
        )
        await self.worker.execute('scroll down 600', 'sess_guard')
        await self.worker.execute('scroll down 600', 'sess_guard')
        with self.assertRaises(ValueError) as ctx:
            await self.worker.execute('scroll down 600', 'sess_guard')
        self.assertIn('SCROLL_LOOP_GUARD', str(ctx.exception))

    async def test_scroll_loop_guard_triggers_on_ping_pong(self):
        self.mock_page.evaluate.return_value = (
            '{"targetName": "Page Window", "scrollY": 600, "maxY": 5000, "percentY": 12, '
            '"atBottom": false, "atTop": false, "moved": true}'
        )
        await self.worker.execute('scroll down 600', 'sess_pong')
        with self.assertRaises(ValueError) as ctx:
            await self.worker.execute('scroll up 600', 'sess_pong')
        self.assertIn('SCROLL_LOOP_GUARD', str(ctx.exception))
