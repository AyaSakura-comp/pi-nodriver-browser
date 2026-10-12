"""Opt-in end-to-end regression for the 2026-10-13 incident (real Chrome, user systemd).

`systemctl --user restart piweb-worker` used to kill the shared Chrome: the worker was a
plain child of a Pi process, so Xvfb and every Chrome helper lived in piweb-worker's
cgroup and received SIGTERM with it. The launcher now gives the browser its own unit.

    RUN_SYSTEMD_INTEGRATION=1 .venv/bin/python -m unittest tests.test_systemd_browser_unit -v

Negative control: with PI_NODRIVER_SYSTEMD=0 (forwarded to the launcher) the worker starts
as the old plain child and this test must fail, because Chrome dies with the parent unit.

Everything is private to the test: socket, profile and downloads live in a temp dir, the
parent unit is pi-test-parent-*, and Xvfb picks a free display.
"""
import http.server
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = b'__PI_NODRIVER__'
ENABLED = (os.environ.get('RUN_SYSTEMD_INTEGRATION') == '1' and shutil.which('systemd-run')
           and shutil.which('xvfb-run') and shutil.which('node'))


class Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'<!doctype html><title>unit test</title><button>Continue</button>'
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def alive(pid):
    try:
        state = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state not in ('Z', 'X')


def chrome_pids(profile):
    pids = []
    for proc in Path('/proc').iterdir():
        if proc.name.isdigit():
            try:
                if str(profile) in (proc / 'cmdline').read_text(errors='replace'):
                    pids.append(int(proc.name))
            except OSError:
                pass
    return pids


def literal(arg):
    """Escape '$' for this test's own systemd-run, which would otherwise expand ${HOME}."""
    return str(arg).replace('$', '$$')


def chrome_root(profile):
    """The browser process: Chrome rewrites argv into one string; helpers carry --type=."""
    for pid in chrome_pids(profile):
        try:
            proc = Path(f'/proc/{pid}')
            if (proc / 'comm').read_text().strip() == 'chrome' and '--type=' not in (proc / 'cmdline').read_text(errors='replace'):
                return pid
        except OSError:
            pass
    return None


def unit_of(pid):
    try:
        cgroup = Path(f'/proc/{pid}/cgroup').read_text().strip()
    except OSError:
        return ''
    match = re.search(r'/([^/]+\.(?:service|scope))$', cgroup)
    return match.group(1) if match else ''


