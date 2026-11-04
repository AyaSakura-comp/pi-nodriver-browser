"""Launch, probe and kill the Chrome that the worker owns.

Chrome runs in its own process group with a fixed profile. Liveness is judged at
page level (a fresh tab must run JavaScript), not by the browser endpoint alone:
after an OOM kill of Chrome's GPU process the browser endpoint kept answering
while every tab hung (2026-11-04), and a browser-level probe called that healthy.
"""
import asyncio
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
LOCK_FILES = ('SingletonLock', 'SingletonSocket', 'SingletonCookie')


def find_chrome():
    for candidate in (os.environ.get('PI_NODRIVER_CHROME'), os.environ.get('PI_BROWSER_CHROME'), '/opt/google/chrome/chrome',
                      shutil.which('google-chrome'), shutil.which('google-chrome-stable'),
                      shutil.which('chromium'), shutil.which('chromium-browser')):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError('Chrome not found; set PI_BROWSER_CHROME')


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
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(pid_alive(pid) for pid in pids):
        await asyncio.sleep(0.1)
    for pid in pids:
        if pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


class Chrome:
    def __init__(self, profile, *, log_path=None):
        self.profile = Path(profile)
        self.log_path = Path(log_path) if log_path else None
        self.proc = None
        self.port = None
        self.conn = None
        self.targets = {}
        self.started_at = None

    async def start(self, timeout=30.0):
        binary = find_chrome()
        self.profile.mkdir(parents=True, exist_ok=True)
        # A Chrome left over from an earlier worker holds the profile lock; a new launch
        # would hand its arguments to that one and exit.
        await kill_pids(profile_pids(self.profile))
        for name in LOCK_FILES:
            try:
                (self.profile / name).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        self.port = free_port()
        args = [binary, *FLAGS, f'--user-data-dir={self.profile}', f'--remote-debugging-port={self.port}',
                'about:blank']
        log = open(self.log_path, 'wb') if self.log_path else asyncio.subprocess.DEVNULL
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        finally:
            if self.log_path:
                log.close()
        deadline = time.monotonic() + timeout
        ws_url = None
        while time.monotonic() < deadline:
            if self.proc.returncode is not None:
                raise RuntimeError(f'Chrome exited during startup (code {self.proc.returncode})')
            try:
                info = await asyncio.to_thread(self._http_json, '/json/version')
                ws_url = info['webSocketDebuggerUrl']
                break
            except Exception:
                await asyncio.sleep(0.2)
        if not ws_url:
            await self.stop()
            raise RuntimeError(f'Chrome did not open its DevTools port within {timeout:g}s')
        self.conn = Connection(ws_url)
        await self.conn.connect()
        self.conn.on('Target.targetCreated', self._on_target)
        self.conn.on('Target.targetInfoChanged', self._on_target)
        self.conn.on('Target.targetDestroyed', self._on_target_gone)
        await self.conn.send('Target.setDiscoverTargets', {'discover': True})
        self.started_at = time.monotonic()

    def _http_json(self, path):
        with urllib.request.urlopen(f'http://127.0.0.1:{self.port}{path}', timeout=2) as response:
            return json.load(response)

    def _on_target(self, _method, params, _session):
        info = params.get('targetInfo') or {}
        target_id = info.get('targetId')
        if target_id:
            known = self.targets.get(target_id)
            info['_seen'] = known['_seen'] if known else time.monotonic()
            self.targets[target_id] = info

    def _on_target_gone(self, _method, params, _session):
        self.targets.pop(params.get('targetId'), None)

    def alive(self):
        return (self.proc is not None and self.proc.returncode is None
                and self.conn is not None and not self.conn.closed)

    def pages(self):
        return [info for info in self.targets.values() if info.get('type') == 'page']

    async def new_tab(self, url='about:blank', *, background=True, new_window=False, timeout=10.0):
        params = {'url': url, 'background': background}
        if new_window:
            # Only a window's front tab is visible; hidden tabs pause requestAnimationFrame,
            # which leaves virtual lists and lazy content half rendered. Each Pi session
            # therefore gets its own window, so every session's page stays visible.
            params.update(newWindow=True, background=False)
        result = await self.conn.send('Target.createTarget', params, timeout=timeout)
        target_id = result['targetId']
        self.targets.setdefault(target_id, {'targetId': target_id, 'type': 'page', '_seen': time.monotonic()})
        try:
            if new_window:
                try:
                    window = await self.conn.send('Browser.getWindowForTarget', {'targetId': target_id}, timeout=timeout)
                    await self.conn.send('Browser.setWindowBounds', {
                        'windowId': window['windowId'],
                        'bounds': {'left': 0, 'top': 0, 'width': 1280, 'height': 720, 'windowState': 'normal'}},
                        timeout=timeout)
                except CDPError:
                    pass
            return await Tab.attach(self, target_id, timeout=timeout)
        except BaseException:
            await self.close_target(target_id)
            raise

    async def close_target(self, target_id, timeout=3.0):
        # Through the browser connection: works even when the tab's renderer is hung.
        try:
            await self.conn.send('Target.closeTarget', {'targetId': target_id}, timeout=timeout)
        except CDPError:
            pass
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

    def helper_pids(self):
        pids = set(profile_pids(self.profile))
        if self.proc is not None and self.proc.returncode is None:
            pids.add(self.proc.pid)
        return pids

    async def stop(self):
        if self.conn is not None:
            try:
                await asyncio.wait_for(self.conn.send('Browser.close', timeout=2), 3)
            except Exception:
                pass
            await self.conn.close()
        pids = self.helper_pids()
        if self.proc is not None and self.proc.returncode is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        await kill_pids(pids)
        if self.proc is not None:
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except asyncio.TimeoutError:
                pass
