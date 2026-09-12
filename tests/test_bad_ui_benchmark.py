import json
import importlib.util
import os
import re
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


def write_fake_xvfb_run(directory):
    executable = directory / 'xvfb-run'
    executable.write_text(
        f'''#!{sys.executable}\nimport os, signal, socket, sys, time\npath = sys.argv[-1]\nos.makedirs(os.path.dirname(path), exist_ok=True)\nserver = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\nserver.bind(path)\nopen(path + '.lock', 'w').write(str(os.getpid()))\nserver.listen(1)\nsignal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\nwhile True: time.sleep(1)\n'''
    )
    executable.chmod(0o755)
    return executable


class BadUiBenchmarkTests(unittest.TestCase):
    def test_manifest_and_fixture_declare_the_seven_sequential_levels(self):
        manifest = json.loads((BENCHMARK / 'manifest.json').read_text())
        html = (BENCHMARK / 'levels.html').read_text()

        self.assertEqual(manifest['levels'], EXPECTED_LEVELS)
        self.assertEqual(set(manifest['modes']), {
            'forced-omni', 'hybrid', 'semantic-manual'
        })
        self.assertEqual(
            re.findall(r'data-level="([^"]+)"', html), EXPECTED_LEVELS
        )
        for level in EXPECTED_LEVELS:
            self.assertIn(f"complete('{level}')", html)
        self.assertIn('id="finish"', html)

    def test_repository_docs_link_suite_and_ignore_generated_results(self):
        root_readme = (ROOT / 'README.md').read_text()
        ignore = (ROOT / '.gitignore').read_text().splitlines()

        self.assertIn('benchmarks/bad-ui/README.md', root_readme)
        self.assertTrue(any(
            line in {'benchmarks/bad-ui/results/', 'benchmarks/**/results/'}
            for line in ignore
        ))

    def test_benchmark_files_are_repository_portable(self):
        files = [
            path for path in BENCHMARK.rglob('*')
            if path.is_file()
            and '__pycache__' not in path.parts
            and 'results' not in path.relative_to(BENCHMARK).parts
        ]
        forbidden_home = '/' + 'home' + '/' + 'chihmin'

        self.assertEqual(len(files), 7)
        for path in files:
            self.assertNotIn(forbidden_home, path.read_text())
        for prompt in (BENCHMARK / 'prompts').glob('*.txt'):
            self.assertIn('{{FIXTURE_URL}}', prompt.read_text())
        hybrid = (BENCHMARK / 'prompts' / 'hybrid.txt').read_text()
        self.assertIn('禁止 screenshot', hybrid)
        self.assertIn('vision-mark omni', hybrid)

    def test_rejects_a_unix_socket_root_that_is_too_long(self):
        result = subprocess.run(
            [
                sys.executable, str(BENCHMARK / 'run.py'), '--mode', 'hybrid',
                '--socket-root', '/' + ('x' * 110), '--dry-run',
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('socket path is too long', result.stderr)

    def test_missing_pi_is_reported_without_masking_the_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / 'results'
            write_fake_xvfb_run(Path(temporary))
            environment = dict(os.environ)
            environment['PATH'] = temporary

            result = subprocess.run(
                [
                    sys.executable, str(BENCHMARK / 'run.py'),
                    '--mode', 'hybrid', '--output-dir', str(output_dir),
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 1)
            self.assertNotIn('UnboundLocalError', result.stderr)
            self.assertIn('could not start', result.stderr)

    def test_timeout_preserves_streamed_partial_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fake_xvfb_run(root)
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
            stderr_outputs = [
                path for path in output_dir.glob('*.stderr.log')
                if not path.name.endswith('.worker.stderr.log')
            ]
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
            self.assertEqual(plan['workerCommand'][0], 'xvfb-run')
            self.assertEqual(Path(plan['workerCommand'][-3]).name, 'worker.py')
            self.assertEqual(plan['workerCommand'][-2], '--server')
            self.assertEqual(
                plan['workerCommand'][-1], plan['environment']['PI_NODRIVER_SOCKET']
            )
            self.assertTrue(plan['output'].endswith('.jsonl'))
            self.assertTrue(plan['stderrOutput'].endswith('.stderr.log'))
            self.assertIn('/.runtime/', plan['runtimeDir'])
            self.assertLessEqual(
                len(plan['environment']['PI_NODRIVER_SOCKET'].encode()), 100
            )
            self.assertTrue(plan['workerLog'].endswith('.worker.log'))
            self.assertTrue(plan['workerStderrOutput'].endswith('.worker.stderr.log'))
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
            profiles.add(environment['PI_NODRIVER_PROFILE'])
        self.assertEqual(len(sockets), 3)
        self.assertEqual(len(profiles), 3)


if __name__ == '__main__':
    unittest.main()