@unittest.skipUnless(ENABLED, 'opt-in: RUN_SYSTEMD_INTEGRATION=1 with user systemd, xvfb-run and node')
class SystemdBrowserUnitTests(unittest.TestCase):
    def setUp(self):
        top = Path(tempfile.mkdtemp(prefix='pi-systemd-browser-'))
        self.addCleanup(shutil.rmtree, top, True)
        # A literal '${HOME}' in the socket path proves the launcher escapes '$': ExecStart
        # expands ${VAR} anywhere in an argument (bare $VAR only as a whole word), so an
        # unescaped path would put the socket under the real home directory instead.
        self.dir = top / '${HOME}'
        self.dir.mkdir()
        self.sock = self.dir / 'worker.sock'
        self.profile = self.dir / 'profile'
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Page)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.parent = f'pi-test-parent-{secrets.token_hex(4)}.service'
        self.browser_unit = None
        self.addCleanup(self.cleanup_units)

    def cleanup_units(self):
        units = {self.parent, self.browser_unit}
        # Also any browser unit whose worker serves this test's socket (e.g. the test failed
        # before it could record the unit name).
        listing = subprocess.run(['systemctl', '--user', 'list-units', '--all', '--plain', '--no-legend'],
                                 capture_output=True, text=True).stdout.splitlines()
        for line in listing:
            name = line.split()[0] if line.split() else ''
            if name.startswith('pi-nodriver-browser-'):
                exec_start = subprocess.run(['systemctl', '--user', 'show', name, '-p', 'ExecStart', '--value'],
                                            capture_output=True, text=True).stdout
                if str(self.sock) in exec_start:
                    units.add(name)
        for unit in filter(None, units):
            subprocess.run(['systemctl', '--user', 'kill', '--signal=SIGKILL', unit], capture_output=True)
            subprocess.run(['systemctl', '--user', 'stop', unit], capture_output=True, timeout=30)
        for pid in chrome_pids(self.profile):
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass

    def request(self, client, command, request_id, timeout=60):
        client.settimeout(timeout)
        client.sendall(json.dumps({'id': request_id, 'sessionId': 'unit-test', 'command': command}).encode() + b'\n')
        buffer = b''
        while True:
            chunk = client.recv(1 << 20)
            if not chunk:
                return None
            buffer += chunk
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                if line.startswith(MARKER):
                    response = json.loads(line[len(MARKER):])
                    if response.get('id') == request_id:
                        return response

    def connect(self, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            client = socket.socket(socket.AF_UNIX)
            try:
                client.connect(str(self.sock))
                self.addCleanup(client.close)
                return client
            except OSError:
                client.close()
                time.sleep(0.1)
        self.fail('browser worker socket never appeared')

    def test_browser_outlives_the_unit_that_started_it(self):
        subprocess.run([
            'systemd-run', '--user', '--quiet', '--collect', f'--unit={self.parent}',
            f'--working-directory={ROOT}',
            # --setenv values are not subject to ExecStart expansion; only argv is.
            f'--setenv=PI_NODRIVER_PROFILE={self.profile}',
            f'--setenv=PI_NODRIVER_DOWNLOAD_DIR={self.dir / "downloads"}',
            '--setenv=PI_NODRIVER_AUTO_SCREENSHOT=0',
            *([f"--setenv=PI_NODRIVER_SYSTEMD={os.environ['PI_NODRIVER_SYSTEMD']}"]
              if 'PI_NODRIVER_SYSTEMD' in os.environ else []),
            *map(literal, (shutil.which('node'), ROOT / 'tests' / 'fixtures' / 'launch-worker.mjs', ROOT, self.sock)),
        ], check=True, timeout=30)
        client = self.connect()
        url = f'http://127.0.0.1:{self.server.server_port}/'
        opened = self.request(client, f'open {url}', 1)
        self.assertTrue(opened and opened.get('ok'), opened)

        info = dict(line.split('=', 1) for line in (self.dir / 'nodriver-browser.env').read_text().splitlines() if '=' in line)
        worker = int(info['PID'])
        self.browser_unit = unit_of(worker)
        self.assertTrue(self.browser_unit.startswith('pi-nodriver-browser-'), self.browser_unit)
        chrome = chrome_pids(self.profile)
        root = chrome_root(self.profile)
        self.assertIsNotNone(root, 'Chrome did not start')
        helpers_in_parent = [pid for pid in chrome if unit_of(pid) == self.parent]
        self.assertEqual(helpers_in_parent, [], 'Chrome helpers must not live in the starting unit')

        # The incident trigger: restarting the service of the Pi process that started the browser.
        subprocess.run(['systemctl', '--user', 'stop', self.parent], check=True, timeout=60)
        time.sleep(2)
        self.assertTrue(alive(worker), 'worker died with the unit that started it')
        self.assertTrue(alive(root), 'Chrome died with the unit that started it')
        again = self.request(client, f'open {url}?after-parent-stop', 2)
        self.assertTrue(again and again.get('ok'), again)

        # Stopping the browser unit itself stops everything, promptly, even with an idle client.
        started = time.monotonic()
        subprocess.run(['systemctl', '--user', 'stop', self.browser_unit], check=True, timeout=60)
        self.assertLess(time.monotonic() - started, 10, 'worker shutdown waited for the idle client')
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and (alive(worker) or any(alive(pid) for pid in chrome)):
            time.sleep(0.1)
        self.assertFalse(alive(worker))
        self.assertEqual([pid for pid in chrome if alive(pid)], [])


if __name__ == '__main__':
    unittest.main()
