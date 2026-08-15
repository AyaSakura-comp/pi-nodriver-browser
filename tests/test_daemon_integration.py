import json
import os
import signal
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = os.environ.get('NODRIVER_PYTHON', str(ROOT / '.venv/bin/python'))
MARKER = '__PI_NODRIVER__'


@unittest.skipUnless(os.environ.get('RUN_BROWSER_INTEGRATION') == '1', 'browser integration test')
class DaemonIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        temp = Path(self.temp_dir.name)
        self.socket_path = temp / 'browser.sock'
        env = {
            **os.environ,
            'PI_NODRIVER_PROFILE': str(temp / 'profile'),
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

    def command(self, command, request_id=1, session_id='test-session'):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(self.socket_path))
            request = {'id': request_id, 'command': command, 'sessionId': session_id}
            client.sendall((json.dumps(request) + '\n').encode())
            stream = client.makefile()
            line = stream.readline()
        self.assertTrue(line.startswith(MARKER), line)
        response = json.loads(line[len(MARKER):])
        self.assertTrue(response.get('ok'), response.get('error'))
        return response

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

    def test_closing_one_session_keeps_other_session_page_alive(self):
        fixture_url = (ROOT / 'tests/fixture.html').as_uri()
        overlay_url = (ROOT / 'tests/fixture_overlays.html').as_uri()
        self.command(f'open {fixture_url}', session_id='session-a')
        self.command(f'open {overlay_url}', request_id=2, session_id='session-b')

        self.command('close', request_id=3, session_id='session-a')
        session_b = self.command('get text', request_id=4, session_id='session-b')

        self.assertIn('9 折優惠', session_b['text'])
        self.assertIsNone(self.proc.poll())

    def test_shutdown_command_stops_daemon(self):
        response = self.command('shutdown')
        self.assertEqual(response['action'], 'shutdown')
        self.proc.wait(timeout=10)
        self.assertFalse(self.socket_path.exists())

    def test_browser_survives_client_disconnect(self):
        fixture_url = (ROOT / 'tests/fixture.html').as_uri()
        self.command(f'open {fixture_url}')

        # command() closed the first client socket; a later Pi task reconnects.
        response = self.command('get text', request_id=2)

        self.assertIn('Go now', response['text'])
        self.assertIsNone(self.proc.poll())


if __name__ == '__main__':
    unittest.main()
