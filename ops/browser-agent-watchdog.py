#!/usr/bin/env python3
"""Watchdog for the shared browser worker; browser-agent-watchdog.timer runs `check` every minute.

A check asks the worker for `browser-health`: a page-level Chrome probe that relaunches a wedged
Chrome by itself and does not start Chrome for an idle worker. A worker whose socket refuses is
started again on the 2nd consecutive failed check (Pi also starts one on demand, so one miss may
be a restart in progress); a worker that answers but keeps failing is replaced on the 3rd. At most
3 revivals per hour. Outages with their cause (earlyoom, the kernel OOM killer, the exit status in
the journal), revivals and recoveries go to ~/.local/state/browser-agent-watchdog/events.jsonl.
Stdlib only: the unit runs it with /usr/bin/python3.
"""
import contextlib
import datetime as dt
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
EXT = HOME / '.pi/agent/extensions/nodriver-browser'
SOCKET = Path(os.environ.get('PI_NODRIVER_SOCKET', HOME / '.pi/agent/nodriver-browser.sock'))
STATE_DIR = HOME / '.local/state/browser-agent-watchdog'
EVENTS = STATE_DIR / 'events.jsonl'
STATE = STATE_DIR / 'state.json'
UNIT_PREFIX = 'pi-nodriver-browser-'
NAME = 'nodriver-worker'  # "service" of every event, key of the state file
MARKER = b'__PI_NODRIVER__'
MAX_REVIVES_PER_HOUR = 3


def iso(ts):
    return dt.datetime.fromtimestamp(ts).astimezone().isoformat(timespec='seconds') if ts else None


def run(*cmd, timeout=30):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def log(event, **fields):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    record = json.dumps({'ts': iso(time.time()), 'service': NAME, 'event': event, **fields}, ensure_ascii=False)
    with EVENTS.open('a') as handle:
        handle.write(record + '\n')
    print(record)


