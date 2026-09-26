"""Offline Lens contract tests: no browser, network, or service processes."""
import asyncio
import json
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image
import browser_logic
import worker


class LensValidationTests(unittest.TestCase):
    def test_only_explicit_google_surfaces_are_allowed(self):
        check = getattr(browser_logic, 'is_google_lens_surface', None)
        self.assertIsNotNone(check, 'Lens origin validation is missing')
        for host, path in [('lens.google.com', '/'), ('www.google.com', '/search'), ('images.google.com', '/')]:
            self.assertTrue(check('https://' + host + path))
        for url in ['http://lens.google.com/', 'https://lens.google.com.evil.test/',
                    'https://user@lens.google.com/', 'https://lens.google.com:444/',
                    'https://accounts.google.com/', 'https://lens.google/',
                    'https://www.google.com/maps', 'file:///tmp/lens']:
            self.assertFalse(check(url), url)

    def test_image_validation_content_path_and_size(self):
        validate = getattr(worker, 'validate_lens_image', None)
        self.assertIsNotNone(validate, 'Lens image validation is missing')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'quoted image \' ; $(touch nope).png'
            Image.new('RGB', (2, 2)).save(path)
            self.assertEqual(validate(str(path)), str(path.resolve()))
            for invalid in ['relative.png', directory, str(path) + '.missing']:
                with self.assertRaises((ValueError, FileNotFoundError)):
                    validate(invalid)
            fake = Path(directory) / 'fake.png'
            fake.write_text('not an image')
            with self.assertRaises(ValueError):
                validate(str(fake))
            mismatch = Path(directory) / 'wrong.jpg'
            mismatch.write_bytes(path.read_bytes())
            with self.assertRaises(ValueError):
                validate(str(mismatch))
            with patch.object(worker, 'LENS_IMAGE_MAX_BYTES', 1):
                with self.assertRaises(ValueError):
                    validate(str(path))


class LensCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.worker = worker.BrowserWorker()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.image = Path(self.directory.name) / 'image \' ; $.png'
        Image.new('RGB', (2, 2)).save(self.image)
        self.page = SimpleNamespace(url='https://lens.google.com/',
                                    select=AsyncMock(return_value=SimpleNamespace(backend_node_id=worker.uc.cdp.dom.BackendNodeId(42))),
                                    send=AsyncMock(), evaluate=AsyncMock(), get=AsyncMock())
        self.current_document = {'url': self.page.url, 'title': 'Google Lens', 'text': 'Visual matches'}
        self.operation_evaluate = AsyncMock()
        async def evaluate(script):
            if script == worker.LENS_EVIDENCE_JS:
                return json.dumps(self.current_document)
            return await self.operation_evaluate(script)
        self.page.evaluate.side_effect = evaluate
        self.worker.pages['lens-test'] = self.page
        self.worker.require_page = AsyncMock(return_value=self.page)
        self.worker.bounded_vision_fallback_context = AsyncMock(side_effect=ValueError('unused'))
        self.worker.attach_action_screenshot = AsyncMock()
        self.worker.probe_page_access_gate = AsyncMock(return_value=None)
        self.worker.wait_for_page_ready = AsyncMock()
        self.upload_probe = {'url': self.page.url, 'title': 'Google Lens', 'text': 'Search by image',
                             'inputCount': 1, 'inputToken': 'fixture-input', 'formAction': ''}
        self.result = {'ref': '@lens-fixture-2', 'title': 'Requested blue chair',
                       'url': 'https://example.com', 'snippet': 'Blue chair source'}

    def ready_page(self):
        self.operation_evaluate.side_effect = [json.dumps(self.upload_probe), 'true',
                                         json.dumps({'url': self.page.url, 'title': 'Google Lens',
                                                     'text': 'Visual matches', 'results': [self.result]})]

    async def upload(self):
        return await self.worker.execute('google-lens ' + shlex.quote(str(self.image)), 'lens-test')

    async def test_upload_once_via_cdp_and_return_result_evidence(self):
        self.ready_page()
        result = await self.upload()
        self.assertEqual(result['status'], 'results')
        self.assertTrue(result['uploadAttempted'])
        self.assertEqual(result['results'], [self.result])
        self.assertIn(self.result['ref'], result['text'])
        self.assertIn(self.result['url'], result['text'])
        self.page.send.assert_awaited_once()
        command = self.page.send.call_args.args[0]
        cdp = next(command)
        self.assertEqual(cdp['method'], 'DOM.setFileInputFiles')
        self.assertEqual(cdp['params']['files'], [str(self.image)])
        self.assertEqual(cdp['params']['backendNodeId'], 42)
        self.page.get.assert_not_awaited()

    async def test_existing_results_are_not_reported_as_new_upload_evidence(self):
        self.upload_probe['resultUrls'] = [self.result['url']]
        self.operation_evaluate.side_effect = [json.dumps(self.upload_probe), 'true',
                                         json.dumps({'url': self.page.url, 'title': 'Google Lens',
                                                     'text': 'Visual matches', 'results': [self.result]})]
        with patch.object(worker, 'LENS_RESULT_ATTEMPTS', 1):
            result = await self.upload()
        self.assertEqual(result['status'], 'no-results')
        self.assertEqual(result['results'], [])
        self.assertNotIn('lens-test', self.worker.lens_result_refs)

    async def test_pending_upload_stays_inconclusive_across_observation_and_selection(self):
        self.upload_probe['resultUrls'] = [self.result['url']]
        self.ready_page()
        with patch.object(worker, 'LENS_RESULT_ATTEMPTS', 1):
            await self.upload()
        self.operation_evaluate.side_effect = None
        self.operation_evaluate.return_value = json.dumps({
            'url': self.page.url, 'title': 'Google Lens', 'text': 'Visual matches', 'results': [self.result]})
        result = await self.worker.execute('google-lens-results', 'lens-test')
        self.assertEqual(result['status'], 'no-results')
        self.assertEqual(result['results'], [])
        with self.assertRaisesRegex(ValueError, 'LENS_STALE'):
            await self.worker.execute('google-lens-select ' + self.result['ref'], 'lens-test')
        self.page.get.assert_not_awaited()
        self.page.send.assert_awaited_once()

    async def test_pending_upload_allows_later_new_evidence_without_reupload(self):
        self.upload_probe['resultUrls'] = [self.result['url']]
        self.ready_page()
        with patch.object(worker, 'LENS_RESULT_ATTEMPTS', 1):
            await self.upload()
        self.operation_evaluate.side_effect = None
        fresh = {**self.result, 'ref': '@lens-fresh-1', 'url': 'https://www.python.org/'}
        self.operation_evaluate.return_value = json.dumps({
            'url': self.page.url, 'title': 'Google Lens', 'text': 'Visual matches', 'results': [fresh]})
        result = await self.worker.execute('google-lens-results', 'lens-test')
        self.assertEqual(result['status'], 'results')
        self.assertEqual(result['results'], [fresh])
        self.page.send.assert_awaited_once()

    async def test_pending_upload_survives_uncertain_dispatch(self):
        self.upload_probe['resultUrls'] = [self.result['url']]
        self.ready_page()
        self.page.send.side_effect = RuntimeError('connection closed')
        self.assertEqual((await self.upload())['status'], 'uncertain')
        result = await self.worker.execute('google-lens-results', 'lens-test')
        self.assertEqual(result['status'], 'no-results')
        self.assertEqual(result['results'], [])

    async def test_consent_after_extraction_prevents_selection(self):
        self.ready_page()
        await self.upload()
        self.current_document['text'] = 'Before you continue to Google'
        self.operation_evaluate.side_effect = [json.dumps({'valid': True, **self.result})]
        with self.assertRaisesRegex(ValueError, 'LENS_GATE'):
            await self.worker.execute('google-lens-select ' + self.result['ref'], 'lens-test')
        self.page.get.assert_not_awaited()

    async def test_consent_appearing_during_input_resolution_prevents_upload(self):
        self.ready_page()
        async def resolve_input(selector):
            self.current_document['text'] = 'Before you continue to Google'
            return SimpleNamespace(backend_node_id=worker.uc.cdp.dom.BackendNodeId(42))
        self.page.select.side_effect = resolve_input
        with self.assertRaisesRegex(ValueError, 'LENS_GATE'):
            await self.upload()
        self.page.send.assert_not_awaited()

    async def test_observed_traditional_chinese_lens_dialog_is_supported(self):
        self.upload_probe.update(title='Google 圖片', text='使用 Google 智慧鏡頭搜尋所有圖片\n將圖片拖曳到這裡，或上傳檔案')
        self.ready_page()
        self.assertEqual((await self.upload())['status'], 'results')
        self.page.send.assert_awaited_once()

    async def test_prepare_unique_semantic_camera_button_once_before_upload(self):
        closed = {**self.upload_probe, 'title': 'Google 圖片', 'text': '登入\n圖片',
                  'inputCount': 0, 'openerCount': 1, 'openerLabel': '以圖搜尋'}
        self.operation_evaluate.side_effect = [json.dumps(closed), 'true',
            json.dumps(self.upload_probe), 'true', json.dumps({'url': self.page.url, 'results': [self.result]})]
        result = await self.upload()
        self.assertEqual(result['status'], 'results')
        self.page.send.assert_awaited_once()
        clicks = [call for call in self.operation_evaluate.call_args_list if '__piLensOpener' in call.args[0] and '.click()' in call.args[0]]
        self.assertEqual(len(clicks), 1)

    async def test_ambiguous_camera_controls_never_click_or_upload(self):
        self.upload_probe.update(inputCount=0, openerCount=2, openerLabel='以圖搜尋')
        self.operation_evaluate.return_value = json.dumps(self.upload_probe)
        with self.assertRaisesRegex(ValueError, 'LENS_TARGET'):
            await self.upload()
        self.page.send.assert_not_awaited()
        self.assertFalse(any('.click()' in call.args[0] for call in self.operation_evaluate.call_args_list))

    async def test_gate_after_preparation_stops_without_upload_or_second_click(self):
        self.upload_probe.update(inputCount=0, openerCount=1, openerLabel='以圖搜尋')
        async def evaluate(script):
            if '.click()' in script:
                self.current_document['text'] = 'Before you continue to Google'
                return 'true'
            return json.dumps(self.upload_probe)
        self.operation_evaluate.side_effect = evaluate
        with self.assertRaisesRegex(ValueError, 'LENS_GATE'):
            await self.upload()
        self.page.send.assert_not_awaited()
        self.assertEqual(sum('.click()' in call.args[0] for call in self.operation_evaluate.call_args_list), 1)

    async def test_preparation_noop_is_bounded_without_repeated_clicks(self):
        self.upload_probe.update(inputCount=0, openerCount=1, openerLabel='以圖搜尋')
        async def evaluate(script):
            return 'true' if '.click()' in script else json.dumps(self.upload_probe)
        self.operation_evaluate.side_effect = evaluate
        with self.assertRaisesRegex(ValueError, 'LENS_TARGET'):
            await self.upload()
        self.page.send.assert_not_awaited()
        self.assertEqual(sum('.click()' in call.args[0] for call in self.operation_evaluate.call_args_list), 1)
        self.assertLessEqual(self.operation_evaluate.await_count, 8)

    async def test_wrong_host_is_not_an_upload_target(self):
        self.upload_probe['url'] = 'https://example.com'
        self.operation_evaluate.return_value = json.dumps(self.upload_probe)
        with self.assertRaisesRegex(ValueError, 'LENS_TARGET'):
            await self.upload()
        self.page.select.assert_not_awaited()
        self.page.send.assert_not_awaited()

    async def test_cancellation_does_not_replay_upload(self):
        self.ready_page()
        self.page.send.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.upload()
        self.page.send.assert_awaited_once()

    async def test_hanging_probe_has_a_bounded_deadline(self):
        async def hang(*args):
            await asyncio.Event().wait()
        self.worker.probe_page_access_gate.side_effect = hang
        with patch.object(worker, 'LENS_COMMAND_TIMEOUT', 0.01):
            with self.assertRaisesRegex(ValueError, 'LENS_TIMEOUT'):
                await self.upload()
        self.page.send.assert_not_awaited()

    async def test_ambiguous_input_never_uploads(self):
        self.upload_probe['inputCount'] = 2
        self.operation_evaluate.return_value = json.dumps(self.upload_probe)
        with self.assertRaisesRegex(ValueError, 'input'):
            await self.upload()
        self.page.send.assert_not_awaited()

    async def test_external_form_action_never_uploads(self):
        self.upload_probe['formAction'] = 'https://example.com'
        self.operation_evaluate.return_value = json.dumps(self.upload_probe)
        with self.assertRaisesRegex(ValueError, 'target'):
            await self.upload()
        self.page.send.assert_not_awaited()

    async def test_pre_upload_gates_stop(self):
        for reason in ['captcha widget', 'login gate', 'access denied']:
            with self.subTest(reason=reason):
                self.worker.probe_page_access_gate.return_value = reason
                with self.assertRaisesRegex(ValueError, 'LENS_GATE'):
                    await self.upload()
        self.page.send.assert_not_awaited()

    async def test_consent_stops_without_dismissal(self):
        self.upload_probe['text'] = 'Before you continue to Google'
        self.current_document['text'] = 'Before you continue to Google'
        self.operation_evaluate.return_value = json.dumps(self.upload_probe)
        with self.assertRaisesRegex(ValueError, 'LENS_GATE'):
            await self.upload()
        self.page.send.assert_not_awaited()

    async def test_input_replaced_before_dispatch_never_uploads(self):
        self.operation_evaluate.side_effect = [json.dumps(self.upload_probe), 'false']
        with self.assertRaisesRegex(ValueError, 'changed'):
            await self.upload()
        self.page.send.assert_not_awaited()

    async def test_post_upload_gate_does_not_replay_or_navigate(self):
        self.ready_page()
        self.worker.probe_page_access_gate.side_effect = [None, None, 'captcha widget']
        result = await self.upload()
        self.assertEqual(result['status'], 'blocked')
        self.assertTrue(result['uploadAttempted'])
        self.page.send.assert_awaited_once()
        self.page.get.assert_not_awaited()

    async def test_uncertain_upload_failure_is_not_retried(self):
        self.ready_page()
        self.page.send.side_effect = RuntimeError('connection closed')
        result = await self.upload()
        self.assertEqual(result['status'], 'uncertain')
        self.assertIn('Do not re-upload', result['text'])
        self.page.send.assert_awaited_once()

    async def test_results_polling_is_bounded_without_reupload(self):
        self.ready_page()
        with patch.object(self.worker, 'lens_results', AsyncMock(return_value={
                'status': 'no-results', 'results': [], 'text': 'No matches', 'action': 'google-lens-results'})) as poll, \
                patch.object(worker, 'LENS_RESULT_ATTEMPTS', 2), patch.object(worker, 'LENS_POLL_SECONDS', 0):
            result = await self.upload()
        self.assertEqual(result['status'], 'no-results')
        self.assertEqual(poll.await_count, 2)
        self.page.send.assert_awaited_once()

    async def test_select_opens_only_the_requested_second_result(self):
        self.ready_page()
        await self.upload()
        self.operation_evaluate.side_effect = [json.dumps({'valid': True, **self.result}),
                                         json.dumps({'url': self.result['url'], 'title': 'Blue chair', 'text': 'Chair details'})]
        async def navigate(url):
            self.current_document.update(url=url, title='Blue chair', text='Chair details')
        self.page.get.side_effect = navigate
        result = await self.worker.execute('google-lens-select ' + self.result['ref'], 'lens-test')
        self.page.get.assert_awaited_once_with(self.result['url'])
        self.assertEqual(result['selected'], self.result)
        self.assertIn('Chair details', result['text'])

    async def test_stale_or_cross_session_ref_never_navigates(self):
        self.ready_page()
        await self.upload()
        self.operation_evaluate.side_effect = [json.dumps({'valid': False})]
        with self.assertRaisesRegex(ValueError, 'LENS_STALE'):
            await self.worker.execute('google-lens-select ' + self.result['ref'], 'lens-test')
        with self.assertRaisesRegex(ValueError, 'LENS_STALE'):
            await self.worker.execute('google-lens-select ' + self.result['ref'], 'other-session')
        self.page.get.assert_not_awaited()


