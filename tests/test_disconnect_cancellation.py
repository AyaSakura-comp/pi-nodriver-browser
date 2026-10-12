"""Socket timeout/disconnect must not leave browser commands holding locks."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import worker


class DisconnectCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_cancels_command_and_releases_same_session_lock(self):
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        never = asyncio.Event()
        calls = 0
        fake_worker = type('Worker', (), {'close': AsyncMock()})()

        class Research:
            def __init__(self, *args, **kwargs):
                pass

            async def close(self):
                pass

        async def execute(_worker, request):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                try:
                    await never.wait()
                finally:
                    cancelled.set()
            return {'id': request['id'], 'ok': True}

        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'browser.sock')
            loop = asyncio.get_running_loop()
            with patch.object(worker, 'BrowserWorker', return_value=fake_worker), \
                 patch.object(worker, 'execute_request', side_effect=execute), \
                 patch('research.jobs.ResearchConnection', Research), \
                 patch.object(loop, 'add_signal_handler'):
                server = asyncio.create_task(worker.server_main(path))
                writer = second_writer = None
                try:
                    for _ in range(100):
                        if Path(path).exists():
                            break
                        await asyncio.sleep(0.01)
                    _, writer = await asyncio.open_unix_connection(path)
                    writer.write((json.dumps({'id': 1, 'sessionId': 'same', 'command': 'get text'}) + '\n').encode())
                    await writer.drain()
                    await asyncio.wait_for(entered.wait(), 1)
                    writer.close()
                    await writer.wait_closed()
                    await asyncio.wait_for(cancelled.wait(), 0.3)

                    reader, second_writer = await asyncio.open_unix_connection(path)
                    second_writer.write((json.dumps({'id': 2, 'sessionId': 'same', 'command': 'get text'}) + '\n').encode())
                    await second_writer.drain()
                    response = await asyncio.wait_for(reader.readline(), 0.3)
                    self.assertIn(b'"ok": true', response)
                finally:
                    never.set()
                    for connection in (writer, second_writer):
                        if connection:
                            connection.close()
                            await connection.wait_closed()
                    server.cancel()
                    await asyncio.gather(server, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
