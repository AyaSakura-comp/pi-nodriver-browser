"""Launch, probe and kill the Chrome that the worker owns.

Chrome runs in its own process group with a fixed profile. Liveness is judged at
page level (a fresh tab must run JavaScript), not by the browser endpoint alone:
after an OOM kill of Chrome's GPU process the browser endpoint kept answering
while every tab hung (2026-11-04), and a browser-level probe called that healthy.
"""
import asyncio
import contextlib
import json
import os
import shutil
import signal
import socket
import time
import urllib.request
from pathlib import Path

from .cdp import CDPError, Connection
from .page import Tab

FLAGS = [
    '--no-first-run', '--no-default-browser-check', '--no-service-autorun', '--no-pings',
    '--password-store=basic', '--homepage=about:blank', '--disable-dev-shm-usage',
    '--disable-breakpad', '--disable-session-crashed-bubble', '--hide-crash-restore-bubble',
    '--disable-search-engine-choice-screen', '--disable-save-password-bubble', '--deny-permission-prompts',
    '--disable-features=Translate,OptimizationGuideModelDownloading,OptimizationHints,PasswordLeakDetection',
    # Crawl tabs are background tabs; without these their timers and rendering are throttled.
    '--disable-background-timer-throttling', '--disable-renderer-backgrounding',
    '--disable-backgrounding-occluded-windows',
    '--simulate-outdated-no-au=Tue, 31 Dec 2099 23:59:59 GMT', '--check-for-update-interval=31536000',
    '--ozone-platform=x11', '--window-position=0,0', '--window-size=1280,720',
    '--remote-allow-origins=*', '--remote-debugging-host=127.0.0.1',
]


