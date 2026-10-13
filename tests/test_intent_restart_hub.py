import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path.home() / '.hermes/skills/restart-service/scripts/resolve-browser.sh'

class HubTests(unittest.TestCase):
    def run_hub(self, healthy=False, stuck=False):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / 'curl').write_text('#!/bin/sh\ntest -f "$FAKE/healthy" && printf \'{"ok":true}\'\n')
            (p / 'systemctl').write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$FAKE/calls"\n[ "$STUCK" = 1 ] || touch "$FAKE/healthy"\n')
            for f in ['curl', 'systemctl']:
                (p / f).chmod(0o755)
            if healthy:
                (p / 'healthy').touch()
            env = dict(os.environ, PATH=d + ':' + os.environ['PATH'], FAKE=d, STUCK=str(int(stuck)), BROWSER_RESOLVE_ATTEMPTS='1')
            r = subprocess.run(['bash', str(SCRIPT)], env=env, capture_output=True, text=True)
            calls = (p / 'calls').read_text() if (p / 'calls').exists() else ''
            return r, calls

    def test_healthy_does_not_restart(self):
        r, calls = self.run_hub(healthy=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(calls, '')

    def test_down_starts_only_router(self):
        r, calls = self.run_hub()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(calls, '--user start laya-intent.service\n')

    def test_unverified_repair_fails(self):
        r, _ = self.run_hub(stuck=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('unavailable', r.stderr)

if __name__ == '__main__':
    unittest.main()
