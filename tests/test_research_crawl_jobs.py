"""Real subprocess, fake daemon extraction, no Chrome/network."""
import asyncio
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from research.controller_contracts import CrawlRequest, CrawlResult
from research.contracts import SearchTask


def request(source='s1', url='https://example.com/'):
    return CrawlRequest('job', source, url, 'title', SearchTask('q1', 't1', 'q', 'd'), ('t1',), ('r1',))


class CrawlJobsTests(unittest.IsolatedAsyncioTestCase):
    async def test_process_overlaps_owned_requests_preserves_text_and_closes(self):
        from research.crawl_jobs import CrawlConsumer
        entered, release = asyncio.Event(), asyncio.Event()
        seen = []
        async def extract(req):
            seen.append(req)
            if len(seen) == 2: entered.set()
            await release.wait()
            return CrawlResult('完整\n' + req.source_id)
        with patch.dict(os.environ, {'PRIVATE_API_KEY': 'do-not-inherit'}):
            async with CrawlConsumer(job_id='job', extract=extract, concurrency=2) as consumer:
                process = consumer.process
                tasks = [asyncio.create_task(consumer.crawl(request(s))) for s in ('s1', 's2')]
                try:
                    await asyncio.wait_for(entered.wait(), 3)
                    self.assertNotEqual(process.pid, os.getpid())
                    environment = Path(f'/proc/{process.pid}/environ').read_bytes()
                    self.assertNotIn(b'PRIVATE_API_KEY', environment)
                finally: release.set()
                results = await asyncio.gather(*tasks)
                self.assertEqual([r.text for r in results], ['完整\ns1', '完整\ns2'])
            self.assertIsNotNone(process.returncode)

    async def test_wrong_job_and_conflicting_source_are_rejected(self):
        from research.crawl_jobs import CrawlConsumer
        async def extract(req): return CrawlResult('text')
        async with CrawlConsumer(job_id='job', extract=extract) as consumer:
            from dataclasses import replace
            with self.assertRaises(ValueError): await consumer.crawl(replace(request(), job_id='other'))
            await consumer.crawl(request())
            with self.assertRaises(ValueError): await consumer.crawl(request(url='https://example.com/changed'))

    async def test_cancelled_job_joins_process_and_owned_extraction(self):
        from research.crawl_jobs import CrawlConsumer
        entered, cleaned = asyncio.Event(), asyncio.Event()
        async def extract(req):
            entered.set()
            try: await asyncio.Event().wait()
            finally: cleaned.set()
        async with CrawlConsumer(job_id='job', extract=extract) as consumer:
            task = asyncio.create_task(consumer.crawl(request()))
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(cleaned.is_set())
        self.assertIsNotNone(consumer.process.returncode)

    async def test_stuck_cleanup_reaps_only_owned_child_and_reports_failure(self):
        from research.crawl_jobs import CrawlConsumer, CrawlIPCError
        entered, release = asyncio.Event(), asyncio.Event()
        async def extract(req):
            entered.set()
            while not release.is_set():
                try: await release.wait()
                except asyncio.CancelledError: pass
            return CrawlResult('late')
        consumer = await CrawlConsumer(job_id='job',extract=extract,cleanup_timeout=0.02).__aenter__()
        task = asyncio.create_task(consumer.crawl(request()))
        await asyncio.wait_for(entered.wait(),3)
        try:
            with self.assertRaisesRegex(CrawlIPCError,'crawl_cleanup_incomplete'):
                await asyncio.wait_for(consumer.__aexit__(None,None,None),1)
            self.assertIsNotNone(consumer.process.returncode)
            self.assertTrue(consumer.detached_tasks)
        finally:
            release.set()
            await asyncio.gather(task,*consumer.detached_tasks,return_exceptions=True)

    async def test_child_death_is_redacted_and_fails_pending(self):
        from research.crawl_jobs import CrawlConsumer, CrawlIPCError
        entered = asyncio.Event()
        async def extract(req): entered.set(); await asyncio.Event().wait()
        async with CrawlConsumer(job_id='job', extract=extract) as consumer:
            task = asyncio.create_task(consumer.crawl(request()))
            await asyncio.wait_for(entered.wait(), 3)
            consumer.process.kill()
            with self.assertRaises(CrawlIPCError): await asyncio.wait_for(task, 3)
