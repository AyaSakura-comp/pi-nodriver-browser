#!/usr/bin/env python3
"""Resident Pi RPC benchmark driver for time breakdown analysis.

Executes questions against a persistent resident Pi RPC process with:
- Installed extension code run as an instrumented daemon on an isolated Unix socket.
- Full trace capturing (crawls, searches, images, focus).
- Event streaming recording (*.events.jsonl).
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import time

# Instrumented daemon template
DAEMON_TEMPLATE = r'''
import asyncio, json, pathlib, sys, time
sys.path.insert(0, sys.argv[1])
import worker
from research.jobs import ResearchConnection, _Google
from research.providers import FourgetProvider
from research.decisions import LayaClient

P = pathlib.Path(sys.argv[2])
SOCKET = str(P / 'browser.sock')

def log(event, **data):
    with (P / 'trace.jsonl').open('a') as f:
        f.write(json.dumps(dict(time=time.time(), event=event, **data), ensure_ascii=False) + '\n')

def wrap(cls, name, kind):
    original = getattr(cls, name)
    async def measured(self, *a, **kw):
        begin = time.time()
        log(kind + '_start', query=getattr(a[0], 'query', None) if a else None,
            url=a[0] if a and isinstance(a[0], str) and kind == 'crawl' else None)
        try:
            result = await original(self, *a, **kw)
            detail = {}
            if kind == 'crawl':
                detail = {'ok': result.get('ok'), 'text_chars': len(result.get('text', '')),
                          'error': result.get('error'), 'truncated': result.get('truncated')}
            if kind in ('search', 'google_search'):
                detail = {'results': len(result)}
            log(kind + '_end', seconds=time.time() - begin, **detail)
            return result
        except BaseException as e:
            log(kind + '_error', seconds=time.time() - begin, error=type(e).__name__)
            raise
    setattr(cls, name, measured)

wrap(FourgetProvider, 'search', 'search')
wrap(_Google, 'search', 'google_search')
wrap(LayaClient, 'choose', 'laya')
wrap(worker.BrowserWorker, 'crawl_one', 'crawl')
wrap(worker.BrowserWorker, 'run_fetch_image', 'image')

import research.focus as _fm
_of = _fm.focus_page
async def _tf(q, title, text, **kw):
    begin = time.time()
    log('focus_start')
    r = await _of(q, title, text, **kw)
    log('focus_end', seconds=time.time() - begin, source_chars=len(text),
        focused_chars=(r[1]['focused_chars'] if r else None))
    return r
_fm.focus_page = _tf

from research.controller import ResearchController
_oc = ResearchController.run
async def _tc(self, plan):
    log('job_start')
    r = await _oc(self, plan)
    log('job_end', status=r.snapshot.status, reason=r.snapshot.reason)
    return r
ResearchController.run = _tc

worker.main([sys.argv[0], '--socket', SOCKET, '--profile', str(P / 'chrome-profile')])
'''

DEFAULT_QUESTIONS = [
    "這禮拜南部有什麼活動",
    "Apple 新的 Mac mini 長什麼樣子、規格如何"
]


async def run_benchmark(output_dir: Path, questions: list[str], pi_bin: str,
                        thinking: str, provider: str, model: str, active_prefill: bool):
    output_dir.mkdir(parents=True, exist_ok=False)
    ext_dir = Path.home() / '.pi/agent/extensions/nodriver-browser'
    if not ext_dir.exists():
        repo_dir = Path(__file__).resolve().parents[2]
        ext_dir = repo_dir

    daemon_file = output_dir / 'daemon.py'
    daemon_file.write_text(DAEMON_TEMPLATE)

    env = dict(
        os.environ,
        PI_NODRIVER_SOCKET=str(output_dir / 'browser.sock'),
        PI_NODRIVER_PROFILE=str(output_dir / 'chrome-profile'),
        PI_SKIP_VERSION_CHECK='1',
        PI_TELEMETRY='0',
        RESEARCH_ACTIVE_PREFILL='1' if active_prefill else '0'
    )
    for key in ('PI_SESSION_ID', 'PI_SESSION_FILE', 'PI_PROVIDER', 'PI_MODEL', 'PI_REASONING_LEVEL', 'WAYLAND_DISPLAY'):
        env.pop(key, None)

    daemon_log = (output_dir / 'daemon.log').open('wb')
    daemon_python = ext_dir / '.venv/bin/python'
    if not daemon_python.exists():
        daemon_python = sys.executable

    print(f"[*] Starting instrumented browser worker daemon (active_prefill={active_prefill})...")
    daemon = await asyncio.create_subprocess_exec(
        'xvfb-run', '-a', '-s', '-screen 0 1366x768x24',
        str(daemon_python), str(daemon_file), str(ext_dir), str(output_dir),
        cwd=str(ext_dir), env=env, stdout=daemon_log, stderr=daemon_log, start_new_session=True
    )

    try:
        async with asyncio.timeout(120):
            while not (output_dir / 'browser.sock').exists():
                await asyncio.sleep(0.1)
    except asyncio.TimeoutError:
        os.killpg(daemon.pid, signal.SIGTERM)
        sys.exit("Error: Timed out waiting for browser worker socket.")

    print("[*] Spawning resident Pi RPC process...")
    pi_err = (output_dir / 'pi.stderr').open('wb')
    pi_cmd = pi_bin.split() + [
        '--mode', 'rpc',
        '--session-dir', str(output_dir / 'sessions'),
        '--provider', provider,
        '--model', model,
        '--thinking', thinking
    ]
    pi = await asyncio.create_subprocess_exec(
        *pi_cmd,
        cwd=str(Path.home()), env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=pi_err,
        start_new_session=True, limit=64 * 1024 * 1024
    )

    sink = [None]

    async def pump():
        while True:
            line = await pi.stdout.readline()
            if not line:
                return
            try:
                e = json.loads(line)
            except Exception:
                e = {'raw': line.decode(errors='replace')}
            if sink[0] is not None:
                sink[0].put_nowait((time.time(), e))

    asyncio.create_task(pump())

    async def send(cmd):
        pi.stdin.write((json.dumps(cmd, ensure_ascii=False) + '\n').encode())
        await pi.stdin.drain()

    async def execute_case(name: str, question: str, record: bool):
        q = asyncio.Queue()
        sink[0] = q
        await send({'type': 'new_session'})
        while True:
            t, e = await asyncio.wait_for(q.get(), 60)
            if e.get('type') == 'response' and e.get('command') == 'new_session':
                break
        await asyncio.sleep(1.0)

        case_dir = output_dir / name
        if record:
            case_dir.mkdir(parents=True, exist_ok=True)
            with (case_dir / 'cases.jsonl').open('a') as f:
                f.write(json.dumps(dict(event='start', case=name, time=time.time())) + '\n')

        begin = time.time()
        print(f"[*] Running {name}: {question[:40]}...")
        await send({'type': 'prompt', 'message': question})
        events = []
        while True:
            t, e = await asyncio.wait_for(q.get(), 400)
            events.append((t, e))
            if e.get('type') == 'agent_end':
                break
        end = time.time()
        duration = end - begin
        print(f"[+] Finished {name} in {duration:.2f}s")

        if record:
            with (case_dir / f'{name}.events.jsonl').open('w') as f:
                for t, e in events:
                    f.write(json.dumps(dict(time=t, event=e), ensure_ascii=False) + '\n')
            with (case_dir / 'cases.jsonl').open('a') as f:
                f.write(json.dumps(dict(event='end', case=name, time=end, seconds=duration)) + '\n')
            if (output_dir / 'trace.jsonl').exists():
                traces = [json.loads(l) for l in (output_dir / 'trace.jsonl').open()]
                with (case_dir / 'trace.jsonl').open('w') as f:
                    for r in traces:
                        if begin - 0.5 <= r['time'] <= end + 0.5:
                            f.write(json.dumps(r, ensure_ascii=False) + '\n')

    try:
        print("[*] Running warmup round...")
        await execute_case('warmup', questions[0], False)
        for i, question in enumerate(questions):
            await execute_case(f'q{i + 1}', question, True)
    finally:
        print("[*] Cleaning up processes...")
        try:
            pi.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(pi.wait(), 10)
        except asyncio.TimeoutError:
            os.killpg(pi.pid, signal.SIGTERM)
        try:
            reader, writer = await asyncio.open_unix_connection(str(output_dir / 'browser.sock'))
            writer.write(b'{"id":9999,"command":"shutdown","sessionId":"evaluation-cleanup"}\n')
            await writer.drain()
            await asyncio.wait_for(reader.readline(), 10)
        except Exception:
            pass
        try:
            await asyncio.wait_for(daemon.wait(), 15)
        except asyncio.TimeoutError:
            os.killpg(daemon.pid, signal.SIGTERM)


def main():
    parser = argparse.ArgumentParser(description="Run benchmark and record full telemetry for time breakdown analysis.")
    parser.add_argument("output_dir", type=Path, help="Target directory for benchmark traces and events")
    parser.add_argument("--questions", nargs="*", default=DEFAULT_QUESTIONS, help="Questions to run")
    parser.add_argument("--active-prefill", action="store_true", default=True, help="Enable active prefill (default: True)")
    parser.add_argument("--no-active-prefill", dest="active_prefill", action="store_false", help="Disable active prefill (passive mode)")
    parser.add_argument("--thinking", default=os.environ.get("PI_THINK", "minimal"), help="Thinking level (default: minimal)")
    parser.add_argument("--provider", default="local-llama", help="LLM provider (default: local-llama)")
    parser.add_argument("--model", default="qwen3.6-35b-q4", help="Model name (default: qwen3.6-35b-q4)")
    parser.add_argument("--pi-bin", default=os.environ.get("PI_BIN", "node /home/chihmin/src/pi-agent/packages/coding-agent/dist/cli.js"),
                        help="Path to pi CLI executable")
    args = parser.parse_args()

    asyncio.run(run_benchmark(
        output_dir=args.output_dir.resolve(),
        questions=args.questions,
        pi_bin=args.pi_bin,
        thinking=args.thinking,
        provider=args.provider,
        model=args.model,
        active_prefill=args.active_prefill
    ))


if __name__ == '__main__':
    main()
