#!/usr/bin/env python3
"""Run the seven-level adversarial UI benchmark through Pi."""

import argparse
import json
import math
import os
import signal
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = ROOT.parents[1]
DEFAULT_WORKER_PYTHON = (
    Path(os.environ.get('PI_CODING_AGENT_DIR', Path.home() / '.pi' / 'agent'))
    / 'extensions' / 'nodriver-browser' / '.venv' / 'bin' / 'python'
)
MODES = ('forced-omni', 'hybrid', 'semantic-manual')
MODE_ENVIRONMENTS = {
    'forced-omni': {
        'PI_NODRIVER_VISION_ONLY': '1',
        'PI_NODRIVER_VISION_FALLBACK': 'omni',
        'PI_NODRIVER_BENCHMARK_ACTION_POLICY': 'forced-omni',
    },
    'hybrid': {
        'PI_NODRIVER_VISION_ONLY': '0',
        'PI_NODRIVER_VISION_FALLBACK': 'omni',
        'PI_NODRIVER_BENCHMARK_ACTION_POLICY': 'hybrid',
    },
    'semantic-manual': {
        'PI_NODRIVER_VISION_ONLY': '0',
        'PI_NODRIVER_VISION_FALLBACK': 'manual',
        'PI_NODRIVER_BENCHMARK_ACTION_POLICY': 'semantic-manual',
    },
}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--mode', choices=MODES)
    selection.add_argument('--all', action='store_true')
    parser.add_argument('--provider', default='local-llama')
    parser.add_argument('--model', default='qwen3.6-35b-q4')
    parser.add_argument('--thinking', default='medium')
    parser.add_argument('--timeout', type=float, default=360.0)
    parser.add_argument('--trials', type=int, default=1)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'results')
    parser.add_argument(
        '--worker-python', type=Path, default=DEFAULT_WORKER_PYTHON,
        help='Python interpreter containing the nodriver dependencies',
    )
    parser.add_argument(
        '--socket-root', type=Path, default=Path('/tmp'),
        help='short local directory for per-trial Unix sockets',
    )
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('--timeout must be positive and finite')
    if args.trials <= 0:
        parser.error('--trials must be positive')
    return args


def build_plan(args, mode, trial, run_id):
    fixture_url = (ROOT / 'levels.html').resolve().as_uri()
    prompt = (ROOT / 'prompts' / f'{mode}.txt').read_text().replace(
        '{{FIXTURE_URL}}', fixture_url
    )
    unique_id = uuid.uuid4().hex[:12]
    trial_id = f'{run_id}-{mode}-trial-{trial}-{unique_id}'
    output = args.output_dir.resolve() / f'{trial_id}.jsonl'
    stderr_output = args.output_dir.resolve() / f'{trial_id}.stderr.log'
    worker_log = args.output_dir.resolve() / f'{trial_id}.worker.log'
    worker_stderr = args.output_dir.resolve() / f'{trial_id}.worker.stderr.log'
    runtime = args.output_dir.resolve() / '.runtime' / trial_id
    socket_runtime = args.socket_root.resolve() / f'pnb-{unique_id}'
    environment = {
        **MODE_ENVIRONMENTS[mode],
        'PI_NODRIVER_SOCKET': str(socket_runtime / 'b.sock'),
        'PI_NODRIVER_PROFILE': str(runtime / 'profile'),
    }
    command = [
        'pi', '--provider', args.provider, '--model', args.model,
        '--thinking', args.thinking, '--no-session', '--no-skills',
        '--no-context-files', '--tools', 'browser', '--mode', 'json', '-p', prompt,
    ]
    if len(environment['PI_NODRIVER_SOCKET'].encode()) > 100:
        raise ValueError(
            'benchmark socket path is too long; choose a shorter --socket-root'
        )
    screen = os.environ.get('PI_NODRIVER_SCREEN', '500x1000x24')
    worker_command = [
        'xvfb-run', '-a', '-s', f'-screen 0 {screen}',
        str(args.worker_python), str(REPOSITORY_ROOT / 'worker.py'),
        '--server', environment['PI_NODRIVER_SOCKET'],
    ]
    return {
        'mode': mode,
        'trial': trial,
        'prompt': prompt,
        'command': command,
        'workerCommand': worker_command,
        'environment': environment,
        'output': str(output),
        'stderrOutput': str(stderr_output),
        'workerLog': str(worker_log),
        'workerStderrOutput': str(worker_stderr),
        'runtimeDir': str(runtime),
        'socketRuntimeDir': str(socket_runtime),
    }


