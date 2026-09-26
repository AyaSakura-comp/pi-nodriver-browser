"""Single-action reverse-image search orchestration; no network required."""
import asyncio
import json
import shlex
import unittest
from unittest.mock import AsyncMock, patch

import worker
try:
    import test_google_lens as lens_tests
except ImportError:
    from tests import test_google_lens as lens_tests


class ImageSearchTests(unittest.IsolatedAsyncioTestCase):
    ready_page = lens_tests.LensCommandTests.ready_page

    async def asyncSetUp(self):
        await lens_tests.LensCommandTests.asyncSetUp(self)
        original_evaluate = self.page.evaluate.side_effect
        self.readiness = AsyncMock(return_value='complete')
        async def evaluate(script):
            if script == 'document.readyState':
                return await self.readiness()
            return await original_evaluate(script)
        self.page.evaluate.side_effect = evaluate
        self.real_execute = self.worker._execute
        self.open_calls = []

        async def dispatch(command, session_id='default', **kwargs):
            if shlex.split(command)[0] == 'open':
                self.open_calls.append((command, session_id, kwargs,
                                        self.worker.browser_modes.get(session_id)))
                return {'action': 'open', 'url': self.page.url}
            return await self.real_execute(command, session_id, **kwargs)
        self.worker._execute = dispatch

    async def search(self):
        return await self.worker.execute('image-search ' + shlex.quote(str(self.image)), 'lens-test')

    async def test_one_action_opens_desktop_uploads_once_returns_refs_and_restores_mode(self):
        self.worker.browser_modes['lens-test'] = 'android'
        self.ready_page()
        result = await self.search()
        self.assertEqual(result['action'], 'image-search')
        self.assertEqual(result['status'], 'results')
        self.assertEqual(result['results'], [self.result])
        self.assertEqual(self.open_calls, [(
            'open https://www.google.com/imghp?hl=en', 'lens-test',
            {'preserve_overlays': True}, 'linux')])
        self.assertEqual(self.worker.browser_modes['lens-test'], 'android')
        self.page.send.assert_awaited_once()
        self.page.get.assert_not_awaited()  # never auto-select a result

    async def test_invalid_image_never_opens_browser(self):
        with self.assertRaises((ValueError, FileNotFoundError)):
            await self.worker.execute('image-search /tmp/nonexistent-image-search-fixture.png', 'lens-test')
        self.assertEqual(self.open_calls, [])
        self.page.send.assert_not_awaited()

    async def test_previous_pending_upload_does_not_mark_a_preparation_failure_as_new_upload(self):
        self.worker.lens_pending_uploads['lens-test'] = (object(), 'old', set())
        self.worker.probe_page_access_gate.return_value = 'captcha widget'
        result = await self.search()
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse(result['uploadAttempted'])
        self.page.send.assert_not_awaited()

    async def test_gate_stops_without_upload_or_manual_click_advice(self):
        self.worker.probe_page_access_gate.return_value = 'captcha widget'
        result = await self.search()
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse(result['uploadAttempted'])
        self.assertNotIn('lens-test', self.worker.browser_modes)
        self.assertNotIn('vision-click', result['text'])
        self.page.send.assert_not_awaited()

    async def test_uncertain_upload_is_not_retried(self):
        self.ready_page()
        self.page.send.side_effect = RuntimeError('connection closed')
        result = await self.search()
        self.assertEqual(result['status'], 'uncertain')
        self.assertTrue(result['uploadAttempted'])
        self.assertEqual(len(self.open_calls), 1)
        self.page.send.assert_awaited_once()
        self.assertIn('Do not', result['text'])

    async def test_no_results_does_not_recommend_clicking_around(self):
        self.ready_page()
        with patch.object(self.worker, 'lens_results', AsyncMock(return_value={
                'results': [], 'status': 'no-results', 'text': 'vision-mark omni',
                'action': 'google-lens-results'})), patch.object(worker, 'LENS_RESULT_ATTEMPTS', 1):
            result = await self.search()
        self.assertNotIn('vision-mark', result['text'])
        self.assertIn('google-lens-results', result['text'])
        self.page.send.assert_awaited_once()

    async def test_timeout_restores_mode_and_never_retries(self):
        async def hang(*args, **kwargs):
            await asyncio.Event().wait()
        self.worker.google_lens = AsyncMock(side_effect=hang)
        with patch.object(worker, 'IMAGE_SEARCH_TIMEOUT', 0.01, create=True):
            result = await self.search()
        self.assertEqual(result['status'], 'uncertain')
        self.assertNotIn('lens-test', self.worker.browser_modes)
        self.assertEqual(len(self.open_calls), 1)
        self.worker.google_lens.assert_awaited_once()

    async def test_waits_for_complete_document_before_camera_preparation(self):
        self.readiness.side_effect = ['loading', 'interactive', 'complete']
        self.ready_page()
        result = await self.search()
        self.assertEqual(result['status'], 'results')
        self.assertEqual(self.readiness.await_count, 3)
        self.page.send.assert_awaited_once()

    async def test_incomplete_document_stops_without_click_or_upload(self):
        self.readiness.return_value = 'interactive'
        self.ready_page()
        with patch.object(worker, 'IMAGE_SEARCH_READY_ATTEMPTS', 2, create=True):
            result = await self.search()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.readiness.await_count, 2)
        self.page.send.assert_not_awaited()
        self.assertFalse(any('.click()' in c.args[0] for c in self.operation_evaluate.call_args_list))

    async def test_cancel_restores_mode(self):
        self.worker.google_lens = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await self.search()
        self.assertNotIn('lens-test', self.worker.browser_modes)