def find_chrome():
    for candidate in ('/opt/google/chrome/chrome',
                      *map(shutil.which, ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'))):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError('Chrome not found; install google-chrome or chromium')


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def profile_pids(profile):
    """PIDs of any process (any Chrome helper too) running with this profile."""
    marker = f'--user-data-dir={profile}'.encode()
    pids = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            cmdline = (entry / 'cmdline').read_bytes()
        except OSError:
            continue
        index = cmdline.find(marker)
        if index >= 0:
            end = index + len(marker)
            if end == len(cmdline) or cmdline[end:end + 1] in (b'\0', b' '):
                pids.append(int(entry.name))
    return pids


def pid_alive(pid):
    try:
        with open(f'/proc/{pid}/stat') as handle:
            return handle.read().split(') ', 1)[1][0] not in 'ZX'
    except (OSError, IndexError):
        return False


async def kill_pids(pids, grace=3.0):
    pids = [pid for pid in pids if pid_alive(pid)]
    for pid in pids:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(pid_alive(pid) for pid in pids):
        await asyncio.sleep(0.1)
    for pid in pids:
        if pid_alive(pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


class Chrome:
    def __init__(self, profile, log_path):
        self.profile = Path(profile)
        self.log_path = log_path
        self.proc = None
        self.port = None
        self.conn = None
        self.targets = {}

    async def start(self, timeout=30.0):
        binary = find_chrome()
        self.profile.mkdir(parents=True, exist_ok=True)
        # A Chrome left over from an earlier worker holds the profile lock; a new launch
        # would hand its arguments to that one and exit.
        await kill_pids(profile_pids(self.profile))
        for name in ('SingletonLock', 'SingletonSocket', 'SingletonCookie'):
            with contextlib.suppress(OSError):
                (self.profile / name).unlink()
        self.port = free_port()
        with open(self.log_path, 'wb') as log:
            self.proc = await asyncio.create_subprocess_exec(
                binary, *FLAGS, f'--user-data-dir={self.profile}', f'--remote-debugging-port={self.port}',
                'about:blank', stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        deadline = time.monotonic() + timeout
        while True:
            if self.proc.returncode is not None:
                raise RuntimeError(f'Chrome exited during startup (code {self.proc.returncode})')
            try:
                ws_url = (await asyncio.to_thread(self._version))['webSocketDebuggerUrl']
                break
            except Exception:
                if time.monotonic() > deadline:
                    await self.stop()
                    raise RuntimeError(f'Chrome did not open its DevTools port within {timeout:g}s') from None
                await asyncio.sleep(0.2)
        self.conn = Connection()
        await self.conn.connect(ws_url)
        self.conn.on('Target.targetCreated', self._on_target)
        self.conn.on('Target.targetInfoChanged', self._on_target)
        self.conn.on('Target.targetDestroyed', lambda params: self.targets.pop(params.get('targetId'), None))
        await self.conn.send('Target.setDiscoverTargets', {'discover': True})

    def _version(self):
        with urllib.request.urlopen(f'http://127.0.0.1:{self.port}/json/version', timeout=2) as response:
            return json.load(response)

    def _on_target(self, params):
        info = params.get('targetInfo') or {}
        target_id = info.get('targetId')
        if target_id:
            info['_seen'] = self.targets.get(target_id, {}).get('_seen', time.monotonic())
            self.targets[target_id] = info

    def alive(self):
        return (self.proc is not None and self.proc.returncode is None
                and self.conn is not None and not self.conn.closed)

    def pages(self):
        return [info for info in self.targets.values() if info.get('type') == 'page']

    async def new_tab(self, *, new_window=False):
        """A blank background tab, or with new_window a tab in a window of its own: only a
        window's front tab is visible, and hidden tabs pause requestAnimationFrame, which
        leaves virtual lists and lazy content half rendered. Each Pi session therefore gets
        its own window, so every session's page stays visible."""
        params = {'url': 'about:blank', 'background': not new_window}
        if new_window:
            params['newWindow'] = True
        target_id = (await self.conn.send('Target.createTarget', params))['targetId']
        self.targets.setdefault(target_id, {'targetId': target_id, 'type': 'page', '_seen': time.monotonic()})
        try:
            if new_window:
                with contextlib.suppress(CDPError):
                    window = await self.conn.send('Browser.getWindowForTarget', {'targetId': target_id})
                    await self.conn.send('Browser.setWindowBounds', {
                        'windowId': window['windowId'],
                        'bounds': {'left': 0, 'top': 0, 'width': 1280, 'height': 720, 'windowState': 'normal'}})
            return await Tab.attach(self, target_id, timeout=10.0)
        except BaseException:
            await self.close_target(target_id)
            raise

    async def close_target(self, target_id, timeout=3.0):
        # Through the browser connection: works even when the tab's renderer is hung.
        with contextlib.suppress(CDPError):
            await self.conn.send('Target.closeTarget', {'targetId': target_id}, timeout=timeout)
        self.targets.pop(target_id, None)

    async def probe(self, timeout=5.0):
        """True when a brand-new tab can run JavaScript."""
        if not self.alive():
            return False
        target_id = None
        try:
            result = await self.conn.send('Target.createTarget', {'url': 'about:blank', 'background': True},
                                          timeout=timeout)
            target_id = result['targetId']
            session = (await self.conn.send('Target.attachToTarget', {'targetId': target_id, 'flatten': True},
                                            timeout=timeout))['sessionId']
            value = await self.conn.send('Runtime.evaluate', {'expression': '6*7', 'returnByValue': True},
                                         session=session, timeout=timeout)
            return (value.get('result') or {}).get('value') == 42
        except CDPError:
            return False
        finally:
            if target_id is not None:
                await self.close_target(target_id)

    async def stop(self):
        if self.conn is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.conn.send('Browser.close', timeout=2), 3)
            await self.conn.close()
        pids = set(profile_pids(self.profile))
        if self.proc is not None and self.proc.returncode is None:
            pids.add(self.proc.pid)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGTERM)
        await kill_pids(pids)
        if self.proc is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.proc.wait(), 3)
