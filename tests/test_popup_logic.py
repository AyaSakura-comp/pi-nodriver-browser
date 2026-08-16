import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from worker import BrowserWorker


class NativeClickCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_target_does_not_leave_mouse_click_waiting_forever(self):
        worker = BrowserWorker()
        page = SimpleNamespace()

        async def hanging_click(_x, _y):
            await asyncio.Event().wait()

        async def update_targets():
            worker.browser.tabs.clear()

        page.mouse_click = hanging_click
        worker.browser = SimpleNamespace(tabs=[page], update_targets=update_targets)

        completed = await worker.mouse_click_allowing_target_close(page, 10, 20, timeout_seconds=0.01)

        self.assertFalse(completed)


class DownloadSessionIsolationTests(unittest.TestCase):
    def test_download_events_and_listings_stay_with_the_initiating_session(self):
        worker = BrowserWorker()
        with tempfile.TemporaryDirectory() as temp_dir:
            worker.download_dir = Path(temp_dir)
            worker.download_frame_sessions = {'frame-a': 'session-a', 'frame-b': 'session-b'}
            worker.on_download_will_begin(SimpleNamespace(
                guid='guid-a', frame_id='frame-a', url='https://example.test/a.zip',
                suggested_filename='a.zip',
            ))
            worker.on_download_will_begin(SimpleNamespace(
                guid='guid-b', frame_id='frame-b', url='https://example.test/b.zip',
                suggested_filename='b.zip',
            ))

            session_a = worker.list_downloads(10, session_id='session-a')
            session_b = worker.list_downloads(10, session_id='session-b')

        self.assertEqual([item['name'] for item in session_a], ['a.zip'])
        self.assertEqual([item['name'] for item in session_b], ['b.zip'])


    def test_unknown_download_frame_is_quarantined_instead_of_using_global_route(self):
        worker = BrowserWorker()
        worker.download_route_session = 'session-b'

        worker.on_download_will_begin(SimpleNamespace(
            guid='unknown-download', frame_id='unknown-frame',
            url='https://example.test/unknown.bin', suggested_filename='unknown.bin',
        ))

        self.assertIsNone(worker.downloads['unknown-download']['sessionId'])
        self.assertEqual(worker.list_downloads(10, 'session-b'), [])
        self.assertEqual(worker.list_downloads(10, '__unowned__'), [])

    def test_dynamic_iframe_inherits_download_ownership_from_its_parent_frame(self):
        worker = BrowserWorker()
        worker.download_frame_sessions = {'parent-frame': 'session-a'}

        worker.on_frame_attached(SimpleNamespace(
            frame_id='child-frame', parent_frame_id='parent-frame'
        ))
        worker.on_download_will_begin(SimpleNamespace(
            guid='iframe-download', frame_id='child-frame',
            url='https://example.test/iframe.pdf', suggested_filename='iframe.pdf',
        ))

        self.assertEqual(worker.downloads['iframe-download']['sessionId'], 'session-a')

    def test_popup_target_inherits_download_ownership_from_its_opener(self):
        worker = BrowserWorker()
        worker.download_target_sessions = {'opener-target': 'session-a'}

        worker.on_target_created(SimpleNamespace(target_info=SimpleNamespace(
            target_id='popup-target', opener_id='opener-target'
        )))
        worker.on_download_will_begin(SimpleNamespace(
            guid='popup-download', frame_id='popup-target',
            url='https://example.test/popup.pdf', suggested_filename='popup.pdf',
        ))

        self.assertEqual(worker.downloads['popup-download']['sessionId'], 'session-a')

    def test_configured_download_directory_cannot_have_a_symlinked_ancestor(self):
        worker = BrowserWorker()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outside = root / 'outside'
            outside.mkdir()
            alias = root / 'alias'
            alias.symlink_to(outside, target_is_directory=True)
            worker.download_dir = alias / 'downloads'

            with self.assertRaisesRegex(ValueError, 'symlink'):
                worker.session_download_dir('session-a')

    def test_session_download_directory_cannot_be_replaced_with_a_symlink(self):
        worker = BrowserWorker()
        with tempfile.TemporaryDirectory() as temp_dir:
            worker.download_dir = Path(temp_dir)
            victim_dir = worker.session_download_dir('session-b')
            session_a_dir = worker.session_download_dir('session-a')
            session_a_dir.rmdir()
            session_a_dir.symlink_to(victim_dir, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, 'symlink'):
                worker.session_download_dir('session-a')

    def test_completed_download_rejects_a_symlink_to_another_session_file(self):
        worker = BrowserWorker()
        with tempfile.TemporaryDirectory() as temp_dir:
            worker.download_dir = Path(temp_dir)
            victim = worker.session_download_dir('session-b') / 'secret.txt'
            victim.write_text('secret')
            malicious = worker.download_dir / 'reported.txt'
            malicious.symlink_to(victim)
            record = {'sessionId': 'session-a', 'filename': 'reported.txt'}

            with self.assertRaisesRegex(ValueError, 'symlink'):
                worker.place_completed_download(record, malicious)

            self.assertEqual(victim.read_text(), 'secret')
            self.assertTrue(malicious.is_symlink())

    def test_completed_file_is_rehomed_to_the_attributed_session_directory(self):
        worker = BrowserWorker()
        with tempfile.TemporaryDirectory() as temp_dir:
            worker.download_dir = Path(temp_dir)
            worker.download_frame_sessions = {'frame-a': 'session-a'}
            worker.on_download_will_begin(SimpleNamespace(
                guid='guid-a', frame_id='frame-a', url='https://example.test/a.zip',
                suggested_filename='a.zip',
            ))
            misplaced = worker.session_download_dir('session-b') / 'a.zip'
            misplaced.write_bytes(b'archive')

            worker.on_download_progress(SimpleNamespace(
                guid='guid-a', state='completed', received_bytes=7,
                total_bytes=7, file_path=str(misplaced),
            ))

            final_path = Path(worker.downloads['guid-a']['path'])
            self.assertEqual(final_path.parent, worker.session_download_dir('session-a'))
            self.assertTrue(final_path.is_file())
            self.assertFalse(misplaced.exists())


class DownloadListingTests(unittest.TestCase):
    def test_active_downloads_are_visible_with_progress(self):
        worker = BrowserWorker()
        with tempfile.TemporaryDirectory() as temp_dir:
            worker.download_dir = Path(temp_dir)
            worker.downloads['guid-1'] = {
                'guid': 'guid-1',
                'filename': 'large-model.zip',
                'state': 'inProgress',
                'receivedBytes': 25,
                'totalBytes': 100,
                'startedAt': 123,
                'path': None,
                'url': 'https://example.test/large-model.zip',
            }

            items = worker.list_downloads(10)

        self.assertEqual(items[0]['name'], 'large-model.zip')
        self.assertEqual(items[0]['state'], 'downloading')
        self.assertEqual(items[0]['progress'], 25)


class PopupOwnershipTests(unittest.TestCase):
    def tab(self, target_id, opener_id=None):
        return SimpleNamespace(target=SimpleNamespace(target_id=target_id, opener_id=opener_id))

    def test_popup_must_name_the_clicking_page_as_its_opener(self):
        session_a = self.tab('session-a')
        session_b = self.tab('session-b')
        popup = self.tab('popup', opener_id='session-a')

        self.assertTrue(BrowserWorker.is_owned_popup(session_a, popup))
        self.assertFalse(BrowserWorker.is_owned_popup(session_b, popup))


if __name__ == '__main__':
    unittest.main()