class LensJavaScriptTests(unittest.TestCase):
    def run_node(self, source, suffix='.js'):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ('lens-fixture' + suffix)
            path.write_text(source)
            result = subprocess.run(['node', '--experimental-strip-types', str(path)],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_real_extraction_and_node_bound_refs_survive_reordering_not_replacement(self):
        source = r'''
const assert = require('node:assert/strict');
global.window = {};
global.location = {href: 'https://lens.google.com/'};
global.getComputedStyle = () => ({display: 'block', visibility: 'visible'});
function anchor(title, href) {
  return {innerText: title, href, isConnected: true, title: '',
    parentElement: {innerText: title + ' description'},
    closest: () => null, hasAttribute: () => false,
    querySelector: () => ({alt: title}), getAttribute: () => '',
    getBoundingClientRect: () => ({width: 100, height: 100})};
}
const first = anchor('Unrelated red chair', 'https://example.com');
const desired = anchor('Requested blue chair', 'https://www.python.org/');
const duplicate = anchor('Duplicate blue chair', desired.href);
const ui = anchor('Google UI', location.href);
// Exact opaque href observed in the live red-circle results; never decode/rewrite.
const redirect = anchor('File:Red dot.svg', 'https://www.google.com/goto?url=CAESZwHrOzAVnCYtvjtVD10VOlQ7xTNpXEnEeUzLRQ9ia_HDiQykd_PYwLeQG9a37LQIFJpGbaT4o78jbvKunYd7LurZv7JnYl0uLQMRIALBgSfL-uR2eFz5h7fIj2Wpe24CZxQoeXuKZlo');
const hidden = anchor('Hidden', 'https://github.com/ultrafunkamsterdam/nodriver');
hidden.getBoundingClientRect = () => ({width: 0, height: 0});
let anchors = [first, desired, duplicate, ui, hidden, redirect];
global.document = {title: 'Google Lens', body: {innerText: 'Visual matches'},
  querySelectorAll: () => anchors};
const rows = JSON.parse(eval(EXTRACT)).results;
assert.equal(rows.length, 3);
assert.equal(rows[2].url, redirect.href);
assert.equal(rows[2].title, 'File:Red dot.svg');
assert.equal(rows[1].title, 'Requested blue chair');
assert.equal(rows[1].url, desired.href);
const select = () => JSON.parse(eval(SELECT.replace('__PI_LENS_REF__', JSON.stringify(rows[1].ref))));
anchors.reverse();
assert.equal(select().url, desired.href);
desired.href = first.href;
assert.equal(select().valid, false);
desired.href = rows[1].url;
desired.innerText = 'Changed target';
assert.equal(select().valid, false);
desired.innerText = rows[1].title;
desired.isConnected = false;
assert.equal(select().valid, false);
desired.isConnected = true;
location.href += '?changed';
assert.equal(select().valid, false);
'''
        self.run_node('const EXTRACT = ' + json.dumps(worker.LENS_RESULTS_JS) + ';\n' +
                      'const SELECT = ' + json.dumps(worker.LENS_SELECT_JS) + ';\n' + source)

    def test_real_upload_probe_discovers_unique_camera_and_dynamic_hidden_input(self):
        source = r'''
const assert = require('node:assert/strict');
global.window = {};
global.location = {href: 'https://images.google.com/'};
global.getComputedStyle = () => ({display:'block', visibility:'visible'});
const camera = {disabled:false, isConnected:true,
  getAttribute: name => name === 'aria-label' ? '以圖搜尋' : null,
  getBoundingClientRect: () => ({width:24,height:24})};
let controls = [camera], inputs = [];
global.document = {title:'Google 圖片', body:{innerText:'登入 圖片'},
  querySelectorAll: selector => selector === 'input[type="file"]' ? inputs :
    selector === 'a[href]' ? [] : controls};
let probe = JSON.parse(eval(PROBE));
assert.equal(probe.openerCount, 1);
assert.equal(probe.openerLabel, '以圖搜尋');
assert.equal(probe.inputCount, 0);
controls.push({...camera});
assert.equal(JSON.parse(eval(PROBE)).openerCount, 2);
controls = [camera];
inputs = [{disabled:false,accept:'image/jpeg,image/png',form:null,setAttribute:()=>{}}];
assert.equal(JSON.parse(eval(PROBE)).inputCount, 1);
'''
        self.run_node('const PROBE = ' + json.dumps(worker.LENS_UPLOAD_PROBE_JS) + ';\n' + source)

    def test_transport_failure_never_replays_lens_but_preserves_other_retries(self):
        source = (Path(__file__).resolve().parents[1] / 'index.ts').read_text()
        class_source = source[source.index('class NodriverWorker {'):source.index('export default function')]
        self.run_node(class_source + r'''
async function check(command, expected) {
  const instance = new NodriverWorker();
  let calls = 0;
  instance.sendRequest = async () => { calls++; throw new Error('connection closed'); };
  try { await instance.request(command, 'isolated'); } catch (_) {}
  if (calls !== expected) throw new Error(`${command}: expected ${expected} send(s), got ${calls}`);
}
await check('google-lens "/tmp/image.png"', 1);
await check('google-lens-select @lens-result-2', 1);
await check(' GOOGLE-LENS "/tmp/image.png"', 1);
await check('"google-lens" "/tmp/image.png"', 1);
await check("'google-lens' '/tmp/image.png'", 1);
await check('google-"lens" "/tmp/image.png"', 1);
await check(String.raw`google\-lens "/tmp/image.png"`, 1);
await check('"google-lens-select" @lens-result-2', 1);
await check('snapshot -i', 2);
''', suffix='.mts')


if __name__ == '__main__':
    unittest.main()
