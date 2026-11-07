"""install.sh --stage copies exactly the runtime and nothing else happens."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class InstallTests(unittest.TestCase):
    def test_stages_runtime_without_install_or_lifecycle_actions(self):
        with tempfile.TemporaryDirectory() as temp:
            agent_dir = Path(temp) / 'agent'
            agent_dir.mkdir()
            extension = agent_dir / 'extensions/nodriver-browser'
            stubs = Path(temp) / 'bin'
            stubs.mkdir()
            lifecycle_log = Path(temp) / 'lifecycle.log'
            for command in ('pgrep', 'kill', 'python3', 'sleep', 'systemctl', 'xvfb-run', 'pdftotext'):
                stub = stubs / command
                stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "{command}" >> "{lifecycle_log}"\nexit 42\n')
                stub.chmod(0o755)
            subprocess.run([str(ROOT / 'install.sh'), '--stage', str(extension)], cwd=ROOT,
                           env={**os.environ, 'PI_AGENT_DIR': str(agent_dir), 'HOME': temp,
                                'PATH': f'{stubs}:/usr/bin:/bin'}, check=True)
            self.assertFalse(lifecycle_log.exists(), 'staging ran a check or a lifecycle command')
            for name in ('index.ts', 'browser-agent.ts', 'browser-intent-tool.ts', 'worker-launch.ts', 'worker.py',
                         'requirements.txt', 'pibrowser/cdp.py', 'pibrowser/chrome.py', 'pibrowser/page.py',
                         'pibrowser/fetch.py', 'pibrowser/js/snapshot.js', 'pibrowser/js/google_results.js',
                         'pibrowser/js/image_candidates.js', 'research/jobs.py', 'ops/browser-agent-watchdog.py'):
                self.assertTrue((extension / name).is_file(), name)
            subprocess.run([str(ROOT / '.venv/bin/python'), '-B', '-c', 'import worker, research.jobs'],
                           cwd=extension, check=True)


if __name__ == '__main__':
    unittest.main()
