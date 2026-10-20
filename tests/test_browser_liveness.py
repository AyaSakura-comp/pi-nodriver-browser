import asyncio
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from browser_logic import pid_alive, profile_lock_pid


class ProfileLockTests(unittest.TestCase):
    def test_lock_pid_is_read_from_singleton_lock_on_this_host(self):
        with tempfile.TemporaryDirectory() as temp:
            os.symlink('chihmin-desktop-4242', Path(temp) / 'SingletonLock')
            self.assertEqual(profile_lock_pid(Path(temp), hostname='chihmin-desktop'), 4242)
            self.assertIsNone(profile_lock_pid(Path(temp), hostname='other-host'))

    def test_no_lock_means_no_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertIsNone(profile_lock_pid(Path(temp)))

    def test_pid_alive_rejects_exited_and_zombie_processes(self):
        self.assertTrue(pid_alive(os.getpid()))
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        deadline = time.monotonic() + 5
        while pid_alive(child.pid) and time.monotonic() < deadline:
            time.sleep(0.05)                     # exited but not yet reaped: a zombie
        self.assertFalse(pid_alive(child.pid))
        child.wait()
        self.assertFalse(pid_alive(child.pid))


async def _never_answers(*_args, **_kwargs):
    await asyncio.sleep(5)


class BusyBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_browser_is_kept_with_every_session_tab(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        calls = 0

        async def slow_then_ok(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls < 3:
                await asyncio.sleep(5)           # longer than the 1 s probe

        busy = SimpleNamespace(send=AsyncMock(side_effect=slow_then_ok), stop=Mock(), _process_pid=os.getpid())
        worker.browser = worker.launched_browser = busy
        page = object()
        worker.pages['session-a'] = page
        with patch('worker.uc.start', AsyncMock()) as start:
            self.assertIs(await worker.ensure_browser(), busy)
        self.assertEqual(calls, 3)
        self.assertIs(worker.pages['session-a'], page)
        busy.stop.assert_not_called()
        start.assert_not_awaited()

    async def test_exited_browser_process_is_not_waited_for(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        child.wait()
        hung = SimpleNamespace(send=AsyncMock(side_effect=_never_answers),
                               stop=Mock(), _process_pid=child.pid)
        fresh = SimpleNamespace(send=AsyncMock(), stop=Mock(), add_handler=Mock())
        worker.browser = worker.launched_browser = hung
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {'PI_NODRIVER_PROFILE': temp}), \
                 patch('worker.uc.start', AsyncMock(return_value=fresh)):
                started = time.monotonic()
                self.assertIs(await worker.ensure_browser(), fresh)
        self.assertLess(time.monotonic() - started, 3)
        hung.stop.assert_called_once()

    async def test_silent_browser_is_relaunched_after_the_probe_budget(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        hung = SimpleNamespace(send=AsyncMock(side_effect=_never_answers),
                               stop=Mock(), _process_pid=None)
        fresh = SimpleNamespace(send=AsyncMock(), stop=Mock(), add_handler=Mock())
        worker.browser = worker.launched_browser = hung
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {'PI_NODRIVER_PROFILE': temp, 'PI_NODRIVER_BROWSER_PROBE_SECONDS': '1.5'}), \
                 patch('worker.uc.start', AsyncMock(return_value=fresh)):
                self.assertIs(await worker.ensure_browser(), fresh)
        hung.stop.assert_called_once()


class ReapTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_chrome_that_ignores_sigterm_is_killed(self):
        from worker import BrowserWorker

        stubborn = subprocess.Popen([sys.executable, '-c',
                                     'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                                     'print(1, flush=True); time.sleep(60)'], stdout=subprocess.PIPE)
        stubborn.stdout.readline()               # SIGTERM is ignored from here on
        try:
            started = time.monotonic()
            await BrowserWorker._reap_browser_processes({stubborn.pid}, grace=0.3)
            self.assertEqual(stubborn.wait(timeout=2), -signal.SIGKILL)
            self.assertLess(time.monotonic() - started, 2.5)
        finally:
            if stubborn.poll() is None:
                stubborn.kill()

    async def test_relaunch_waits_until_the_old_profile_owner_is_gone(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        stubborn = subprocess.Popen([sys.executable, '-c',
                                     'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                                     'print(1, flush=True); time.sleep(60)'], stdout=subprocess.PIPE)
        stubborn.stdout.readline()
        stale = SimpleNamespace(send=AsyncMock(side_effect=ConnectionError('closed')), stop=Mock())
        worker.browser = worker.launched_browser = stale
        owner_alive_at_launch = []

        async def start(**_kwargs):
            owner_alive_at_launch.append(stubborn.poll() is None)
            return SimpleNamespace(send=AsyncMock(), stop=Mock(), add_handler=Mock())

        try:
            with tempfile.TemporaryDirectory() as temp:
                os.symlink(f'{os.uname().nodename}-{stubborn.pid}', Path(temp) / 'SingletonLock')
                with patch.dict(os.environ, {'PI_NODRIVER_PROFILE': temp}), \
                     patch('worker.uc.start', side_effect=start):
                    await worker.ensure_browser()
            self.assertEqual(owner_alive_at_launch, [False])
        finally:
            if stubborn.poll() is None:
                stubborn.kill()


class AttachFallbackTests(unittest.TestCase):
    def test_exited_launches_are_skipped_and_dropped_from_the_registry(self):
        from worker import BrowserWorker

        exited = subprocess.Popen([sys.executable, '-c', 'pass'])
        exited.wait()
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp)
            (profile / 'DevToolsActivePort').write_text('48361\n/devtools/browser/x\n')
            live = SimpleNamespace(config=SimpleNamespace(port=40001, user_data_dir=temp), _process_pid=os.getpid())
            dead = SimpleNamespace(config=SimpleNamespace(port=40002, user_data_dir=temp), _process_pid=exited.pid)
            other = SimpleNamespace(config=SimpleNamespace(port=40003, user_data_dir='/elsewhere'), _process_pid=os.getpid())
            registry_set = _Registry([live, dead, other])
            with patch('worker.uc.util.get_registered_instances', return_value=registry_set):
                ports = BrowserWorker()._live_profile_ports(profile)
        self.assertEqual(ports, [(40001, live), (48361, None)])
        self.assertNotIn(dead, registry_set)


class ProfileOwnerPortTests(unittest.TestCase):
    def test_port_of_the_chrome_holding_the_profile(self):
        from browser_logic import chrome_debug_port
        from worker import BrowserWorker

        owner = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', '--remote-debugging-port=44887'])
        try:
            deadline = time.monotonic() + 5
            while chrome_debug_port(owner.pid) is None and time.monotonic() < deadline:
                time.sleep(0.02)                 # /proc/<pid>/cmdline is empty until exec finishes
            self.assertEqual(chrome_debug_port(owner.pid), 44887)
            with tempfile.TemporaryDirectory() as temp:
                os.symlink(f'{os.uname().nodename}-{owner.pid}', Path(temp) / 'SingletonLock')
                with patch('worker.uc.util.get_registered_instances', return_value=_Registry()):
                    self.assertEqual(BrowserWorker()._live_profile_ports(Path(temp)), [(44887, None)])
        finally:
            owner.kill()
            owner.wait()


class _Registry(list):
    # nodriver keeps a set of Browser objects; SimpleNamespace stand-ins are unhashable.
    def discard(self, item):
        if item in self:
            self.remove(item)


if __name__ == '__main__':
    unittest.main()


class BrowserHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_never_starts_chrome_just_to_probe(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        with patch('worker.uc.start', AsyncMock()) as start:
            result = await worker.execute('browser-health', '__watchdog__')
        self.assertEqual(result['browser'], 'not-started')
        start.assert_not_awaited()

    async def test_reports_a_relaunch_without_touching_sessions(self):
        from worker import BrowserWorker

        worker = BrowserWorker()
        dead = SimpleNamespace(send=AsyncMock(side_effect=ConnectionError('closed')), stop=Mock())
        fresh = SimpleNamespace(send=AsyncMock(), stop=Mock(), add_handler=Mock(), tabs=[])
        worker.browser = worker.launched_browser = dead
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {'PI_NODRIVER_PROFILE': temp}), \
                 patch('worker.uc.start', AsyncMock(return_value=fresh)):
                result = await worker.execute('browser-health', '__watchdog__')
        self.assertEqual(result['browser'], 'relaunched')
        self.assertNotIn('__watchdog__', worker.pages)
