"""Event-gated regression for cancellation while shutdown admission is blocked."""
import asyncio
from pathlib import Path
import tempfile
import threading
import unittest

from research.evidence import EvidenceWriter


class EvidenceShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_shutdown_admits_sentinel_and_closes_file(self):
        with tempfile.TemporaryDirectory() as root:
            writer = await EvidenceWriter(Path(root), 'shutdown', queue_size=1).__aenter__()
            entered, release = asyncio.Event(), threading.Event()
            loop = asyncio.get_running_loop()
            original = writer._write
            def blocked(line):
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(2):
                    raise AssertionError('fixture gate not released')
                original(line)
            writer._write = blocked
            def record(event):
                return dict(eventId=event, sourceId='s1', topicIds=['t1'], kind='page_extract',
                            url='https://example.com/', title='fixture', text='text',
                            status='completed', truncated=False)
            first = asyncio.create_task(writer.append(record('e1')))
            await asyncio.wait_for(entered.wait(), 1)
            second = asyncio.create_task(writer.append(record('e2')))
            # FIFO callback barrier: second append has filled the one-item queue.
            ready = asyncio.Event(); loop.call_soon(ready.set); await ready.wait()
            self.assertTrue(writer._queue.full())
            closing = asyncio.create_task(writer.__aexit__(None, None, None))
            ready.clear(); loop.call_soon(ready.set); await ready.wait()
            closing.cancel()
            ready.clear(); loop.call_soon(ready.set); await ready.wait()
            closing.cancel()  # repeated cancellation must not interrupt cleanup
            release.set()
            try:
                with self.assertRaises(asyncio.CancelledError):
                    await closing
                await asyncio.gather(first, second)
                self.assertTrue(writer._task.done(), 'shutdown leaked evidence consumer')
                self.assertTrue(writer._file.closed)
            finally:
                release.set()
                if not writer._task.done():
                    await writer._queue.put(None)
                    await writer._task
