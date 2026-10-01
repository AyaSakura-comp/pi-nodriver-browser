"""Job-owned crawl scheduler process over a private inherited socket capability.

Only opaque request IDs cross this channel. The daemon retains exact registered
URLs and extracted text; a child cannot substitute either. No browser/model is
constructed in the child. EOF ends the child, and parent shutdown reaps it.
"""
import asyncio
import json
import math
from pathlib import Path
import socket
import sys

from .contracts import identifier, natural
from .controller_contracts import CrawlResult


class CrawlIPCError(RuntimeError):
    pass


async def _send(writer, lock, message):
    raw = json.dumps(message).encode() + b'\n'
    if len(raw) > 4096:
        raise CrawlIPCError('crawl_frame_too_large')
    async with lock:
        writer.write(raw)
        await writer.drain()


async def _receive(reader):
    line = await reader.readline()
    if not line:
        raise CrawlIPCError('crawl_consumer_disconnected')
    message = json.loads(line)
    if not isinstance(message, dict) or set(message) != {'type', 'id'}:
        raise CrawlIPCError('invalid_crawl_frame')
    identifier(message['id'])
    return message


class CrawlConsumer:
    def __init__(self, *, job_id, extract, concurrency=2, queue_size=32, cleanup_timeout=2.0):
        self.job_id = identifier(job_id)
        natural(concurrency, 'concurrency', 1); natural(queue_size, 'queue_size', 1)
        if concurrency > 64 or queue_size > 128:
            raise ValueError('crawl consumer limit exceeded')
        if type(cleanup_timeout) not in (int,float) or not math.isfinite(cleanup_timeout) or cleanup_timeout <= 0:
            raise ValueError('invalid cleanup timeout')
        self.cleanup_timeout = cleanup_timeout
        self.detached_tasks = set()
        self.extract = extract
        self.concurrency, self.queue_size = concurrency, queue_size
        self._slots = asyncio.Semaphore(queue_size)
        self._lock = asyncio.Lock()
        self._pending, self._running, self._results, self._sources = {}, {}, {}, {}
        self._counter = 0
        self._closed = False
        self.process = None

    async def __aenter__(self):
        parent, child = socket.socketpair()
        try:
            self.process = await asyncio.create_subprocess_exec(
                sys.executable, '-m', 'research.crawl_jobs', str(child.fileno()),
                str(self.concurrency), str(self.queue_size),
                pass_fds=(child.fileno(),), cwd=str(Path(__file__).resolve().parents[1]),
                env={'PYTHONUTF8': '1'}, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            child.close()
            self._reader, self._writer = await asyncio.open_connection(sock=parent, limit=4096)
            self._reader_task = asyncio.create_task(self._read())
        except BaseException:
            parent.close(); child.close()
            if self.process is not None:
                if self.process.returncode is None: self.process.kill()
                await self.process.wait()
            raise CrawlIPCError('crawl_consumer_start_failed') from None
        return self

    async def crawl(self, request):
        if request.job_id != self.job_id:
            raise ValueError('crawl_job_mismatch')
        previous = self._sources.get(request.source_id)
        if previous is not None:
            raise ValueError('duplicate_or_conflicting_crawl_source')
        self._sources[request.source_id] = request.url
        async with self._slots:
            if self._closed:
                raise CrawlIPCError('crawl_consumer_closed')
            self._counter += 1
            rid = f'c{self._counter}'
            future = asyncio.get_running_loop().create_future()
            self._pending[rid] = (request, future)
            try:
                await _send(self._writer, self._lock, {'type': 'queue', 'id': rid})
                return await future
            finally:
                self._pending.pop(rid, None)
                task = self._running.get(rid)
                if task is not None and not task.done(): task.cancel()
                self._results.pop(rid, None)

    async def _extract(self, rid, request):
        try:
            result = await self.extract(request)
            if not isinstance(result, CrawlResult):
                raise ValueError('invalid extraction')
        except asyncio.CancelledError:
            result = CrawlResult('', success=False)
        except Exception:
            result = CrawlResult('', success=False)
        if not self._closed:
            self._results[rid] = result
            await _send(self._writer, self._lock, {'type': 'extracted', 'id': rid})

    async def _read(self):
        try:
            while True:
                message = await _receive(self._reader)
                rid = message['id']
                if message['type'] == 'run':
                    if rid not in self._pending or rid in self._running:
                        raise CrawlIPCError('unowned_or_duplicate_crawl_request')
                    request, _ = self._pending[rid]
                    self._running[rid] = asyncio.create_task(self._extract(rid, request))
                elif message['type'] == 'result' and rid in self._running:
                    task = self._running.pop(rid)
                    await task
                    result = self._results.pop(rid, CrawlResult('', success=False))
                    pending = self._pending.get(rid)
                    if pending and not pending[1].done(): pending[1].set_result(result)
                else:
                    raise CrawlIPCError('invalid_crawl_response')
        except (Exception, asyncio.CancelledError):
            self._closed = True
            for _, future in self._pending.values():
                if not future.done(): future.set_exception(CrawlIPCError('crawl_consumer_disconnected'))
            for task in self._running.values(): task.cancel()

    async def _close(self):
        self._closed = True
        self._reader_task.cancel()
        for task in self._running.values(): task.cancel()
        self._writer.close()
        await self._writer.wait_closed()
        done, pending = await asyncio.wait({self._reader_task, *self._running.values()}, timeout=self.cleanup_timeout)
        for task in done:
            if not task.cancelled(): task.exception()
        self.detached_tasks.update(pending)
        for task in pending:
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        try:
            await asyncio.wait_for(self.process.wait(), self.cleanup_timeout)
        except asyncio.TimeoutError:
            self.process.kill()
            await self.process.wait()
        self._results.clear()
        if pending:
            raise CrawlIPCError('crawl_cleanup_incomplete')

    async def __aexit__(self, *args):
        cleanup = asyncio.create_task(self._close())
        cancelled = False
        while not cleanup.done():
            try: await asyncio.shield(cleanup)
            except asyncio.CancelledError: cancelled = True
        cleanup.result()
        if cancelled: raise asyncio.CancelledError


async def _child(fd, concurrency, queue_size):
    reader, writer = await asyncio.open_connection(sock=socket.socket(fileno=fd), limit=4096)
    queue, lock, pending = asyncio.Queue(maxsize=queue_size), asyncio.Lock(), {}
    seen = set()
    async def consume():
        while True:
            rid = await queue.get()
            future = asyncio.get_running_loop().create_future()
            pending[rid] = future
            try:
                await _send(writer, lock, {'type': 'run', 'id': rid})
                await future
                await _send(writer, lock, {'type': 'result', 'id': rid})
            finally:
                pending.pop(rid, None); queue.task_done()
    workers = [asyncio.create_task(consume()) for _ in range(concurrency)]
    try:
        while True:
            message = await _receive(reader)
            rid = message['id']
            if message['type'] == 'queue' and rid not in seen and len(seen) < 1000:
                seen.add(rid)
                queue.put_nowait(rid)  # reader must never block extracted replies
            elif message['type'] == 'extracted' and rid in pending and not pending[rid].done():
                pending[rid].set_result(None)
            else:
                raise CrawlIPCError('invalid_crawl_command')
    finally:
        for task in workers: task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        writer.close(); await writer.wait_closed()


if __name__ == '__main__':
    try:
        asyncio.run(_child(int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])))
    except (Exception, KeyboardInterrupt):
        sys.exit(1)
