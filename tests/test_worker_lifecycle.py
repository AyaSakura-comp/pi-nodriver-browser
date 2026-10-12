"""Worker process lifecycle (no Chrome needed): prompt shutdown with idle Pi clients,
and an attach-only worker that never outlives the shared Chrome it attached to.

Regression for 2026-10-13: a PiWeb restart killed the shared Chrome, but the
attach-only worker kept its socket and every browser/research call stalled until
the 30 s research deadline. Separately, SIGTERM with idle clients blocked in
asyncio.Server.wait_closed() until systemd escalated to SIGKILL after 90 s.
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = b'__PI_NODRIVER__'


def unused_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class FakeCdpPort:
    """Stands in for Chrome's DevTools port: the watchdog only checks TCP liveness."""

    def __init__(self):
        self.sock = socket.create_server(('127.0.0.1', 0))
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self):
        while True:
            try:
                connection, _ = self.sock.accept()
            except OSError:
                return
            connection.close()

    def close(self):
        """Chrome exited: from now on connections are refused."""
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        self.thread.join(timeout=2)


class WorkerProcess:
    def __init__(self, testcase, **extra_env):
        self.dir = Path(tempfile.mkdtemp(prefix='pi-worker-lifecycle-'))
        testcase.addCleanup(shutil.rmtree, self.dir, True)
        self.sock = self.dir / 'worker.sock'
        self.clients = []
        env = {k: v for k, v in os.environ.items() if not k.startswith('PI_NODRIVER_')}
        env.update(PI_NODRIVER_PROFILE=str(self.dir / 'profile'),
                   PI_NODRIVER_DOWNLOAD_DIR=str(self.dir / 'downloads'), **extra_env)
        self.stderr = open(self.dir / 'stderr.log', 'wb')
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / 'worker.py'), '--server', str(self.sock)],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=self.stderr, start_new_session=True)
        testcase.addCleanup(self.kill)

    def log(self):
        return (self.dir / 'stderr.log').read_text(errors='replace')[-2000:]

    def kill(self):
        for client in self.clients:
            client.close()
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait(timeout=5)
        self.stderr.close()

    def wait_for_socket(self, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError(f'worker exited early ({self.proc.returncode}): {self.log()}')
            try:
                with socket.socket(socket.AF_UNIX) as probe:
                    probe.connect(str(self.sock))
                    return
            except OSError:
                time.sleep(0.05)
        raise AssertionError('worker socket never appeared')

    def connect(self):
        client = socket.socket(socket.AF_UNIX)
        client.connect(str(self.sock))
        self.clients.append(client)
        return client

    def wait_exit(self, timeout):
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    @staticmethod
    def request(client, command, request_id, timeout=15):
        client.settimeout(timeout)
        client.sendall(json.dumps({'id': request_id, 'sessionId': 'lifecycle', 'command': command}).encode() + b'\n')
        buffer = b''
        while True:
            chunk = client.recv(65536)
            if not chunk:
                return None
            buffer += chunk
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                if line.startswith(MARKER):
                    response = json.loads(line[len(MARKER):])
                    if response.get('id') == request_id:
                        return response

    @staticmethod
    def closed_by_peer(client, timeout=5):
        client.settimeout(timeout)
        try:
            return client.recv(65536) == b''
        except (ConnectionResetError, socket.timeout):
            return False


class WorkerShutdownTests(unittest.TestCase):
    def test_sigterm_with_idle_persistent_client_exits_promptly(self):
        worker = WorkerProcess(self)
        worker.wait_for_socket()
        client = worker.connect()  # every Pi process keeps one idle connection open
        started = time.monotonic()
        worker.proc.send_signal(signal.SIGTERM)
        code = worker.wait_exit(timeout=10)
        self.assertIsNotNone(code, 'worker stayed alive after SIGTERM while a client was connected')
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(code, 0, worker.log())
        self.assertFalse(worker.sock.exists())
        self.assertTrue(worker.closed_by_peer(client))


class AttachOnlyWorkerTests(unittest.TestCase):
    def test_invalid_attach_port_exits_with_clear_error_instead_of_crashing(self):
        for value in ('not-a-port', '70000'):
            worker = WorkerProcess(self, PI_NODRIVER_ATTACH_PORT=value)
            code = worker.wait_exit(timeout=20)
            self.assertEqual(code, 2, worker.log())
            self.assertIn('PI_NODRIVER_ATTACH_PORT', worker.log())
            self.assertNotIn('Traceback', worker.log())
            self.assertFalse(worker.sock.exists())

    def test_refuses_to_start_when_no_chrome_listens(self):
        worker = WorkerProcess(self, PI_NODRIVER_ATTACH_PORT=str(unused_port()))
        code = worker.wait_exit(timeout=20)
        self.assertEqual(code, 0, 'attach-only worker without Chrome must exit 0 (no systemd restart loop): '
                         + worker.log())
        self.assertFalse(worker.sock.exists(), 'must not take the shared socket from a working worker')

    def test_exits_and_closes_clients_when_shared_chrome_stops_listening(self):
        chrome = FakeCdpPort()
        self.addCleanup(chrome.close)
        worker = WorkerProcess(self, PI_NODRIVER_ATTACH_PORT=str(chrome.port))
        worker.wait_for_socket()
        client = worker.connect()
        chrome.close()
        code = worker.wait_exit(timeout=15)
        self.assertEqual(code, 0, worker.log())
        self.assertFalse(worker.sock.exists(), 'Pi must see the socket gone and start a fresh worker')
        self.assertTrue(worker.closed_by_peer(client))

    def test_browser_command_fails_fast_once_shared_chrome_is_gone(self):
        chrome = FakeCdpPort()
        self.addCleanup(chrome.close)
        worker = WorkerProcess(self, PI_NODRIVER_ATTACH_PORT=str(chrome.port))
        worker.wait_for_socket()
        client = worker.connect()
        chrome.close()
        started = time.monotonic()
        response = worker.request(client, 'open https://example.test/', 1)
        elapsed = time.monotonic() - started
        self.assertIsNotNone(response, 'worker closed the connection without answering')
        self.assertFalse(response['ok'])
        self.assertIn('shared Chrome', response['error'])
        self.assertLess(elapsed, 2.0, 'must not wait for a connection to a Chrome that is gone')
        self.assertEqual(worker.wait_exit(timeout=10), 0, worker.log())


if __name__ == '__main__':
    unittest.main()
