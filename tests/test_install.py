import json
import os
import shutil
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
                'SKIP_CHROME_EXTENSION_INSTALL': '1',
                'INSTALL_BUSTER': '1',
            }

            subprocess.run([str(ROOT / 'install.sh')], cwd=ROOT, env=env, check=True)

            installer_source = (ROOT / 'install.sh').read_text()
            self.assertIn('PI_NODRIVER_SOCKET', installer_source)
            self.assertIn("'command': 'shutdown'", installer_source)
            extension = agent_dir / 'extensions/nodriver-browser'
            self.assertTrue((extension / 'index.ts').is_file())
            self.assertTrue((extension / 'worker.py').is_file())
            self.assertTrue((extension / 'browser_logic.py').is_file())
            self.assertTrue((extension / 'install_chrome_extensions.py').is_file())
            buster_manifest = extension / 'chrome-extensions/buster/manifest.json'
            self.assertTrue(buster_manifest.is_file())
            self.assertEqual(json.loads(buster_manifest.read_text())['version'], '3.4.0')
            self.assertEqual(
                json.loads(buster_manifest.read_text())['homepage_url'],
                'https://github.com/dessant/buster',
            )
            extension_source = (extension / 'index.ts').read_text()
            self.assertIn('createConnection', extension_source)
            self.assertIn('--server', extension_source)
            self.assertIn('worker.disconnect()', extension_source)
            self.assertIn('ctx.sessionManager.getSessionId()', extension_source)
            self.assertIn('sessionId', extension_source)
            self.assertIn('cancelId', extension_source)
            self.assertIn('-screen 0 1600x1000x24', extension_source)
            self.assertIn('if (response.screenshotPath)', extension_source)
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

    def test_does_not_deploy_buster_without_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as temp:
            agent_dir = Path(temp) / 'agent'
            agent_dir.mkdir()
            env = {
                **os.environ,
                'PI_AGENT_DIR': str(agent_dir),
                'SKIP_SYSTEM_CHECKS': '1',
                'SKIP_PIP_INSTALL': '1',
                'SKIP_CHROME_EXTENSION_INSTALL': '1',
            }

            subprocess.run([str(ROOT / 'install.sh')], cwd=ROOT, env=env, check=True)

            self.assertFalse(
                (agent_dir / 'extensions/nodriver-browser/chrome-extensions/buster').exists()
            )

    def test_failed_buster_swap_restores_previous_deployment(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            source = temp / 'source'
            source.mkdir()
            for filename in (
                'install.sh',
                'index.ts',
                'worker.py',
                'install_chrome_extensions.py',
                'browser_logic.py',
                'requirements.txt',
            ):
                shutil.copy2(ROOT / filename, source / filename)
            shutil.copytree(ROOT / 'stealth-extension', source / 'stealth-extension')
            archive_dir = source / 'third_party/buster'
            archive_dir.mkdir(parents=True)
            shutil.copy2(
                ROOT / 'third_party/buster/buster-3.4.0-chrome.zip',
                archive_dir / 'buster-3.4.0-chrome.zip',
            )

            fake_bin = temp / 'bin'
            fake_bin.mkdir()
            fake_mv = fake_bin / 'mv'
            fake_mv.write_text(
                '#!/usr/bin/env bash\n'
                'if [[ "$1" == *"/.buster-stage."* ]]; then exit 73; fi\n'
                'exec /bin/mv "$@"\n'
            )
            fake_mv.chmod(0o755)

            agent_dir = temp / 'agent'
            old_buster = agent_dir / 'extensions/nodriver-browser/chrome-extensions/buster'
            old_buster.mkdir(parents=True)
            old_manifest = {'name': 'Previously installed Buster', 'version': '3.3.0'}
            (old_buster / 'manifest.json').write_text(json.dumps(old_manifest))
            env = {
                **os.environ,
                'PATH': f'{fake_bin}:{os.environ["PATH"]}',
                'PI_AGENT_DIR': str(agent_dir),
                'SKIP_SYSTEM_CHECKS': '1',
                'SKIP_PIP_INSTALL': '1',
                'SKIP_CHROME_EXTENSION_INSTALL': '1',
                'INSTALL_BUSTER': '1',
            }

            result = subprocess.run(
                [str(source / 'install.sh')],
                cwd=source,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 73)
            self.assertEqual(json.loads((old_buster / 'manifest.json').read_text()), old_manifest)

    def test_bad_buster_archive_preserves_previous_deployment(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            source = temp / 'source'
            source.mkdir()
            for filename in (
                'install.sh',
                'index.ts',
                'worker.py',
                'install_chrome_extensions.py',
                'browser_logic.py',
                'requirements.txt',
            ):
                shutil.copy2(ROOT / filename, source / filename)
            shutil.copytree(ROOT / 'stealth-extension', source / 'stealth-extension')
            archive_dir = source / 'third_party/buster'
            archive_dir.mkdir(parents=True)
            (archive_dir / 'buster-3.4.0-chrome.zip').write_bytes(b'corrupt archive')

            agent_dir = temp / 'agent'
            old_buster = agent_dir / 'extensions/nodriver-browser/chrome-extensions/buster'
            old_buster.mkdir(parents=True)
            old_manifest = {'name': 'Previously installed Buster', 'version': '3.3.0'}
            (old_buster / 'manifest.json').write_text(json.dumps(old_manifest))
            env = {
                **os.environ,
                'PI_AGENT_DIR': str(agent_dir),
                'SKIP_SYSTEM_CHECKS': '1',
                'SKIP_PIP_INSTALL': '1',
                'SKIP_CHROME_EXTENSION_INSTALL': '1',
                'INSTALL_BUSTER': '1',
            }

            result = subprocess.run(
                [str(source / 'install.sh')],
                cwd=source,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads((old_buster / 'manifest.json').read_text()), old_manifest)


if __name__ == '__main__':
    unittest.main()
