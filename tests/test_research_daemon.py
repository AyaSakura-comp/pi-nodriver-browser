"""Research through the real worker socket dispatch, with a fake browser service and fake 4get."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import worker
from research import jobs


class ResearchDaemonTests(unittest.IsolatedAsyncioTestCase):
    async def test_research_bypasses_interactive_lock_and_disconnect_cancels_owned_job(self):
        started, held, release, cleaned, crawl_cancelled = (asyncio.Event() for _ in range(5))
        entered = []

        async def fourget(query):
            return [('Fixture', f'https://example.com/{query}', 'Complete answer')]

        async def crawl_one(url, *args, **kwargs):
            if url.endswith('/hang'):
                try:
                    await asyncio.Event().wait()
                finally:
                    crawl_cancelled.set()
            return dict(ok=True, text='Complete answer page')
        service = SimpleNamespace(close=AsyncMock(), health_loop=AsyncMock(), browser=None, ensure_browser=AsyncMock(),
                                  crawl_one=crawl_one, research_cleanup_tasks=set(),
                                  cleanup_research_owner=AsyncMock(side_effect=lambda owner: cleaned.set()))

        async def command(service, request):
            entered.append(request['command'])
            if request['command'] == 'hold':
                held.set()
                await release.wait()
            return dict(id=request['id'], ok=True, text='fixture', action=request['command'])
        actual_start = asyncio.start_unix_server

        async def start(*args, **kwargs):
            server = await actual_start(*args, **kwargs)
            started.set()
            return server
        loop = asyncio.get_running_loop()
        with tempfile.TemporaryDirectory() as root, patch.object(worker, 'BrowserService', return_value=service), \
                patch.object(worker, 'execute_request', side_effect=command), patch.object(jobs, 'fourget', fourget), \
                patch.object(asyncio, 'start_unix_server', side_effect=start), patch.object(loop, 'add_signal_handler'), \
                patch.object(worker, 'log_event', lambda *args, **kwargs: None):
            socket_path = str(Path(root) / 'daemon.sock')
            server_task = asyncio.create_task(worker.server_main(socket_path))
            await asyncio.wait_for(started.wait(), 1)
            reader, writer = await asyncio.open_unix_connection(socket_path)

            async def send(value):
                writer.write(json.dumps(value).encode() + b'\n')
                await writer.drain()

            async def read():
                line = await asyncio.wait_for(reader.readline(), 3)
                self.assertTrue(line.startswith(worker.MARKER.encode()))
                return json.loads(line[len(worker.MARKER):])
            try:
                await send(dict(id=1, sessionId='owner', command='hold'))
                await asyncio.wait_for(held.wait(), 1)
                params = dict(jobId='job', question='question', queries=['answer'],
                              clock={'iso': '2026-01-01T00:00:00Z', 'timezone': 'UTC'})
                await send(dict(id=2, sessionId='owner', command='research', research=params))
                frames = []
                while (frame := await read()).get('type') == 'progress':
                    frames.append(frame)
                self.assertTrue(frames and all(f['id'] == 2 and f['phase'] == 'evidence' for f in frames))
                self.assertTrue(frame['ok'])
                self.assertEqual((frame['id'], frame['action'], frame['jobId']), (2, 'research', 'job'))
                self.assertTrue(frame['fullEvidenceDelivered'])
                self.assertIn('Complete answer page', frame['text'])
                self.assertTrue(frame['text'].startswith(frames[-1]['prefix']))
                self.assertFalse(release.is_set(), 'research did not wait for the held interactive command')
                self.assertTrue(cleaned.is_set())
                # Interactive commands of one Pi session stay serialized; other sessions are not held up.
                await send(dict(id=4, sessionId='owner', command='queued'))
                await send(dict(id=5, sessionId='other', command='other'))
                self.assertEqual((await read())['id'], 5)
                self.assertNotIn('queued', entered)
                release.set()
                self.assertEqual([(await read())['id'] for _ in range(2)], [1, 4])
                # A second job hangs on a crawl; the client disconnects.
                cleaned.clear()
                await send(dict(id=3, sessionId='owner', command='research',
                                research=dict(params, jobId='disconnected', queries=['hang'])))
                self.assertEqual((await read())['type'], 'progress')
                writer.close()
                await writer.wait_closed()
                await asyncio.wait_for(asyncio.gather(cleaned.wait(), crawl_cancelled.wait()), 3)
                self.assertTrue((Path(root) / 'research-artifacts' / 'disconnected' / 'evidence.jsonl').exists())
            finally:
                release.set()
                writer.close()
                # Stop this fixture's server only, through its temporary socket.
                stop_reader, stop_writer = await asyncio.open_unix_connection(socket_path)
                stop_writer.write(b'{"id":99,"command":"shutdown"}\n')
                await stop_writer.drain()
                await asyncio.wait_for(stop_reader.readline(), 2)
                stop_writer.close()
                await stop_writer.wait_closed()
                await asyncio.wait_for(server_task, 3)


if __name__ == '__main__':
    unittest.main()