def memory():
    info = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    return {'mem_available_mb': int(info['MemAvailable'].split()[0]) // 1024,
            'swap_free_mb': int(info['SwapFree'].split()[0]) // 1024}


def health(timeout=60):
    """('ok' | 'unhealthy' | 'dead', detail). A socket that accepts is not enough: on 2026-10-20
    the worker answered while every command failed with "Failed to connect to browser"."""
    request_id = int.from_bytes(os.urandom(3), 'big')
    client = socket.socket(socket.AF_UNIX)
    client.settimeout(2)
    try:
        client.connect(str(SOCKET))
    except OSError as error:
        client.close()
        return 'dead', f'{type(error).__name__}: {error}'[:160]
    with client, client.makefile('rb') as lines:
        try:
            client.settimeout(timeout)
            client.sendall(json.dumps({'id': request_id, 'command': 'browser-health',
                                       'sessionId': '__watchdog__'}).encode() + b'\n')
            reply = next((reply for reply in (json.loads(line[len(MARKER):]) for line in lines
                                              if line.startswith(MARKER)) if reply.get('id') == request_id), None)
        except (OSError, ValueError) as error:
            return 'unhealthy', f'{type(error).__name__}: {error}'[:160]
    if reply is None:
        return 'unhealthy', 'worker closed the connection'
    if not reply.get('ok'):
        return 'unhealthy', f"browser-health: {reply.get('error')}"[:160]
    detail = f"browser {reply.get('browser')}"
    if reply.get('probeMs') is not None:
        detail += f" ({reply['probeMs']} ms, {reply.get('tabs')} tabs)"
    return 'ok', detail


def worker_units():
    out = run('systemctl', '--user', 'list-units', f'{UNIT_PREFIX}*', '--all', '--plain', '--no-legend').stdout
    return [line.split()[0] for line in out.splitlines() if line.strip()]


def main_pid(unit):
    pid = run('systemctl', '--user', 'show', unit, '-p', 'MainPID', '--value').stdout.strip() if unit else ''
    return pid if pid not in ('', '0') else None


def journal(since, *args):
    """[(unix time, message)] of journal entries since `since`."""
    try:
        out = run('journalctl', '--no-pager', '-o', 'json', '--since', f'@{int(since)}', *args, timeout=15).stdout
    except Exception:
        return []
    entries = []
    for line in out.splitlines():
        try:
            record = json.loads(line)
            if isinstance(record.get('MESSAGE'), str):
                entries.append((int(record['__REALTIME_TIMESTAMP']) / 1e6, record['MESSAGE']))
        except (ValueError, KeyError):
            pass
    return entries


def outage(pid, unit, since):
    """(death time or None, cause, evidence) for a worker that stopped after `since`. The death
    time comes from the journal, so the 60 s check interval does not blur the timeline."""
    entries = journal(since, '--user', '-u', unit) if unit else []
    died = next((ts for ts, message in entries if re.search(
        r'Deactivated|Failed with result|Main process exited|Stopped|Consumed .* CPU time', message)), None)
    evidence = [f'{iso(ts)} {message}' for ts, message in entries if re.search(
        r'exited|Failed|Killed|killed|Stopping|Stopped|Deactivated|core|signal|Traceback|Error', message)][-6:]
    if pid:
        hits = [f'{iso(ts)} {message}' for ts, message in journal(since, '-u', 'earlyoom')
                if re.search(rf'process {pid}\b', message)]
        if hits:
            sent = re.search(r'sending (SIG\w+)', ' '.join(hits))
            return died, f"earlyoom-{sent.group(1) if sent else 'kill'}", hits[:3]
        hits = [f'{iso(ts)} {message}' for ts, message in journal(since, '-k')
                if re.search(rf'Killed process {pid}\b', message)]
        if hits:
            return died, 'kernel-oom', hits[:3]
    for line in evidence:
        exited = re.search(r'Main process exited, code=(\w+), status=([\w/]+)', line)
        if exited:
            code, status = exited.groups()
            return died, (f"signal-{status.split('/')[-1]}" if code in ('killed', 'dumped') else f'exit-{status}'), evidence
    return died, 'unknown', evidence


def revive():
    """Stop every worker unit (a broken worker still holds the socket path and the profile),
    then start one the way worker-launch.ts does: in its own unit, so it outlives this run."""
    for unit in worker_units():
        run('systemctl', '--user', 'stop', unit)
    with contextlib.suppress(OSError):
        SOCKET.unlink(missing_ok=True)
    unit = f'{UNIT_PREFIX}{os.urandom(6).hex()}'
    result = run('systemd-run', '--user', '--collect', '--quiet', f'--unit={unit}',
                 '--description=Pi nodriver browser (Xvfb, worker and Chrome; outlives the Pi process that started it)',
                 f'--working-directory={EXT}', '--property=TimeoutStopSec=20', '--',
                 'xvfb-run', '-a', '-s', '-screen 0 1366x768x24',
                 str(EXT / '.venv/bin/python'), str(EXT / 'worker.py'), '--server', str(SOCKET), timeout=20)
    return result.returncode == 0, unit if result.returncode == 0 else (result.stderr or result.stdout)[:200]


def wait_healthy(seconds=30):
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(2)
        if health()[0] == 'ok':
            return True
    return False


def check(state):
    """One check; returns the new state."""
    now = time.time()
    unit = (worker_units() or [None])[0]
    status, detail = health()
    if status == 'ok':
        if state.get('unit') and unit != state['unit'] and not state.get('down_since'):
            # Replaced between two checks (Pi starts a worker on demand): still an outage.
            died, cause, evidence = outage(state.get('pid'), state['unit'], state.get('last_check', now - 120))
            log('restarted_outside_watchdog', previous_pid=state.get('pid'), cause=cause, died_at=iso(died),
                evidence=evidence)
        if detail.startswith('browser relaunched'):
            # The worker survived but its Chrome did not answer and was replaced: every tab was lost.
            log('browser_relaunched', unit=unit, detail=detail, **memory())
        if state.get('down_since'):
            log('recovered', died_at=iso(state['down_since']), detected_at=iso(state['detected_at']),
                downtime_s=round(now - state['down_since']), by=state.get('revived_by', 'itself'))
        return {'last_ok': now, 'last_check': now, 'unit': unit, 'pid': main_pid(unit),
                'restarts': state.get('restarts', [])}

    dead = status == 'dead'
    state.update(last_check=now, fails=state.get('fails', 0) + 1)
    if not state.get('down_since'):
        if dead:
            died, cause, evidence = outage(state.get('pid'), state.get('unit'), state.get('last_ok', now - 3600))
        else:
            died, cause, evidence = None, 'unhealthy (process running, health check failing)', []
        state.update(down_since=died or now, detected_at=now, cause=cause)
        log('down', died_at=iso(died or now), died_at_source='systemd/journal' if died else 'detected',
            detected_at=iso(now), last_ok=iso(state.get('last_ok')), cause=cause, health=detail,
            pid=state.get('pid'), **memory(), evidence=evidence)
    if state['fails'] < (2 if dead else 3):
        return state
    recent = [t for t in state.get('restarts', []) if t > now - 3600]
    if len(recent) >= MAX_REVIVES_PER_HOUR:
        if not state.get('gave_up'):
            state['gave_up'] = True
            log('giveup', restarts_last_hour=len(recent), **memory(),
                hint='restart budget spent; likely memory pressure (earlyoom) or a crash loop')
        return state
    started, message = revive()
    state.update(restarts=recent + [now], revived_by='watchdog')
    healthy = started and wait_healthy()
    log('revive', ok=started, healthy=healthy, detail=message, **memory())
    if not healthy:
        return state
    now = time.time()
    log('recovered', died_at=iso(state['down_since']), detected_at=iso(state['detected_at']),
        downtime_s=round(now - state['down_since']), by='watchdog')
    unit = (worker_units() or [None])[0]
    return {'last_ok': now, 'last_check': now, 'unit': unit, 'pid': main_pid(unit), 'restarts': state['restarts']}


def main():
    if sys.argv[1:] not in ([], ['check']):
        sys.exit('usage: browser-agent-watchdog.py [check]')
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(STATE.read_text()).get(NAME, {})
    except (OSError, ValueError, AttributeError):
        state = {}
    temporary = STATE.with_suffix('.tmp')
    temporary.write_text(json.dumps({NAME: check(state)}, indent=1))
    temporary.replace(STATE)


if __name__ == '__main__':
    main()