class ImageSearchTransportTests(unittest.TestCase):
    run_node = lens_tests.LensJavaScriptTests.run_node

    def test_standalone_tool_registration_and_single_request(self):
        from pathlib import Path
        text = (Path(__file__).resolve().parents[1] / 'index.ts').read_text()
        factory = text[text.index('export default function'):].replace('export default function', 'function register', 1)
        self.run_node(r'''
const Type = new Proxy({}, { get: () => (...args) => args[0] });
const DESCRIPTION = '', VISION_FALLBACK_GUIDANCE = '', SEARCH_FIRST_URL_RULE = '';
const DEFAULT_MAX_LINES = 2000, DEFAULT_MAX_BYTES = 50000;
const truncateHead = text => ({content:text, truncated:false});
const calls = [];
class NodriverWorker {
  async request(command, session, signal) {
    calls.push({command, session, signal});
    return {action:'image-search', status:'results', text:'candidate evidence', results:[{ref:'@lens-x'}]};
  }
}
''' + factory + r'''
const tools = new Map();
register({on(){}, registerTool(tool){tools.set(tool.name, tool);}});
const tool = tools.get('image_search');
if (!tool) throw new Error('missing standalone image_search tool');
const signal = new AbortController().signal;
const path = "/tmp/image ' \\ space.png";
const result = await tool.execute('id', {path}, signal, undefined, {sessionManager:{getSessionId:()=> 'session'}});
if (calls.length !== 1 || calls[0].session !== 'session' || calls[0].signal !== signal) throw new Error('wrong dispatch');
if (!calls[0].command.startsWith('image-search ')) throw new Error('wrong action');
if (result.details.status !== 'results' || result.content[0].text !== 'candidate evidence') throw new Error('lost evidence');
const {spawnSync} = await import('node:child_process');
const parsed = spawnSync('python3', ['-c', 'import shlex,sys,json;print(json.dumps(shlex.split(sys.argv[1])))', calls[0].command], {encoding:'utf8'});
if (JSON.parse(parsed.stdout)[1] !== path) throw new Error('path quoting mismatch');
''', suffix='.mts')

    def test_image_search_cannot_be_transport_replayed(self):
        from pathlib import Path
        text = (Path(__file__).resolve().parents[1] / 'index.ts').read_text()
        source = text[text.index('class NodriverWorker {'):text.index('export default function')]
        self.run_node(source + r'''
for (const command of ['image-search "/tmp/a.png"', ' IMAGE-SEARCH "/tmp/a.png"', '"image-search" "/tmp/a.png"']) {
  const instance = new NodriverWorker(); let calls = 0;
  instance.sendRequest = async () => { calls++; throw new Error('connection closed'); };
  try { await instance.request(command, 'test'); } catch (_) {}
  if (calls !== 1) throw new Error(`unsafe replay ${command}: ${calls}`);
}
''', suffix='.mts')


if __name__ == '__main__':
    unittest.main()
