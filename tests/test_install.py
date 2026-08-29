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
            self.assertIn('-screen 0 1600x1000x24', extension_source)
            self.assertIn('if (response.screenshotPath)', extension_source)
            self.assertIn('name: "fetch_image"', extension_source)
            self.assertIn('fetch-image ${JSON.stringify(params.url)}', extension_source)
            self.assertIn('response.imagePath', extension_source)
            self.assertIn('[[image: ${response.imagePath}]]', extension_source)
            self.assertIn('vision-mark <x> <y>', extension_source)
            self.assertIn('vision-click <preview-token>', extension_source)
            self.assertIn('Raw coordinate clicks are blocked', extension_source)
            self.assertIn('inspect the attached marked screenshot', extension_source)
            worker_source = (extension / 'worker.py').read_text()
            self.assertIn('--window-size=1600,1000', worker_source)
            native_click = worker_source.split('    async def native_click', 1)[1].split('    async def execute', 1)[0]
            self.assertIn('minimum_settle_seconds = 0.1', native_click)
            self.assertIn('maximum_settle_seconds = 0.5', native_click)
            self.assertIn('new_tab_timeout_seconds = 2.0', native_click)
            self.assertNotIn('page.sleep(1)', native_click)
            updated = json.loads(settings.read_text())
            self.assertEqual(updated['packages'], ['npm:pi-until-done'])
            self.assertTrue((agent_dir / 'settings.json.pi-nodriver-browser.bak').is_file())

    def test_fetch_image_metadata_does_not_force_implicit_visual_intent(self):
        extension_source = (ROOT / 'index.ts').read_text()
        self.assertIn(
            'Use fetch_image after web_search, crawl, or browser when the user asks to see',
            extension_source,
        )
        self.assertNotIn('Treat visual-appearance questions', extension_source)
        self.assertNotIn('「X 長怎樣？」', extension_source)
        self.assertNotIn('what does X look like?', extension_source)
        self.assertNotIn('visually depictable real-world subject', extension_source)
        self.assertNotIn('explicit or implicit visual-delivery requests', extension_source)


if __name__ == '__main__':
    unittest.main()
