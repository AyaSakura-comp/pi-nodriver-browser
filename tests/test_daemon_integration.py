import functools
import http.server
import json
import os
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = os.environ.get('NODRIVER_PYTHON', str(ROOT / '.venv/bin/python'))
MARKER = '__PI_NODRIVER__'


class QuietSimpleHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, _format, *_args):
        pass


@unittest.skipUnless(os.environ.get('RUN_BROWSER_INTEGRATION') == '1', 'browser integration test')
class DaemonIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        temp = Path(self.temp_dir.name)
        self.socket_path = temp / 'browser.sock'
        self.download_dir = temp / 'downloads'
        env = {
            **os.environ,
            'PI_NODRIVER_PROFILE': str(temp / 'profile'),
            'PI_NODRIVER_DOWNLOAD_DIR': str(self.download_dir),
            'PI_NODRIVER_COMMAND_TIMEOUT': '3' if self._testMethodName == 'test_command_timeout_releases_session' else '30',
        }
        self.proc = subprocess.Popen(
            [
                'xvfb-run', '-a', '-s', '-screen 0 1440x1000x24',
                PYTHON, str(ROOT / 'worker.py'), '--server', str(self.socket_path),
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        for _ in range(100):
            if self.socket_path.exists():
                break
            if self.proc.poll() is not None:
                self.fail(f'daemon exited early: {self.proc.stderr.read()}')
            time.sleep(0.1)
        else:
            self.fail('daemon socket was not created')

    def tearDown(self):
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            self.proc.wait(timeout=10)
        for _ in range(20):
            try:
                os.killpg(self.proc.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.killpg(self.proc.pid, signal.SIGKILL)
        if self.proc.stderr:
            self.proc.stderr.close()
        self.temp_dir.cleanup()

    def command_raw(self, command, request_id=1, session_id='test-session'):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(self.socket_path))
            request = {'id': request_id, 'command': command, 'sessionId': session_id}
            client.sendall((json.dumps(request) + '\n').encode())
            stream = client.makefile()
            line = stream.readline()
        self.assertTrue(line.startswith(MARKER), line)
        return json.loads(line[len(MARKER):])

    def command(self, command, request_id=1, session_id='test-session'):
        response = self.command_raw(command, request_id=request_id, session_id=session_id)
        self.assertTrue(response.get('ok'), response.get('error'))
        return response

    def test_long_wait_in_one_session_does_not_block_another(self):
        fixture_url = (ROOT / 'tests/fixture.html').as_uri()
        self.command(f'open {fixture_url}', session_id='session-a')
        self.command(f'open {fixture_url}', request_id=2, session_id='session-b')
        started = threading.Event()

        def wait_in_session_a():
            started.set()
            return self.command('wait 2500', request_id=3, session_id='session-a')

        thread = threading.Thread(target=wait_in_session_a)
        thread.start()
        started.wait(timeout=1)
        time.sleep(0.2)
        before = time.monotonic()
        response = self.command('get text', request_id=4, session_id='session-b')
        elapsed = time.monotonic() - before
        thread.join(timeout=5)

        self.assertIn('Go now', response['text'])
        self.assertLess(elapsed, 1.5)
        self.assertFalse(thread.is_alive())

    def test_command_timeout_releases_session(self):
        fixture_url = (ROOT / 'tests/fixture.html').as_uri()
        self.command(f'open {fixture_url}', session_id='session-a')

        timed_out = self.command_raw('wait 5000', request_id=2, session_id='session-a')
        recovered = self.command('get text', request_id=3, session_id='session-a')

        self.assertFalse(timed_out['ok'])
        self.assertIn('timed out', timed_out['error'])
        self.assertIn('Go now', recovered['text'])

    def test_cancel_interrupts_running_command_and_releases_session(self):
        fixture_url = (ROOT / 'tests/fixture.html').as_uri()
        self.command(f'open {fixture_url}', session_id='session-a')

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3)
            client.connect(str(self.socket_path))
            stream = client.makefile()
            wait_request = {'id': 2, 'command': 'wait 5000', 'sessionId': 'session-a'}
            cancel_request = {'id': 3, 'cancelId': 2, 'sessionId': 'session-a'}
            client.sendall((json.dumps(wait_request) + '\n').encode())
            time.sleep(0.2)
            started = time.monotonic()
            client.sendall((json.dumps(cancel_request) + '\n').encode())
            responses = {}
            while len(responses) < 2:
                line = stream.readline()
                self.assertTrue(line.startswith(MARKER), line)
                response = json.loads(line[len(MARKER):])
                responses[response['id']] = response

        recovered = self.command('get text', request_id=4, session_id='session-a')

        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(responses[2]['ok'])
        self.assertIn('cancelled', responses[2]['error'].lower())
        self.assertTrue(responses[3]['ok'])
        self.assertEqual(responses[3]['action'], 'cancel')
        self.assertIn('Go now', recovered['text'])

    def test_sessions_keep_independent_active_pages(self):
        fixture_url = (ROOT / 'tests/fixture.html').as_uri()
        overlay_url = (ROOT / 'tests/fixture_overlays.html').as_uri()
        self.command(f'open {fixture_url}', session_id='session-a')
        self.command('snapshot -i', request_id=2, session_id='session-a')
        self.command('click @e2', request_id=3, session_id='session-a')
        self.command(f'open {overlay_url}', request_id=4, session_id='session-b')

        session_a = self.command('get text', request_id=5, session_id='session-a')
        session_b = self.command('get text', request_id=6, session_id='session-b')
        screenshot = self.command('screenshot', request_id=7, session_id='session-a')

        self.assertIn('clicked', session_a['text'])
        self.assertNotIn('9 折優惠', session_a['text'])
        self.assertIn('9 折優惠', session_b['text'])
        screenshot_path = Path(screenshot['screenshotPath'])
        self.assertTrue(screenshot_path.is_file())
        self.assertGreater(screenshot_path.stat().st_size, 100)

    def test_background_download_uses_the_session_that_opened_its_frame(self):
        handler = functools.partial(
            QuietSimpleHTTPRequestHandler,
            directory=str(ROOT / 'tests'),
        )
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            base_url = f'http://127.0.0.1:{server.server_port}'
            self.command(
                f'open {base_url}/fixture_background_download.html',
                session_id='session-a',
            )
            self.command(
                f'open {base_url}/fixture.html', request_id=2, session_id='session-b'
            )
            snapshot_b = self.command(
                'snapshot -i', request_id=3, session_id='session-b'
            )['text']
            go_ref = next(line for line in snapshot_b.splitlines() if 'Go now' in line).split()[0]
            self.command(f'click {go_ref}', request_id=4, session_id='session-b')

            session_a = self.command(
                'wait-download 5000', request_id=5, session_id='session-a'
            )
            session_b = self.command_raw(
                'download-latest', request_id=6, session_id='session-b'
            )

            self.assertEqual(session_a['filename'], 'background-report.txt')
            self.assertFalse(session_b['ok'])
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    def test_downloaded_files_are_not_visible_to_another_session(self):
        handler = functools.partial(
            QuietSimpleHTTPRequestHandler,
            directory=str(ROOT / 'tests'),
        )
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            self.command(
                f'open http://127.0.0.1:{server.server_port}/fixture.html',
                session_id='session-a',
            )
            snapshot = self.command('snapshot -i', request_id=2, session_id='session-a')['text']
            download_ref = next(line for line in snapshot.splitlines() if 'Download sample report' in line).split()[0]
            downloaded = self.command(
                f'download {download_ref} 5000', request_id=3, session_id='session-a'
            )

            session_b = self.command_raw('download-latest', request_id=4, session_id='session-b')
            session_a = self.command('download-latest', request_id=5, session_id='session-a')

            self.assertFalse(session_b['ok'])
            self.assertEqual(session_a['downloadPath'], downloaded['downloadPath'])
            self.assertTrue(Path(session_a['downloadPath']).is_file())
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    def test_delayed_popup_cannot_be_claimed_by_another_session(self):
        fixture_url = (ROOT / 'tests/fixture.html').as_uri()
        self.command(f'open {fixture_url}', session_id='session-a')
        self.command(f'open {fixture_url}', request_id=2, session_id='session-b')
        snapshot_a = self.command('snapshot -i', request_id=3, session_id='session-a')['text']
        snapshot_b = self.command('snapshot -i', request_id=4, session_id='session-b')['text']
        delayed_ref = next(line for line in snapshot_a.splitlines() if 'Open delayed report' in line).split()[0]
        noisy_ref = next(line for line in snapshot_b.splitlines() if 'Start noisy updates' in line).split()[0]
        session_a_result = {}

        def click_delayed_popup():
            session_a_result.update(self.command(f'click {delayed_ref}', request_id=5, session_id='session-a'))

        popup_thread = threading.Thread(target=click_delayed_popup)
        popup_thread.start()
        time.sleep(0.45)
        session_b = self.command(f'click {noisy_ref}', request_id=6, session_id='session-b')
        popup_thread.join(timeout=5)

        self.assertFalse(popup_thread.is_alive())
        self.assertIn('fixture.html', session_b['url'])
        self.assertIn('fixture_new_tab.html', session_a_result['url'])

    def test_closing_one_session_keeps_other_session_page_alive(self):
        fixture_url = (ROOT / 'tests/fixture.html').as_uri()
        overlay_url = (ROOT / 'tests/fixture_overlays.html').as_uri()
        self.command(f'open {fixture_url}', session_id='session-a')
        self.command(f'open {overlay_url}', request_id=2, session_id='session-b')

        self.command('close', request_id=3, session_id='session-a')
        session_b = self.command('get text', request_id=4, session_id='session-b')

        self.assertIn('9 折優惠', session_b['text'])
        self.assertIsNone(self.proc.poll())

    def test_shutdown_command_stops_daemon_with_persistent_client_connected(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(self.socket_path))
            request = {'id': 1, 'command': 'shutdown', 'sessionId': 'test-session'}
            client.sendall((json.dumps(request) + '\n').encode())
            stream = client.makefile()
            line = stream.readline()
            self.assertTrue(line.startswith(MARKER), line)
            response = json.loads(line[len(MARKER):])
            self.assertEqual(response['action'], 'shutdown')

            self.proc.wait(timeout=10)
            self.assertFalse(self.socket_path.exists())

    def test_shutdown_stops_daemon_with_another_idle_client_connected(self):
        idle_client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        idle_client.connect(str(self.socket_path))
        try:
            response = self.command('shutdown', session_id='shutdown-session')

            self.assertEqual(response['action'], 'shutdown')
            self.proc.wait(timeout=3)
            self.assertFalse(self.socket_path.exists())
        finally:
            idle_client.close()

    def test_stale_ref_guard_requires_a_fresh_snapshot_before_ref_clicks_resume(self):
        fixture_url = (ROOT / 'tests/fixture.html').as_uri()
        overlay_url = (ROOT / 'tests/fixture_overlays.html').as_uri()
        self.command(f'open {fixture_url}')
        old_snapshot = self.command('snapshot -i', request_id=2)['text']
        stale_ref = next(line for line in old_snapshot.splitlines() if 'Go now' in line).split()[0]
        self.command(f'open {overlay_url}', request_id=3)

        recovery = self.command_raw(f'click {stale_ref}', request_id=4)
        guarded_failure = self.command_raw(f'click {stale_ref}', request_id=5)

        self.assertTrue(recovery['ok'])
        self.assertEqual(recovery['action'], 'stale-ref-recovery')
        self.assertIn(f'CLICK NOT PERFORMED: {stale_ref}', recovery['text'])
        self.assertIn('Fresh DOM snapshot:', recovery['text'])
        self.assertTrue(Path(recovery['screenshotPath']).is_file())
        self.assertEqual(Path(recovery['screenshotPath']).suffix, '.jpg')
        self.assertFalse(guarded_failure['ok'])
        self.assertIn('STALE_REF_GUARD', guarded_failure['error'])
        self.assertIn('run exactly: snapshot -i', guarded_failure['error'])

        fresh_snapshot = self.command('snapshot -i', request_id=6)['text']
        fresh_ref = next(line for line in fresh_snapshot.splitlines() if 'Next step' in line).split()[0]
        resumed = self.command(f'click {fresh_ref}', request_id=7)
        self.assertIn('Clicked', resumed['text'])

    def test_browser_survives_client_disconnect(self):
        fixture_url = (ROOT / 'tests/fixture.html').as_uri()
        self.command(f'open {fixture_url}')

        # command() closed the first client socket; a later Pi task reconnects.
        response = self.command('get text', request_id=2)

        self.assertIn('Go now', response['text'])
        self.assertIsNone(self.proc.poll())


if __name__ == '__main__':
    unittest.main()
