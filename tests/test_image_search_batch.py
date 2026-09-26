"""Batch orchestration contracts without network or model calls."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from PIL import Image
import worker
try:
    import test_google_lens as lens_tests
except ImportError:
    from tests import test_google_lens as lens_tests


class BatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.w = worker.BrowserWorker()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = []
        for color in ('red', 'blue', 'green'):
            p = Path(self.tmp.name) / (color + '.png')
            Image.new('RGB', (4, 4), color).save(p)
            self.paths.append(str(p))
        self.parent = SimpleNamespace(url='https://example.com/')
        self.w.pages['owner'] = self.parent
        self.w.browser_modes['owner'] = 'android'
        self.w.bounded_vision_fallback_context = AsyncMock(side_effect=ValueError('unused'))
        self.w.attach_action_screenshot = AsyncMock()
        self.w.ensure_page_front = AsyncMock()
        self.w.close_session_page = AsyncMock(side_effect=self.close_page)
        self.running = 0
        self.peak = 0
        self.seen = []
        async def search(parts, sid):
            self.running += 1
            self.peak = max(self.peak, self.running)
            self.seen.append((parts[1], sid))
            self.w.pages[sid] = SimpleNamespace(url='https://www.google.com/search', close=AsyncMock())
            try:
                await asyncio.sleep(0.01)
                return {'status': 'results', 'action': 'image-search', 'uploadAttempted': True,
                        'results': [{'ref': '@lens-' + sid, 'title': Path(parts[1]).stem}], 'text': 'matches'}
            finally:
                self.running -= 1
        self.w.image_search = AsyncMock(side_effect=search)

    async def close_page(self, sid, page):
        self.w.pages.pop(sid, None)
        return {'action': 'close'}

    async def batch(self, paths=None, concurrency=2):
        return await self.w.execute('image-search-batch ' + json.dumps({
            'paths': self.paths if paths is None else paths, 'concurrency': concurrency}), 'owner')

    async def test_real_overlap_and_bound_with_input_pairing(self):
        r = await self.batch()
        self.assertEqual(self.peak, 2)
        self.assertEqual([j['path'] for j in r['searches']], self.paths)
        self.assertEqual(len({sid for _, sid in self.seen}), 3)
        self.assertEqual(len({j['searchId'] for j in r['searches']}), 3)
        self.assertIs(self.w.pages['owner'], self.parent)
        self.assertEqual(self.w.browser_modes['owner'], 'android')
        for j in r['searches']:
            self.assertEqual(j['results'][0]['title'], Path(j['path']).stem)
            self.assertIn(j['searchId'], r['text'])

    async def test_failure_is_per_image_and_not_retried(self):
        original = self.w.image_search.side_effect
        async def search(parts, sid):
            if parts[1] == self.paths[1]:
                raise RuntimeError('test blocked')
            return await original(parts, sid)
        self.w.image_search.side_effect = search
        r = await self.batch()
        self.assertEqual([j['status'] for j in r['searches']], ['results', 'uncertain', 'results'])
        self.assertEqual(self.w.image_search.await_count, 3)

    async def test_validates_entire_batch_before_any_upload(self):
        for data in ({'paths': []}, {'paths': self.paths * 2},
                     {'paths': self.paths, 'concurrency': 4},
                     {'paths': self.paths, 'concurrency': True},
                     {'paths': [self.paths[0], '/missing/fixture.png']},
                     {'paths': [self.paths[0], self.paths[0]]}):
            with self.subTest(data=data):
                with self.assertRaises((ValueError, FileNotFoundError)):
                    await self.w.execute('image-search-batch ' + json.dumps(data), 'owner')
        self.w.image_search.assert_not_awaited()

    async def test_owner_scoped_followup_routes_to_exact_child(self):
        r = await self.batch(self.paths[:2])
        job = r['searches'][1]
        self.w.google_lens = AsyncMock(return_value={'status': 'selected', 'text': 'Blue destination'})
        result = await self.w.execute('image-search-select ' + job['searchId'] + ' ' + job['results'][0]['ref'], 'owner')
        self.assertEqual(result['searchId'], job['searchId'])
        self.assertEqual(result['path'], self.paths[1])
        self.assertEqual(self.w.google_lens.call_args.args[1], self.seen[1][1])
        self.w.ensure_page_front.assert_awaited_once_with(self.w.pages[self.seen[1][1]])
        with self.assertRaisesRegex(ValueError, 'IMAGE_SEARCH_STALE'):
            await self.w.execute('image-search-results ' + job['searchId'], 'other')
        self.assertIs(self.w.pages['owner'], self.parent)

    async def test_completed_selection_cannot_reuse_old_refs_or_misguide_to_main_tab(self):
        r = await self.batch(self.paths[:1])
        j = r['searches'][0]
        self.w.google_lens = AsyncMock(return_value={'status': 'selected', 'text': 'destination'})
        command = 'image-search-select ' + j['searchId'] + ' ' + j['results'][0]['ref']
        selected = await self.w.execute(command, 'owner')
        self.assertIn('expired', selected['text'])
        for followup in [command, 'image-search-results ' + j['searchId']]:
            with self.assertRaisesRegex(ValueError, 'IMAGE_SEARCH_COMPLETE'):
                await self.w.execute(followup, 'owner')
        self.w.google_lens.assert_awaited_once()

    async def test_cross_image_ref_cannot_navigate(self):
        r = await self.batch(self.paths[:2])
        first, second = r['searches']
        for job, (_, sid) in zip(r['searches'], self.seen):
            page = self.w.pages[sid]
            self.w.lens_result_refs[sid] = (page, page.url, {job['results'][0]['ref']: job['results'][0]})
        self.w.require_page = AsyncMock(side_effect=lambda sid: self.w.pages[sid])
        with self.assertRaisesRegex(ValueError, 'LENS_STALE'):
            await self.w.execute('image-search-select ' + first['searchId'] + ' ' + second['results'][0]['ref'], 'owner')

    async def test_evicted_child_does_not_fall_back_to_parent_tab(self):
        r = await self.batch(self.paths[:1])
        self.w.pages.pop(self.seen[0][1])
        with self.assertRaisesRegex(ValueError, 'IMAGE_SEARCH_STALE'):
            await self.w.execute('image-search-results ' + r['searches'][0]['searchId'], 'owner')
        self.assertIs(self.w.pages['owner'], self.parent)

    async def test_new_batch_expires_previous_jobs_and_cleanup_is_owner_scoped(self):
        r = await self.batch(self.paths[:2])
        ids = [sid for _, sid in self.seen]
        await self.batch([self.paths[2]])
        for sid in ids:
            self.assertTrue(any(c.args[0] == sid for c in self.w.close_session_page.call_args_list))
        with self.assertRaisesRegex(ValueError, 'IMAGE_SEARCH_STALE'):
            await self.w.execute('image-search-results ' + r['searches'][0]['searchId'], 'owner')
        await self.w.execute('session-cleanup', 'owner')
        self.assertFalse(self.w.image_search_jobs.get('owner'))
        self.assertIs(self.w.pages['owner'], self.parent)

    async def test_cancel_drains_children_and_closes_only_batch_tabs(self):
        started = asyncio.Event()
        async def hang(parts, sid):
            self.running += 1
            started.set()
            try: await asyncio.Event().wait()
            finally: self.running -= 1
        self.w.image_search.side_effect = hang
        task = asyncio.create_task(self.batch())
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(self.running, 0)
        self.assertFalse(self.w.image_search_jobs.get('owner'))
        self.assertIs(self.w.pages['owner'], self.parent)

    async def test_batch_deadline_preserves_completed_results_and_cancels_rest(self):
        async def search(parts, sid):
            if parts[1] != self.paths[0]: await asyncio.Event().wait()
            return {'status': 'results', 'results': [{'ref': '@lens-one'}], 'text': 'match'}
        self.w.image_search.side_effect = search
        with patch.object(worker, 'IMAGE_SEARCH_BATCH_TIMEOUT', 0.03, create=True):
            r = await self.batch()
        self.assertEqual(r['searches'][0]['status'], 'results')
        self.assertEqual([x['status'] for x in r['searches'][1:]], ['uncertain', 'uncertain'])


class BatchTransportTests(unittest.TestCase):
    run_node = lens_tests.LensJavaScriptTests.run_node

    def test_tool_sends_one_batch_with_literal_paths_and_owner(self):
        source = (Path(__file__).resolve().parents[1] / 'index.ts').read_text()
        factory = source[source.index('export default function'):].replace('export default function', 'function register', 1)
        self.run_node(r'''
const Type = new Proxy({}, {get: () => (...args) => args[0]});
const DESCRIPTION='', VISION_FALLBACK_GUIDANCE='', SEARCH_FIRST_URL_RULE='';
const DEFAULT_MAX_LINES=2000, DEFAULT_MAX_BYTES=50000;
const truncateHead = content => ({content,truncated:false});
const calls=[];
class NodriverWorker {
 async request(command,session,signal) {
  calls.push({command,session,signal});
  return {action:'image-search-batch',text:'paired evidence',searches:[{searchId:'search-one'}]};
 }
}
''' + factory + r'''
const tools=new Map();register({on(){},registerTool(t){tools.set(t.name,t);}});
const tool=tools.get('image_search_batch');if(!tool)throw Error('missing batch tool');
const paths=["/tmp/a ' \\ x.png",'/tmp/b.png'];
const signal=new AbortController().signal;
const result=await tool.execute('id',{paths},signal,undefined,{sessionManager:{getSessionId:()=> 'owner'}});
if(calls.length!==1 || calls[0].session!=='owner' || calls[0].signal!==signal)throw Error('wrong routing');
const payload=JSON.parse(calls[0].command.slice('image-search-batch '.length));
if(JSON.stringify(payload.paths)!==JSON.stringify(paths)||payload.concurrency!==2)throw Error('changed input');
if(result.details.searches[0].searchId!=='search-one')throw Error('lost search pairing');
''', suffix='.mts')

    def test_batch_and_followups_are_never_replayed(self):
        source = (Path(__file__).resolve().parents[1] / 'index.ts').read_text()
        source = source[source.index('class NodriverWorker {'):source.index('export default function')]
        self.run_node(source + r'''
for (const action of ['image-search-batch', 'image-search-select', 'image-search-results']) {
  const w = new NodriverWorker(); let calls = 0;
  w.sendRequest = async () => {calls++; throw new Error('connection closed');};
  try {await w.request(action + ' {}', 'owner');} catch (_) {}
  if (calls !== 1) throw new Error(`${action} replayed ${calls} times`);
}
''', suffix='.mts')
