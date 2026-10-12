"""Operator-requested worker replacement, preserving existing Chrome and Xvfb.
Never invokes Browser.close, restarts Chrome, or touches its profile/cookies.
Only for the legacy layout (worker spawned as a plain child of a Pi process); a worker in
its own pi-nodriver-browser-* unit cannot be replaced without stopping that unit.
"""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import time
import urllib.request

HOME = Path.home()
ROOT = HOME / '.pi/agent/extensions/nodriver-browser'
SOCKET = HOME / '.pi/agent/nodriver-browser.sock'
UNIT = 'nodriver-browser-worker.service'


def argv(pid):
    return (Path('/proc') / str(pid) / 'cmdline').read_bytes().decode().strip('\0').split('\0')


def browser_unit_of(cgroup):
    """Dedicated browser unit (worker-launch.ts) of a worker, or None for the legacy layout."""
    match = re.search(r'/(pi-nodriver-browser-[0-9a-f]+\.service)$', cgroup.strip())
    return match.group(1) if match else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', required=True, type=int)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--reload', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('invalid CDP port')
    with urllib.request.urlopen(f'http://127.0.0.1:{args.port}/json/version', timeout=2) as response:
        if 'Chrome/' not in json.load(response).get('Browser', ''):
            raise ValueError('healthy existing Chrome required')
    info = dict(line.split('=', 1) for line in (SOCKET.parent / 'nodriver-browser.env').read_text().splitlines() if '=' in line)
    pid = int(info['PID'])
    proc = Path('/proc') / str(pid)
    if proc.stat().st_uid != os.getuid():
        raise ValueError('worker must belong to current user')
    expected = [str(ROOT / '.venv/bin/python'), str(ROOT / 'worker.py'), '--server', str(SOCKET)]
    if argv(pid) != expected:
        raise ValueError('worker argv mismatch; refusing process control')
    # In its own unit the worker's xvfb-run wrapper is the unit's main process: killing it
    # ends the unit, and systemd then stops Xvfb and every Chrome helper too.
    browser_unit = browser_unit_of((proc / 'cgroup').read_text())
    if browser_unit:
        raise ValueError(
            f'worker runs in its own browser unit {browser_unit} together with Xvfb and Chrome; '
            'replacing only the worker would stop Chrome as well. To restart the whole browser '
            f'(profile and cookies are kept, open tabs are not): systemctl --user stop {browser_unit}')
    status = dict(line.split(':', 1) for line in (proc / 'status').read_text().splitlines() if ':' in line)
    parent = int(status['PPid'])
    parent_args = argv(parent)
    wrapper = '/usr/bin/xvfb-run' in parent_args and str(ROOT / 'worker.py') in parent_args
    active = subprocess.run(['systemctl', '--user', 'is-active', UNIT], capture_output=True, text=True).stdout.strip() == 'active'
    if active and not args.reload:
        raise ValueError('managed worker already active; use explicit --reload for an attached worker')
    env = {}
    for field in (proc / 'environ').read_bytes().split(b'\0'):
        key, sep, value = field.partition(b'=')
        if sep and key.decode().startswith(('PI_NODRIVER_', 'PI_RESEARCH_', 'RESEARCH_')):
            env[key.decode()] = value.decode()
    if args.reload and (not active or env.get('PI_NODRIVER_ATTACH_PORT') != str(args.port)):
        raise ValueError('reload requires the active, attach-only managed worker on the same Chrome port')
    env.update(DISPLAY=info['DISPLAY'], XAUTHORITY=info['XAUTHORITY'], PI_NODRIVER_ATTACH_PORT=str(args.port))
    if not Path(env['XAUTHORITY']).is_file():
        raise ValueError('existing Xauthority missing')
    print(json.dumps({'worker_pid': pid, 'xvfb_wrapper': parent if wrapper else None, 'display': env['DISPLAY'], 'chrome_restarted': False, 'dry_run': args.dry_run}))
    if args.dry_run:
        return
    # Legacy xvfb-run would tear down the shared display when its worker exits.
    # Kill only that verified wrapper without running its cleanup trap, preserving
    # the existing X server/auth file. Then discard only the wedged Python worker.
    if args.reload:
        subprocess.run(['systemctl', '--user', 'restart', UNIT], check=True)
    else:
        if wrapper:
            os.kill(parent, signal.SIGKILL)
        os.kill(pid, signal.SIGKILL)
        subprocess.run(['systemctl', '--user', 'reset-failed', UNIT], capture_output=True)
        command = ['systemd-run', '--user', '--unit=' + UNIT.removesuffix('.service'), '--collect',
                   '--property=Restart=on-failure', '--property=RestartSec=2',
                   '--working-directory=' + str(ROOT)]
        command += ['--setenv=' + key + '=' + value for key, value in env.items()]
        command += expected
        subprocess.run(command, check=True)
    for _ in range(80):
        try:
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(1)
                client.connect(str(SOCKET))
                client.sendall(b'{"id":1,"sessionId":"hub-health","command":"diagnostic-invalid-command"}\n')
                if b'unsupported browser command' in client.recv(65536):
                    print('worker reconnected; socket verified; existing Chrome/display preserved')
                    return
        except OSError:
            pass
        time.sleep(0.1)
    raise RuntimeError('replacement worker socket unavailable; inspect journalctl --user -u ' + UNIT)


if __name__ == '__main__':
    main()