def wait_for_worker(process, socket_path, deadline):
    """Require a connectable socket owned by this worker process group."""
    socket_path = Path(socket_path)
    lock_path = socket_path.with_name(socket_path.name + '.lock')
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f'isolated browser worker exited during startup ({process.returncode})'
            )
        try:
            worker_pid = int(lock_path.read_text().strip())
            owned = os.getpgid(worker_pid) == process.pid
        except (FileNotFoundError, ProcessLookupError, ValueError, PermissionError):
            owned = False
        if owned and socket_path.is_socket():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(0.2)
                    client.connect(str(socket_path))
                return
            except OSError:
                pass
        time.sleep(0.05)
    raise subprocess.TimeoutExpired(process.args, 0)


def wait_for_trial(process, worker_process, timeout):
    """Wait for Pi while continuously requiring its browser worker to stay alive."""
    deadline = time.monotonic() + timeout
    while True:
        returncode = process.poll()
        if returncode is not None:
            return returncode
        worker_returncode = worker_process.poll()
        if worker_returncode is not None:
            raise RuntimeError(
                f'isolated browser worker exited unexpectedly ({worker_returncode})'
            )
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(process.args, timeout)
        time.sleep(0.05)


def stop_process_group(process):
    """Terminate Pi's dedicated process group, including surviving descendants."""
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    if process.poll() is None:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    for _ in range(20):
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            return
        time.sleep(0.05)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    if process.poll() is None:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def main():
    args = parse_args()
    modes = MODES if args.all else (args.mode,)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    plans = [
        build_plan(args, mode, trial, run_id)
        for mode in modes
        for trial in range(1, args.trials + 1)
    ]
    if args.dry_run:
        for plan in plans:
            print(json.dumps(plan, ensure_ascii=False))
        return 0

    args.output_dir.mkdir(parents=True, exist_ok=True)
    failures = 0
    active_processes = {'pi': None, 'worker': None}
    previous_handlers = {}

    def interrupt(signum, _frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        stop_process_group(active_processes['pi'])
        stop_process_group(active_processes['worker'])
        raise SystemExit(128 + signum)

    for signal_value in (signal.SIGTERM, signal.SIGHUP):
        previous_handlers[signal_value] = signal.signal(signal_value, interrupt)

    for plan in plans:
        environment = os.environ.copy()
        environment.update(plan['environment'])
        environment.pop('WAYLAND_DISPLAY', None)
        output = Path(plan['output'])
        stderr_output = Path(plan['stderrOutput'])
        worker_log = Path(plan['workerLog'])
        worker_stderr_output = Path(plan['workerStderrOutput'])
        print(f"Running {plan['mode']} trial {plan['trial']} -> {output}", flush=True)
        process = None
        worker_process = None
        trial_deadline = time.monotonic() + args.timeout
        try:
            runtime = Path(plan['runtimeDir'])
            socket_runtime = Path(plan['socketRuntimeDir'])
            runtime.mkdir(parents=True, mode=0o700)
            socket_runtime.mkdir(parents=True, mode=0o700)
            with (
                output.open('x') as stream,
                stderr_output.open('x') as error_stream,
                worker_log.open('x') as worker_stream,
                worker_stderr_output.open('x') as worker_error_stream,
            ):
                worker_process = subprocess.Popen(
                    plan['workerCommand'], env=environment, text=True,
                    stdout=worker_stream, stderr=worker_error_stream,
                    start_new_session=True,
                )
                active_processes['worker'] = worker_process
                wait_for_worker(
                    worker_process, environment['PI_NODRIVER_SOCKET'], trial_deadline
                )
                process = subprocess.Popen(
                    plan['command'], env=environment, text=True,
                    stdout=stream, stderr=error_stream, start_new_session=True,
                )
                active_processes['pi'] = process
                try:
                    returncode = wait_for_trial(
                        process, worker_process,
                        max(0.0, trial_deadline - time.monotonic()),
                    )
                except subprocess.TimeoutExpired:
                    stop_process_group(process)
                    failures += 1
                    print(f"  timed out after {args.timeout:g}s", file=sys.stderr)
                    continue
            if returncode != 0:
                failures += 1
                print(f"  exited {returncode}", file=sys.stderr)
        except subprocess.TimeoutExpired:
            failures += 1
            print(f"  timed out after {args.timeout:g}s", file=sys.stderr)
        except (OSError, RuntimeError) as error:
            failures += 1
            print(f"  could not start trial: {error}", file=sys.stderr)
        finally:
            stop_process_group(process)
            stop_process_group(worker_process)
            active_processes['pi'] = None
            active_processes['worker'] = None
            shutil.rmtree(plan['runtimeDir'], ignore_errors=True)
            shutil.rmtree(plan['socketRuntimeDir'], ignore_errors=True)
    for signal_value, previous in previous_handlers.items():
        signal.signal(signal_value, previous)
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
