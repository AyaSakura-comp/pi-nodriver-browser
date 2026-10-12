import importlib.util
import unittest
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / 'ops' / 'browser-worker-reconnect.py'


class WorkerReconnectLayoutTests(unittest.TestCase):
    def load(self):
        spec = importlib.util.spec_from_file_location('worker_reconnect', HELPER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_detects_dedicated_browser_unit(self):
        module = self.load()
        cgroup = '0::/user.slice/user-1000.slice/user@1000.service/app.slice/pi-nodriver-browser-0123456789ab.service\n'
        self.assertEqual(module.browser_unit_of(cgroup), 'pi-nodriver-browser-0123456789ab.service')

    def test_legacy_layout_inside_a_pi_service_is_not_a_browser_unit(self):
        module = self.load()
        for cgroup in ('0::/user.slice/user-1000.slice/user@1000.service/app.slice/piweb-worker.service',
                       '0::/user.slice/user-1000.slice/user@1000.service/app.slice/nodriver-browser-worker.service',
                       '0::/user.slice/user-1000.slice/session-3.scope'):
            self.assertIsNone(module.browser_unit_of(cgroup), cgroup)


if __name__ == '__main__':
    unittest.main()
