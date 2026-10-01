"""Offline contracts for the research scheduler; no browser/model is started."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path


class AvailabilityTests(unittest.TestCase):
    def test_research_contracts_exist(self):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec('research'),
                             'research package is not implemented yet')


# Import lazily so the first RED reports the missing behavior, not collection errors.
def api():
    from research.contracts import SearchTask, SearchResult, Limits
    from research.budget import QueryBudget
    from research.frontier import Frontier
    from research.registry import SourceRegistry
    from research.evidence import EvidenceWriter, FrozenJournalError, PacketTooLarge
    return SearchTask, SearchResult, Limits, QueryBudget, Frontier, SourceRegistry, EvidenceWriter, FrozenJournalError, PacketTooLarge


class ContractTests(unittest.TestCase):
    def test_nonempty_ids_query_direction_required(self):
        Task, *_ = api()
        for field in ('task_id', 'topic_id', 'query', 'direction'):
            kw = dict(task_id='q1', topic_id='t1', query='x', direction='official')
            kw[field] = ' '
            with self.subTest(field=field), self.assertRaises(ValueError):
                Task(**kw)

    def test_provider_depth_and_query_limits(self):
        Task, *_ = api()
        for kw in (dict(provider='bad'), dict(depth=-1), dict(depth=True),
                   dict(query='x' * 10001), dict(depth=1), dict(depth=0, parent_topic_id='t0')):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                Task(**(dict(task_id='q1', topic_id='t1', query='x', direction='d') | kw))

    def test_invalid_limits_rejected(self):
        _, _, Limits, *_ = api()
        for kw in (dict(search_budget=-1), dict(search_concurrency=0), dict(search_concurrency=True),
                   dict(laya_concurrency=0), dict(crawl_concurrency=0), dict(max_crawls=-1),
                   dict(max_packet_bytes=0), dict(drain_timeout=0), dict(drain_timeout=float('nan'))):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                Limits(**kw)
        self.assertEqual(Limits(search_budget=0).search_concurrency, 3)

    def test_result_accepts_only_network_urls_without_credentials(self):
        _, Result, *_ = api()
        for url in ('file:///etc/passwd', 'javascript:alert(1)', 'https://user:pw@example.com',
                    'https://', 'https://example.com/\nfoo'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                Result(title='Title', url=url, description='text')
        self.assertEqual(Result(title='Title', url='https://example.com/?x=1', description='').description, '')


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_atomic_budget(self):
        Budget = api()[3]
        b = Budget(2)
        grants = await asyncio.gather(*(b.reserve(f'a{i}') for i in range(20)))
        self.assertEqual(sum(grants), 2)
        self.assertEqual(b.used, 2)
        self.assertEqual(b.remaining, 0)

    async def test_attempt_ids_cannot_be_reused_and_no_refund(self):
        Budget = api()[3]
        b = Budget(3)
        self.assertTrue(await b.reserve('first'))
        self.assertFalse(await b.reserve('first'))
        self.assertTrue(await b.reserve('retry'))
        self.assertTrue(await b.reserve('google-fallback'))
        self.assertFalse(await b.reserve('fourth'))
        self.assertEqual(b.attempts, ('first', 'retry', 'google-fallback'))


class FrontierTests(unittest.TestCase):
    def task(self, id, query=None, **kw):
        Task = api()[0]
        return Task(task_id=id, topic_id=id, query=query or id, direction='official', **kw)

    def test_parallel_same_level_and_child_barrier(self):
        Frontier = api()[4]
        f = Frontier()
        for id in ('a', 'b', 'c'):
            self.assertTrue(f.add(self.task(id)))
        roots = f.take(2)
        self.assertEqual([t.task_id for t in roots], ['a', 'b'])
        f.add(self.task('child', depth=1, parent_topic_id='a'))
        f.complete('a')
        self.assertEqual([t.task_id for t in f.take(3)], ['c'])
        f.complete('c')
        self.assertEqual(f.take(3), [])
        f.complete('b')
        self.assertEqual([t.task_id for t in f.take(3)], ['child'])
        f.complete('child')
        self.assertTrue(f.idle)

    def test_normalized_query_dedup_is_provider_scoped(self):
        f = api()[4]()
        self.assertTrue(f.add(self.task('a', '  Python   TaskGroup ')))
        self.assertFalse(f.add(self.task('b', 'python taskgroup')))
        self.assertTrue(f.add(self.task('c', 'python taskgroup', provider='google')))
        self.assertEqual(f.pending_count, 2)

    def test_task_identity_and_invalid_completion(self):
        f = api()[4]()
        f.add(self.task('a'))
        with self.assertRaises(ValueError):
            f.add(self.task('a', query='different'))
        with self.assertRaises(ValueError):
            f.complete('a')
        self.assertEqual(len(f.take(1)), 1)
        f.complete('a')
        with self.assertRaises(ValueError):
            f.complete('a')

    def test_late_shallower_topic_waits_for_running_deeper_tasks(self):
        f = api()[4]()
        f.add(self.task('a'))
        f.take(1); f.complete('a')
        f.add(self.task('b', depth=1, parent_topic_id='a'))
        f.take(1); f.complete('b')
        f.add(self.task('c', depth=2, parent_topic_id='b'))
        f.add(self.task('d', depth=2, parent_topic_id='b'))
        f.take(1)
        f.add(self.task('late', depth=1, parent_topic_id='a'))
        self.assertEqual(f.take(1), [])
        f.complete('c')
        self.assertEqual([t.task_id for t in f.take(1)], ['late'])
        f.complete('late')
        self.assertEqual([t.task_id for t in f.take(1)], ['d'])

    def test_unknown_parent_rejected(self):
        f = api()[4]()
        with self.assertRaises(ValueError):
            f.add(self.task('x', depth=1, parent_topic_id='unknown'))


class RegistryTests(unittest.TestCase):
    def test_dedup_retains_original_url_and_topic_links(self):
        _, Result, _, _, _, Registry, *_ = api()
        r = Registry('job1')
        a, fresh = r.discover(Result('A', 'https://EXAMPLE.com/a#one', 'first'), 't1', 'q1', '4get')
        b, fresh2 = r.discover(Result('B', 'https://example.com/a#two', 'second'), 't2', 'q2', 'google')
        self.assertTrue(fresh); self.assertFalse(fresh2)
        self.assertEqual(a.source_id, b.source_id)
        self.assertEqual(a.url, 'https://EXAMPLE.com/a#one')
        self.assertEqual(a.topic_ids, ('t1', 't2'))
        self.assertEqual(len(a.discoveries), 2)

    def test_semantic_query_and_trailing_slash_are_distinct(self):
        _, Result, _, _, _, Registry, *_ = api()
        r = Registry('j')
        urls = ['https://x.test/a', 'https://x.test/a/', 'https://x.test/a?q=1', 'https://x.test/a?q=2']
        ids = [r.discover(Result('t', u, ''), 't', f'q{i}', '4get')[0].source_id for i, u in enumerate(urls)]
        self.assertEqual(len(set(ids)), 4)

    def test_crawl_state_machine_and_unknown_provenance(self):
        _, Result, _, _, _, Registry, *_ = api()
        r = Registry('j'); src, _ = r.discover(Result('t', 'https://x.test', ''), 't', 'q', '4get')
        self.assertTrue(r.queue_crawl(src.source_id))
        self.assertFalse(r.queue_crawl(src.source_id))
        r.start_crawl(src.source_id)
        r.finish_crawl(src.source_id, success=False)
        self.assertEqual(src.status, 'failed')
        self.assertFalse(r.queue_crawl(src.source_id))
        self.assertTrue(r.queue_crawl(src.source_id, retry=True))
        r.start_crawl(src.source_id); r.finish_crawl(src.source_id, success=True)
        self.assertFalse(r.queue_crawl(src.source_id, retry=True))
        with self.assertRaises(KeyError):
            r.queue_crawl('invented')
        with self.assertRaises(ValueError):
            r.finish_crawl(src.source_id, success=True)


class EvidenceTests(unittest.IsolatedAsyncioTestCase):
    def record(self, id='e1', **kw):
        return dict(eventId=id, sourceId='s1', topicIds=['t1'], kind='search_snippet',
                    url='https://example.com', title='標題', text='完整\n內容', status='completed', truncated=False) | kw

    async def test_one_writer_concurrent_append_freeze_preserves_every_text(self):
        Writer = api()[6]
        with tempfile.TemporaryDirectory() as d:
            async with Writer(Path(d), 'j') as w:
                records = [self.record(f'e{i}', text=f'內容{i}\n' + 'x' * 1000) for i in range(20)]
                await asyncio.gather(*(w.append(r) for r in records))
                # Idempotent replay cannot add another record.
                self.assertFalse(await w.append(records[0]))
                snap = await w.freeze(status='incomplete', reason='budget_exhausted', gaps=['release date'])
                self.assertEqual(snap.record_count, 20)
                packet = snap.render(max_bytes=100000)
                for r in records:
                    self.assertIn(r['text'], packet)
                self.assertIn('search_snippet', packet)
                self.assertIn('incomplete', packet)
                self.assertIn('release date', packet)
                self.assertEqual(len(snap.sha256), 64)
                with self.assertRaises(api()[7]):
                    await w.append(self.record('late'))
            lines = (Path(d) / 'j' / 'evidence.jsonl').read_text().splitlines()
            self.assertEqual(len(lines), 20)
            self.assertTrue(all(json.loads(l)['jobId'] == 'j' for l in lines))
            self.assertEqual((Path(d) / 'j').stat().st_mode & 0o777, 0o700)

    async def test_snippet_and_extract_both_preserved_and_overflow_explicit(self):
        Writer, _, TooLarge = api()[6:]
        with tempfile.TemporaryDirectory() as d:
            async with Writer(Path(d), 'j') as w:
                await w.append(self.record('e1'))
                await w.append(self.record('e2', kind='page_extract', text='全文', truncated=True))
                snap = await w.freeze(status='incomplete', reason='source_truncated', gaps=['full page'])
                self.assertIn('完整\n內容', snap.render(max_bytes=10000))
                self.assertIn('全文', snap.render(max_bytes=10000))
                self.assertIn('truncated', snap.render(max_bytes=10000))
                with self.assertRaises(TooLarge):
                    snap.render(max_bytes=10)

    async def test_reject_secrets_unknown_fields_and_event_id_collision(self):
        Writer = api()[6]
        with tempfile.TemporaryDirectory() as d:
            async with Writer(Path(d), 'j') as w:
                with self.assertRaises(ValueError):
                    await w.append(self.record(headers={'Authorization': 'secret'}))
                await w.append(self.record())
                with self.assertRaises(ValueError):
                    await w.append(self.record(text='changed'))

    async def test_exclusive_job_ownership_and_path_validation(self):
        Writer = api()[6]
        with tempfile.TemporaryDirectory() as d:
            for id in ('../bad', '/tmp/bad', '', 'a/b'):
                with self.subTest(id=id), self.assertRaises(ValueError):
                    Writer(Path(d), id)
            async with Writer(Path(d), 'j'):
                with self.assertRaises(FileExistsError):
                    async with Writer(Path(d), 'j'):
                        pass

    async def test_frozen_snapshot_detects_external_mutation(self):
        Writer = api()[6]
        with tempfile.TemporaryDirectory() as d:
            async with Writer(Path(d), 'j') as w:
                await w.append(self.record())
                snap = await w.freeze(status='incomplete', reason='test', gaps=['x'])
            snap.path.write_text('changed')
            with self.assertRaises(ValueError):
                snap.render(max_bytes=10000)
