import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = '__PI_NODRIVER__'


@unittest.skipUnless(os.environ.get('RUN_BROWSER_INTEGRATION') == '1', 'browser integration test')
class WorkerIntegrationTests(unittest.TestCase):
    def setUp(self):
        python = os.environ.get('NODRIVER_PYTHON', str(ROOT / '.venv/bin/python'))
        self.temp_dir = tempfile.TemporaryDirectory()
        env = {**os.environ, 'PI_NODRIVER_PROFILE': str(Path(self.temp_dir.name) / 'profile')}
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


if __name__ == '__main__':
    unittest.main()
