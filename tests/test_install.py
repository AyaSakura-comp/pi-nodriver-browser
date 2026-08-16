import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def test_installs_extension_and_disables_conflicting_package(self):
        with tempfile.TemporaryDirectory() as temp:
            agent_dir = Path(temp) / 'agent'
            agent_dir.mkdir()
            settings = agent_dir / 'settings.json'
            settings.write_text(json.dumps({
                'packages': ['npm:pi-agent-browser', 'npm:pi-until-done'],
            }))
            env = {
                **os.environ,
                'PI_AGENT_DIR': str(agent_dir),
                'SKIP_SYSTEM_CHECKS': '1',
                'SKIP_PIP_INSTALL': '1',
            }

            subprocess.run([str(ROOT / 'install.sh')], cwd=ROOT, env=env, check=True)

            installer_source = (ROOT / 'install.sh').read_text()
            self.assertIn('PI_NODRIVER_SOCKET', installer_source)
            self.assertIn("'command': 'shutdown'", installer_source)
            extension = agent_dir / 'extensions/nodriver-browser'
            self.assertTrue((extension / 'index.ts').is_file())
            self.assertTrue((extension / 'worker.py').is_file())
            self.assertTrue((extension / 'browser_logic.py').is_file())
            extension_source = (extension / 'index.ts').read_text()
            self.assertIn('createConnection', extension_source)
            self.assertIn('--server', extension_source)
            self.assertIn('worker.disconnect()', extension_source)
            self.assertIn('ctx.sessionManager.getSessionId()', extension_source)
            self.assertIn('sessionId', extension_source)
            self.assertIn('cancelId', extension_source)
            self.assertIn('-screen 0 1280x720x24', extension_source)
            worker_source = (extension / 'worker.py').read_text()
            self.assertIn('--window-size=1280,720', worker_source)
            updated = json.loads(settings.read_text())
            self.assertEqual(updated['packages'], ['npm:pi-until-done'])
            self.assertTrue((agent_dir / 'settings.json.pi-nodriver-browser.bak').is_file())


if __name__ == '__main__':
    unittest.main()
