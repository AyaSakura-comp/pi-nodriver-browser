import json
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / 'benchmarks' / 'bad-ui'
EXPECTED_LEVELS = [
    'adClose', 'checkbox', 'dropdown', 'cartPlus', 'tinyPlus', 'toggle', 'coupon'
]


def load_runner():
    spec = importlib.util.spec_from_file_location('bad_ui_runner', BENCHMARK / 'run.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BadUiBenchmarkTests(unittest.TestCase):
    def test_manifest_declares_the_seven_sequential_levels(self):
        manifest = json.loads((BENCHMARK / 'manifest.json').read_text())

        self.assertEqual(manifest['levels'], EXPECTED_LEVELS)
        self.assertEqual(set(manifest['modes']), {
            'forced-omni', 'hybrid', 'semantic-manual'
        })

    def test_benchmark_files_are_repository_portable(self):
        files = [
            path for path in BENCHMARK.rglob('*')
            if path.is_file() and '__pycache__' not in path.parts
        ]
        forbidden_home = '/' + 'home' + '/' + 'chihmin'

        self.assertEqual(len(files), 7)
        for path in files:
            self.assertNotIn(forbidden_home, path.read_text())
        for prompt in (BENCHMARK / 'prompts').glob('*.txt'):
            self.assertIn('{{FIXTURE_URL}}', prompt.read_text())

    def test_daemon_cleanup_accepts_only_the_owned_socket_process(self):
        runner = load_runner()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            socket_path = root / 'trial' / 'browser.sock'
            lock_path = Path(f'{socket_path}.lock')
            lock_path.parent.mkdir(parents=True)
            lock_path.write_text('1234\n')
            process = root / 'proc' / '1234'
            process.mkdir(parents=True)
            process.joinpath('cmdline').write_bytes(
                f'python\0worker.py\0--server\0{socket_path}\0'.encode()
            )

            self.assertEqual(
                runner.owned_daemon_pid(socket_path, root / 'proc'), 1234
            )
            process.joinpath('cmdline').write_bytes(b'python\0unrelated.py\0')
            self.assertIsNone(runner.owned_daemon_pid(socket_path, root / 'proc'))

    def test_timeout_preserves_streamed_partial_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_pi = root / 'pi'
            fake_pi.write_text(
                '#!/bin/sh\nprintf \'%s\\n\' \'{"event":"started"}\'\nprintf \'warning\\n\' >&2\nsleep 2\n'
            )
            fake_pi.chmod(0o755)
            output_dir = root / 'results'
            environment = dict(os.environ)
            environment['PATH'] = f'{root}:{environment.get("PATH", "")}'

            result = subprocess.run(
                [
                    sys.executable, str(BENCHMARK / 'run.py'),
                    '--mode', 'hybrid', '--timeout', '0.1',
                    '--output-dir', str(output_dir),
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 1)
            outputs = list(output_dir.glob('*.jsonl'))
            self.assertEqual(len(outputs), 1)
            self.assertEqual(outputs[0].read_text().strip(), '{"event":"started"}')
            stderr_outputs = list(output_dir.glob('*.stderr.log'))
            self.assertEqual(len(stderr_outputs), 1)
            self.assertEqual(stderr_outputs[0].read_text().strip(), 'warning')

    def test_dry_run_resolves_fixture_and_emits_all_mode_commands(self):
        result = subprocess.run(
            [sys.executable, str(BENCHMARK / 'run.py'), '--all', '--dry-run'],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        plans = [json.loads(line) for line in result.stdout.splitlines() if line]

        self.assertEqual([plan['mode'] for plan in plans], [
            'forced-omni', 'hybrid', 'semantic-manual'
        ])
        fixture_uri = (BENCHMARK / 'levels.html').resolve().as_uri()
        for plan in plans:
            self.assertIn(fixture_uri, plan['prompt'])
            self.assertEqual(plan['command'][0], 'pi')
            self.assertTrue(plan['output'].endswith('.jsonl'))
            self.assertTrue(plan['stderrOutput'].endswith('.stderr.log'))
        expected_environments = {
            'forced-omni': ('1', 'omni', 'forced-omni'),
            'hybrid': ('0', 'omni', 'hybrid'),
            'semantic-manual': ('0', 'manual', 'semantic-manual'),
        }
        sockets = set()
        profiles = set()
        for plan in plans:
            environment = plan['environment']
            expected = expected_environments[plan['mode']]
            self.assertEqual(environment['PI_NODRIVER_VISION_ONLY'], expected[0])
            self.assertEqual(environment['PI_NODRIVER_VISION_FALLBACK'], expected[1])
            self.assertEqual(
                environment['PI_NODRIVER_BENCHMARK_ACTION_POLICY'], expected[2]
            )
            sockets.add(environment['PI_NODRIVER_SOCKET'])
            profiles.add(environment['PI_NODRIVER_PROFILE_DIR'])
        self.assertEqual(len(sockets), 3)
        self.assertEqual(len(profiles), 3)


if __name__ == '__main__':
    unittest.main()
