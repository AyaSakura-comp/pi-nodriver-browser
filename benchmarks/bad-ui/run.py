#!/usr/bin/env python3
"""Run the seven-level adversarial UI benchmark through Pi."""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
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
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    if args.trials <= 0:
        parser.error('--trials must be positive')
    return args


def build_plan(args, mode, trial, run_id):
    fixture_url = (ROOT / 'levels.html').resolve().as_uri()
    prompt = (ROOT / 'prompts' / f'{mode}.txt').read_text().replace(
        '{{FIXTURE_URL}}', fixture_url
    )
    trial_id = f'{run_id}-{mode}-trial-{trial}-{uuid.uuid4().hex[:12]}'
    output = args.output_dir.resolve() / f'{trial_id}.jsonl'
    stderr_output = args.output_dir.resolve() / f'{trial_id}.stderr.log'
    runtime = args.output_dir.resolve() / '.runtime' / trial_id
    environment = {
        **MODE_ENVIRONMENTS[mode],
        'PI_NODRIVER_SOCKET': str(runtime / 'browser.sock'),
        'PI_NODRIVER_PROFILE_DIR': str(runtime / 'profile'),
    }
    command = [
        'pi', '--provider', args.provider, '--model', args.model,
        '--thinking', args.thinking, '--no-session', '--no-skills',
        '--no-context-files', '--tools', 'browser', '--mode', 'json', '-p', prompt,
    ]
    return {
        'mode': mode,
        'trial': trial,
        'prompt': prompt,
        'command': command,
        'environment': environment,
        'output': str(output),
        'stderrOutput': str(stderr_output),
    }


def owned_daemon_pid(socket_path, proc_root=Path('/proc')):
    """Return the PID only when its command line names this trial's socket."""
    try:
        pid = int(Path(f'{socket_path}.lock').read_text().strip())
        command = (proc_root / str(pid) / 'cmdline').read_bytes().split(b'\0')
    except (FileNotFoundError, ValueError, PermissionError):
        return None
    decoded = [part.decode(errors='replace') for part in command if part]
    if not any(Path(part).name == 'worker.py' for part in decoded) or '--server' not in decoded:
        return None
    return pid if str(socket_path) in decoded else None


def stop_owned_daemon(socket_path):
    """Stop only the isolated daemon created for this benchmark trial."""
    pid = owned_daemon_pid(socket_path)
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)


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
    for plan in plans:
        environment = os.environ.copy()
        environment.update(plan['environment'])
        output = Path(plan['output'])
        stderr_output = Path(plan['stderrOutput'])
        print(f"Running {plan['mode']} trial {plan['trial']} -> {output}", flush=True)
        try:
            with output.open('x') as stream, stderr_output.open('x') as error_stream:
                process = subprocess.Popen(
                    plan['command'], env=environment, text=True,
                    stdout=stream, stderr=error_stream,
                )
                try:
                    returncode = process.wait(timeout=args.timeout)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    failures += 1
                    print(f"  timed out after {args.timeout:g}s", file=sys.stderr)
                    continue
            if returncode != 0:
                failures += 1
                print(f"  exited {returncode}", file=sys.stderr)
        finally:
            stop_owned_daemon(environment['PI_NODRIVER_SOCKET'])
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
