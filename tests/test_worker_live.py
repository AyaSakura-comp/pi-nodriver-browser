"""Real Chrome + Xvfb through the worker socket on local fixture pages.

Skipped unless PI_BROWSER_LIVE=1 (needs google-chrome and xvfb-run; ~20 s).
"""
import functools
import http.server
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / 'tests' / 'live' / 'site'
MARKER = b'__PI_NODRIVER__'


@unittest.skipUnless(os.environ.get('PI_BROWSER_LIVE') == '1' and shutil.which('xvfb-run'),
                     'set PI_BROWSER_LIVE=1 (needs Chrome and xvfb-run)')
class WorkerLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path.startswith('/slow'):
                    time.sleep(1.5)  # a popup stays on about:blank until this answers
                return super().do_GET()

        cls.http = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(SITE)))
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.http.server_address[1]}'
        cls.tmp = tempfile.TemporaryDirectory()
        cls.sock = os.path.join(cls.tmp.name, 'w.sock')
        env = {**os.environ, 'PI_NODRIVER_PROFILE': os.path.join(cls.tmp.name, 'profile'),
               'PI_AGENT_DIR': cls.tmp.name}
        cls.proc = subprocess.Popen(['xvfb-run', '-a', '-s', '-screen 0 1366x768x24', sys.executable,
                                     str(ROOT / 'worker.py'), '--server', cls.sock],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
        for _ in range(100):
            if os.path.exists(cls.sock):
                break
            time.sleep(0.1)
        cls.next_id = 0

    @classmethod
    def tearDownClass(cls):
        try:
            cls.request('shutdown')
        except Exception:
            pass
        try:
            cls.proc.wait(20)
        except subprocess.TimeoutExpired:
            os.killpg(cls.proc.pid, 9)
        cls.http.shutdown()
        cls.tmp.cleanup()

    @classmethod
    def request(cls, command, session='live'):
        cls.next_id += 1
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(90)
            client.connect(cls.sock)
            client.sendall((json.dumps({'id': cls.next_id, 'command': command, 'sessionId': session}) + '\n').encode())
            buffer = b''
            while True:
                chunk = client.recv(1 << 20)
                if not chunk:
                    raise ConnectionError('worker closed the connection')
                buffer += chunk
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    if line.startswith(MARKER):
                        response = json.loads(line[len(MARKER):])
                        if response.get('id') == cls.next_id:
                            return response

    def ok(self, command, session='live'):
        response = self.request(command, session)
        self.assertTrue(response.get('ok'), f'{command}: {response.get("error")}')
        return response

    @staticmethod
    def ref(snapshot, pattern):
        match = re.search(r'^\[(e\d+)\] ' + pattern, snapshot, re.M)
        if not match:
            raise AssertionError(f'no element matching {pattern!r} in:\n{snapshot}')
        return match.group(1)

    def test_browsing_flow(self):
        snap = self.ok(f'open {self.base}/fixture.html')['snapshot']
        self.assertIn('Lazy item 2 $200', snap, 'requestAnimationFrame content must render')
        self.assertIn('Visible link', snap)
        self.assertNotIn('Clipped hidden link', snap, 'content clipped by overflow:hidden is not visible')
        self.assertIn('Inner button', snap, 'same-origin iframe content is part of the snapshot')
        # PChome wraps the whole product box in role=button: its text and buttons must be listed.
        self.assertIn('Delivery: 24h到貨, ships from the Taipei warehouse', snap)
        self.ref(snap, r'button "Add to wishlist"')

        selected = self.ok(f'select {self.ref(snap, "select")} "R02 台北車站"')
        self.assertIn('Selected: R10 台北車站', selected['snapshot'])

        covered = self.ref(snap, r'button "Covered button"')
        snap = self.ok(f'click {covered}')['snapshot']
        self.assertIn('covered clicked', snap)

        alert = self.ref(snap, r'button "Save with alert"')
        snap = self.ok(f'click {alert}')['snapshot']
        self.assertIn('after alert', snap)
        self.assertIn('Dialog closed automatically: alert: Saved!', snap)

        inner = self.ref(snap, r'button "Inner button"')
        snap = self.ok(f'click {inner}')['snapshot']
        self.assertIn('Inner clicked', snap)

        link = self.ref(snap, r'link "Open report in new tab"')
        result = self.ok(f'click {link}')
        self.assertIn('/newtab.html', result['url'])
        self.assertIn('opened a new tab', result['snapshot'])
        result = self.ok('back')
        self.assertIn('/fixture.html', result['url'])

        slow = self.ref(result['snapshot'], r'button "Open scripted report"')
        result = self.ok(f'click {slow}')
        self.assertIn('/slow-report.html', result['url'], 'a popup is read after it loads, not on about:blank')
        self.assertIn('Slow report loaded', result['snapshot'])
        result = self.ok('back')
        self.assertIn('/fixture.html', result['url'])

        snap = result['snapshot']
        result = self.ok(f'type {self.ref(snap, "textbox")} 手機散熱器 --submit')
        self.assertIn('/results.html?q=', result['url'])
        self.assertIn('Results for 手機散熱器', result['snapshot'])
        result = self.ok('back')
        self.assertIn('/fixture.html', result['url'])

        # Item links show where they go; menu links do not.
        self.ref(result['snapshot'], r'link "Open report in new tab" → /newtab\.html')
        self.ref(result['snapshot'], r'link "Menu results link"$')
        links = self.ok('get links')['text'].split('\n')
        self.assertIn(f'{self.base}/newtab.html', links)
        self.assertIn('Scroll:', self.ok('scroll down')['snapshot'])
        self.assertIn('/fixture.html', self.ok('press Escape')['url'])

        shot = self.ok('screenshot')
        self.assertTrue(Path(shot['path']).stat().st_size > 1000)
        self.ok('session-cleanup')

    def test_open_dialog_is_listed_first(self):
        snap = self.ok(f'open {self.base}/modal.html', session='modal')['snapshot']
        snap = self.ok(f'click {self.ref(snap, r"button \"地區\"")}', session='modal')['snapshot']
        dialog = snap.split('[dialog]', 1)[1].split('[/dialog]', 1)[0]
        self.assertIn('地區類別選單', dialog)
        self.ref(dialog, r'checkbox "台北市"')
        self.ref(dialog, r'checkbox "新北市"')  # named after its row: the text is a sibling
        self.ref(dialog, r'button "確定"')
        self.ok('session-cleanup', session='modal')

    def test_crawl_and_health(self):
        result = self.ok(f'crawl {self.base}/results.html?q=abc {self.base}/missing-page.html')
        self.assertEqual(result['successCount'], 1)
        self.assertIn('Results for abc', result['text'])
        health = self.ok('browser-health')
        self.assertEqual(health['browser'], 'ok')
        refused = self.request(f'fetch-image {self.base}/missing.png')
        self.assertIn('private address', refused['error'], 'downloads go to public addresses only')
        # The watchdog's own check, against this worker.
        spec = importlib.util.spec_from_file_location('watchdog', ROOT / 'ops' / 'browser-agent-watchdog.py')
        watchdog = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(watchdog)
        with mock.patch.object(watchdog, 'SOCKET', Path(self.sock)):
            status, detail = watchdog.health()
        self.assertEqual(status, 'ok', detail)


if __name__ == '__main__':
    unittest.main()
