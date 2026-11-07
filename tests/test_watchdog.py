"""ops/browser-agent-watchdog.py state machine, with health checks and systemd faked."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('watchdog', ROOT / 'ops' / 'browser-agent-watchdog.py')
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        temp = Path(directory.name)
        self.events = temp / 'events.jsonl'
        self.health = []
        self.revives = 0
        self.units = ['pi-nodriver-browser-aaa.service']

        def revive():
            self.revives += 1
            return True, 'pi-nodriver-browser-new'
        for name, value in {'STATE_DIR': temp, 'EVENTS': self.events, 'health': lambda: self.health.pop(0),
                            'worker_units': lambda: list(self.units), 'main_pid': lambda unit: '123',
                            'outage': lambda pid, unit, since: (None, 'exit-1', []), 'revive': revive,
                            'wait_healthy': lambda: True, 'memory': dict}.items():
            patch = mock.patch.object(watchdog, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        quiet = mock.patch.object(watchdog, 'print', lambda *args: None, create=True)
        quiet.start()
        self.addCleanup(quiet.stop)

    def run_checks(self, *statuses, state=None):
        self.health = [(status, f'browser {status}') for status in statuses]
        state = state or {}
        for _ in statuses:
            state = watchdog.check(state)
        return state

    def logged(self):
        return [json.loads(line)['event'] for line in self.events.read_text().splitlines()] if self.events.exists() else []

    def test_a_stopped_worker_is_revived_on_the_second_failed_check(self):
        state = self.run_checks('ok', 'dead')
        self.assertEqual(self.revives, 0)
        state = self.run_checks('dead', state=state)
        self.assertEqual(self.revives, 1)
        self.assertEqual(self.logged(), ['down', 'revive', 'recovered'])
        self.assertNotIn('down_since', state)

    def test_a_worker_that_answers_but_fails_is_replaced_on_the_third_check(self):
        state = self.run_checks('unhealthy', 'unhealthy')
        self.assertEqual(self.revives, 0)
        self.run_checks('unhealthy', state=state)
        self.assertEqual(self.revives, 1)

    def test_revivals_stop_after_three_an_hour(self):
        now = watchdog.time.time()
        state = self.run_checks('dead', 'dead', 'dead', state={'restarts': [now - 60] * 3})
        self.assertEqual(self.revives, 0)
        self.assertEqual(self.logged().count('giveup'), 1)
        self.assertTrue(state['gave_up'])

    def test_a_worker_replaced_between_checks_is_logged(self):
        state = self.run_checks('ok')
        self.units = ['pi-nodriver-browser-bbb.service']
        self.run_checks('ok', state=state)
        self.assertEqual(self.logged(), ['restarted_outside_watchdog'])


if __name__ == '__main__':
    unittest.main()
