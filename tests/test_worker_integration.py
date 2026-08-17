import functools
import http.server
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = '__PI_NODRIVER__'


class QuietSimpleHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, _format, *_args):
        pass


@unittest.skipUnless(os.environ.get('RUN_BROWSER_INTEGRATION') == '1', 'browser integration test')
class WorkerIntegrationTests(unittest.TestCase):
    def setUp(self):
        python = os.environ.get('NODRIVER_PYTHON', str(ROOT / '.venv/bin/python'))
        self.temp_dir = tempfile.TemporaryDirectory()
        self.download_dir = Path(self.temp_dir.name) / 'downloads'
        env = {
            **os.environ,
            'PI_NODRIVER_PROFILE': str(Path(self.temp_dir.name) / 'profile'),
            'PI_NODRIVER_DOWNLOAD_DIR': str(self.download_dir),
        }
        self.proc = subprocess.Popen(
            ['xvfb-run', '-a', '-s', '-screen 0 1280x900x24', python, str(ROOT / 'worker.py')],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
            env=env,
        )

    def tearDown(self):
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        if self.proc.poll() is None:
            self.proc.wait(timeout=10)
        for _ in range(20):
            try:
                os.killpg(self.proc.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.killpg(self.proc.pid, signal.SIGKILL)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if stream:
                stream.close()
        self.temp_dir.cleanup()

    def command(self, command):
        request_id = getattr(self, '_request_id', 0) + 1
        self._request_id = request_id
        self.proc.stdin.write(json.dumps({'id': request_id, 'command': command}) + '\n')
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                stderr = self.proc.stderr.read()
                self.fail(f'worker exited before response: {stderr}')
            if line.startswith(MARKER):
                response = json.loads(line[len(MARKER):])
                self.assertEqual(response['id'], request_id)
                if not response.get('ok'):
                    self.fail(response.get('error', 'worker command failed'))
                return response

    def test_dismisses_cookie_and_marketing_overlays_safely(self):
        fixture_url = (ROOT / 'tests/fixture_overlays.html').as_uri()
        self.command(f'open {fixture_url}')

        result = self.command('dismiss overlays --cookies=accept')['text']
        self.assertIn('cookie', result)
        self.assertIn('同意', result)
        self.assertIn('不用，謝謝', result)

        page_text = self.command('get text')['text']
        self.assertNotIn('網站使用了 Cookie', page_text)
        self.assertNotIn('9 折優惠', page_text)
        self.assertIn('Product survey', page_text)
        self.assertIn('Next step', page_text)
        self.assertNotIn('Next step', result)
        self.assertIn('Buy product', page_text)

    def open_fixture(self):
        fixture_url = (ROOT / 'tests/fixture.html').as_uri()
        self.command(f'open {fixture_url}')

    def status(self):
        return self.command('get text')['text']

    def test_opens_snapshots_clicks_and_reads_page(self):
        fixture_url = (ROOT / 'tests/fixture.html').as_uri()
        self.command(f'open {fixture_url}')
        snapshot = self.command('snapshot -i')['text']
        self.assertIn('@e1', snapshot)
        self.assertIn('Search term', snapshot)
        self.assertIn('Go now', snapshot)

        button_line = next(line for line in snapshot.splitlines() if 'Go now' in line)
        button_ref = button_line.split()[0]
        self.command(f'click {button_ref}')
        page_text = self.command('get text')['text']
        self.assertIn('clicked', page_text)

        self.command(f'open {fixture_url}')
        snapshot = self.command('snapshot -i')['text']
        button_ref = next(line for line in snapshot.splitlines() if 'Go now' in line).split()[0]
        self.command(f'click-js {button_ref}')
        time.sleep(0.2)
        page_text = self.command('get text')['text']
        self.assertIn('clicked', page_text)

    def test_snapshot_lists_only_interactive_objects_in_the_current_viewport(self):
        fixture_url = (ROOT / 'tests/fixture_viewport.html').as_uri()
        self.command(f'open {fixture_url}')

        top_snapshot = self.command('snapshot -i')['text']
        self.assertIn('Top viewport action', top_snapshot)
        self.assertNotIn('Middle viewport action', top_snapshot)
        self.assertNotIn('Bottom viewport action', top_snapshot)
        top_ref = next(line for line in top_snapshot.splitlines() if 'Top viewport action' in line).split()[0]
        self.command(f'click {top_ref}')
        self.assertIn('top-clicked', self.command('get text')['text'])

        self.command('scroll down 650')
        middle_snapshot = self.command('snapshot -i')['text']
        self.assertNotIn('Top viewport action', middle_snapshot)
        self.assertNotIn('Middle viewport action', middle_snapshot)
        self.assertIn('Bottom viewport action', middle_snapshot)
        bottom_ref = next(line for line in middle_snapshot.splitlines() if 'Bottom viewport action' in line).split()[0]
        self.command(f'click {bottom_ref}')
        self.assertIn('bottom-clicked', self.command('get text')['text'])

    def test_full_snapshot_is_visual_only_and_prompts_scroll_exploration(self):
        fixture_url = (ROOT / 'tests/fixture_viewport.html').as_uri()
        self.command(f'open {fixture_url}')

        result = self.command('snapshot -i --full')

        self.assertEqual(result['action'], 'snapshot-full-vision')
        self.assertEqual(result['count'], 0)
        self.assertNotIn('@e', result['text'])
        self.assertIn('Visual overview only', result['text'])
        self.assertIn('scroll down', result['text'])
        self.assertTrue(Path(result['screenshotPath']).is_file())
        self.assertGreater(Path(result['screenshotPath']).stat().st_size, 0)

    def test_download_info_describes_a_snapshot_target_without_clicking(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        download_ref = next(line for line in snapshot.splitlines() if 'Download sample report' in line).split()[0]

        result = self.command(f'download-info {download_ref}')

        self.assertIn('sample-report.txt', result['text'])
        self.assertIn('text/plain', result['text'])
        self.assertIn('download_sample.txt', result['url'])

    def test_downloads_lists_recent_files_without_opening_a_page(self):
        self.download_dir.mkdir(parents=True)
        (self.download_dir / 'older.csv').write_text('old')
        time.sleep(0.01)
        (self.download_dir / 'latest.pdf').write_bytes(b'%PDF-fixture')

        result = self.command('downloads 5')

        self.assertIn('latest.pdf', result['text'])
        self.assertIn('older.csv', result['text'])
        self.assertLess(result['text'].index('latest.pdf'), result['text'].index('older.csv'))

    def test_download_latest_returns_the_newest_completed_file(self):
        self.download_dir.mkdir(parents=True)
        (self.download_dir / 'older.csv').write_text('old')
        time.sleep(0.01)
        expected = self.download_dir / 'latest.pdf'
        expected.write_bytes(b'%PDF-fixture')

        result = self.command('download-latest')

        self.assertEqual(Path(result['downloadPath']), expected)
        self.assertIn('latest.pdf', result['text'])
        self.assertEqual(result['mimeType'], 'application/pdf')

    def test_wait_download_reports_a_file_started_by_a_normal_click(self):
        handler = functools.partial(
            QuietSimpleHTTPRequestHandler,
            directory=str(ROOT / 'tests'),
        )
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            self.command(f'open http://127.0.0.1:{server.server_port}/fixture.html')
            snapshot = self.command('snapshot -i')['text']
            download_ref = next(line for line in snapshot.splitlines() if 'Download sample report' in line).split()[0]
            self.command(f'click {download_ref}')

            result = self.command('wait-download 5000')

            self.assertEqual(Path(result['downloadPath']).name, 'sample-report.txt')
            self.assertIn('completed', result['text'].lower())
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    def test_download_click_waits_for_a_completed_file(self):
        handler = functools.partial(
            QuietSimpleHTTPRequestHandler,
            directory=str(ROOT / 'tests'),
        )
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            self.command(f'open http://127.0.0.1:{server.server_port}/fixture.html')
            snapshot = self.command('snapshot -i')['text']
            download_ref = next(line for line in snapshot.splitlines() if 'Download sample report' in line).split()[0]

            result = self.command(f'download {download_ref} 5000')

            output = Path(result['downloadPath'])
            self.assertEqual(output.parent, self.download_dir)
            self.assertEqual(output.name, 'sample-report.txt')
            self.assertEqual(output.read_text(), 'Pi Nodriver download fixture\n')
            self.assertIn('completed', result['text'].lower())
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    def test_click_returns_quickly_after_synchronous_update(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        button_ref = next(line for line in snapshot.splitlines() if 'Go now' in line).split()[0]

        started = time.monotonic()
        self.command(f'click {button_ref}')

        self.assertLess(time.monotonic() - started, 0.75)
        self.assertIn('clicked', self.status())

    def test_click_caps_wait_for_continuously_mutating_pages(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        button_ref = next(line for line in snapshot.splitlines() if 'Start noisy updates' in line).split()[0]

        started = time.monotonic()
        self.command(f'click {button_ref}')

        self.assertLess(time.monotonic() - started, 0.9)

    def test_click_switches_to_a_new_tab_without_fixed_two_second_wait(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        link_ref = next(line for line in snapshot.splitlines() if 'Open report' in line).split()[0]

        started = time.monotonic()
        result = self.command(f'click {link_ref}')

        self.assertLess(time.monotonic() - started, 1.25)
        self.assertIn('fixture_new_tab.html', result['url'])
        self.assertIn('New tab report ready', self.status())

    def test_popup_close_automatically_returns_to_its_opener(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        popup_ref = next(line for line in snapshot.splitlines() if 'Open OAuth login' in line).split()[0]
        self.command(f'click {popup_ref}')
        popup_snapshot = self.command('snapshot -i')['text']
        complete_ref = next(line for line in popup_snapshot.splitlines() if 'Complete login' in line).split()[0]

        result = self.command(f'click {complete_ref}')

        self.assertIn('fixture.html', result['url'])
        self.assertIn('oauth-complete', self.status())

    def test_wait_popup_is_idempotent_after_click_already_switched(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        popup_ref = next(line for line in snapshot.splitlines() if 'Open OAuth login' in line).split()[0]
        self.command(f'click {popup_ref}')

        result = self.command('wait-popup 100')

        self.assertIn('fixture_new_tab.html', result['url'])

    def test_wait_popup_detects_a_delayed_child_of_an_active_popup(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        popup_ref = next(line for line in snapshot.splitlines() if 'Open OAuth login' in line).split()[0]
        self.command(f'click {popup_ref}')
        popup_snapshot = self.command('snapshot -i')['text']
        nested_ref = next(line for line in popup_snapshot.splitlines() if 'Open nested OAuth' in line).split()[0]
        self.command(f'click-js {nested_ref}')

        result = self.command('wait-popup 2000')

        self.assertIn('nested=1', result['url'])

    def test_wait_popup_switches_to_a_delayed_oauth_window(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        popup_ref = next(line for line in snapshot.splitlines() if 'Open delayed report' in line).split()[0]
        self.command(f'click-js {popup_ref}')

        result = self.command('wait-popup 2000')

        self.assertIn('fixture_new_tab.html', result['url'])
        self.assertIn('New tab report ready', self.status())

    def test_switch_opener_returns_to_source_without_closing_popup(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        popup_ref = next(line for line in snapshot.splitlines() if 'Open OAuth login' in line).split()[0]
        self.command(f'click {popup_ref}')

        result = self.command('switch opener')

        self.assertIn('fixture.html', result['url'])
        self.assertIn('Go now', self.status())

    def test_next_command_recovers_after_popup_closes_between_commands(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        popup_ref = next(line for line in snapshot.splitlines() if 'Open OAuth login' in line).split()[0]
        self.command(f'click {popup_ref}')
        popup_snapshot = self.command('snapshot -i')['text']
        complete_ref = next(line for line in popup_snapshot.splitlines() if 'Complete delayed login' in line).split()[0]
        self.command(f'click {complete_ref}')
        time.sleep(1)

        page_text = self.status()

        self.assertIn('oauth-complete', page_text)

    def test_wait_popup_close_is_idempotent_after_automatic_recovery(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        popup_ref = next(line for line in snapshot.splitlines() if 'Open OAuth login' in line).split()[0]
        self.command(f'click {popup_ref}')
        popup_snapshot = self.command('snapshot -i')['text']
        complete_ref = next(line for line in popup_snapshot.splitlines() if 'Complete delayed login' in line).split()[0]
        self.command(f'click {complete_ref}')
        time.sleep(1)

        result = self.command('wait-popup-close 100')

        self.assertIn('fixture.html', result['url'])

    def test_wait_popup_close_returns_to_opener_after_oauth_finishes(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        popup_ref = next(line for line in snapshot.splitlines() if 'Open OAuth login' in line).split()[0]
        self.command(f'click {popup_ref}')
        popup_snapshot = self.command('snapshot -i')['text']
        complete_ref = next(line for line in popup_snapshot.splitlines() if 'Complete delayed login' in line).split()[0]
        self.command(f'click {complete_ref}')

        result = self.command('wait-popup-close 2000')

        self.assertIn('fixture.html', result['url'])
        self.assertIn('oauth-complete', self.status())

    def test_click_waits_for_a_scripted_delayed_new_tab(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        button_ref = next(line for line in snapshot.splitlines() if 'Open delayed report' in line).split()[0]

        result = self.command(f'click {button_ref}')

        self.assertIn('fixture_new_tab.html', result['url'])
        self.assertIn('New tab report ready', self.status())

    def test_click_detects_a_delayed_new_tab_handler_on_an_ancestor(self):
        self.open_fixture()
        result = self.command('click-css "#nested-delayed-new-tab span"')

        self.assertIn('fixture_new_tab.html', result['url'])
        self.assertIn('New tab report ready', self.status())

    def test_click_waits_for_a_delayed_named_form_target(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        button_ref = next(line for line in snapshot.splitlines() if 'Open named report' in line).split()[0]

        result = self.command(f'click {button_ref}')

        self.assertIn('fixture_new_tab.html', result['url'])
        self.assertIn('New tab report ready', self.status())

    def test_click_switches_to_an_existing_named_form_target(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        seed_ref = next(line for line in snapshot.splitlines() if 'Seed named report' in line).split()[0]
        self.command(f'click-js {seed_ref}')
        time.sleep(0.3)
        snapshot = self.command('snapshot -i')['text']
        form_ref = next(line for line in snapshot.splitlines() if 'Open named report' in line).split()[0]

        result = self.command(f'click {form_ref}')

        self.assertIn('fixture_new_tab.html', result['url'])
        self.assertIn('New tab report ready', self.status())

    def test_snapshot_and_ref_click_support_custom_div_controls(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        custom_lines = [line for line in snapshot.splitlines() if 'Custom checkout' in line]
        self.assertEqual(len(custom_lines), 1)
        self.command(f'click {custom_lines[0].split()[0]}')
        self.assertIn('custom-clicked', self.status())

    def test_snapshot_and_ref_click_support_open_shadow_dom(self):
        self.open_fixture()
        snapshot = self.command('snapshot -i')['text']
        shadow_line = next(line for line in snapshot.splitlines() if 'Shadow action' in line)
        self.command(f'click {shadow_line.split()[0]}')
        self.assertIn('shadow-clicked', self.status())

    def test_click_text_finds_non_semantic_control(self):
        self.open_fixture()
        self.command('click-text "加入購物車"')
        self.assertIn('text-clicked', self.status())

    def test_click_css_finds_non_semantic_control(self):
        self.open_fixture()
        self.command('click-css "#custom"')
        self.assertIn('custom-clicked', self.status())

    def test_click_coordinates_uses_viewport_coordinates(self):
        self.open_fixture()
        self.command('click 360 330')
        self.assertIn('coordinate-clicked', self.status())


if __name__ == '__main__':
    unittest.main()
