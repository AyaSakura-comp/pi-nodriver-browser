#!/usr/bin/env python3
"""Run the Long Page Direct Scroll Benchmark through Pi + Qwen."""

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


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', default='local-llama')
    parser.add_argument('--model', default='qwen3.6-35b-q4')
    parser.add_argument('--thinking', default='low')
    parser.add_argument('--timeout', type=float, default=240.0)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'results')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    return args


def owned_daemon_pid(socket_path, proc_root=Path('/proc')):
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
    try:
        Path(socket_path).unlink(missing_ok=True)
    except Exception:
        pass


def main():
    args = parse_args()
    fixture_url = (ROOT / 'long_scroll.html').resolve().as_uri()
    
    prompt = f"""你是 Pi + Qwen 的超長網頁自動化測試員。
開啟 {fixture_url}。這是一個長度達 5000px 的長頁面，請達成以下兩個目標：
1. 尋找【限時閃購專區】並點擊「點我領取閃購折價券 (SAVE500)」。
2. 尋找【VIP 專屬優惠區】並點擊「啟動 VIP 終極特惠」，取得顯示的神秘通關代碼。

操作指引：
- 頁面極長，嚴格禁止反覆上下小幅滾動（3次無互動滾動會觸發 SCROLL_LOOP_GUARD 報錯）。
- 推薦使用 `get text`、`screenshot --full`、或直達跳捲指令（如 `scroll to 2600`、`scroll to-text "限時閃購"`、`scroll to-text "VIP 專屬優惠"`、`scroll 50%`、`scroll 85%`）快速前往目標，再用 click 完成點擊。

完成後請輸出結果 JSON：
{{"success": true或false, "secretCode": "取得的代碼", "notes": "操作總結"}}
"""

    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    trial_id = f'{run_id}-longscroll-{uuid.uuid4().hex[:8]}'
    output_path = args.output_dir.resolve() / f'{trial_id}.jsonl'
    stderr_path = args.output_dir.resolve() / f'{trial_id}.stderr.log'
    runtime_dir = args.output_dir.resolve() / '.runtime' / trial_id
    socket_path = Path(f'/tmp/pi-b-{uuid.uuid4().hex[:8]}.sock')
    profile_dir = runtime_dir / 'profile'
    
    runtime_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        'PI_NODRIVER_SOCKET': str(socket_path),
        'PI_NODRIVER_PROFILE_DIR': str(profile_dir),
    }

    command = [
        'pi', '--provider', args.provider, '--model', args.model,
        '--thinking', args.thinking, '--no-session', '--no-skills',
        '--no-context-files', '--tools', 'browser', '--mode', 'json', '-p', prompt,
    ]

    print(f"=== Starting Long Scroll Benchmark ===")
    print(f"Fixture: {fixture_url}")
    print(f"Model: {args.provider}/{args.model} (thinking={args.thinking})")
    print(f"Trial ID: {trial_id}")
    print(f"Output: {output_path}")

    if args.dry_run:
        print("Dry run complete.")
        return 0

    t0 = time.perf_counter()
    with output_path.open('w', encoding='utf-8') as out_f, stderr_path.open('w', encoding='utf-8') as err_f:
        try:
            proc = subprocess.Popen(
                command,
                env=env,
                stdout=out_f,
                stderr=err_f,
                text=True,
            )
            proc.wait(timeout=args.timeout)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            exit_code = 124
            print(f"Trial TIMED OUT after {args.timeout}s")
        finally:
            stop_owned_daemon(socket_path)

    elapsed = time.perf_counter() - t0
    print(f"Trial finished in {elapsed:.2f}s with exit code {exit_code}")
    
    # Analyze results from jsonl
    actions = []
    final_text = ""
    if output_path.exists():
        for line in output_path.read_text(encoding='utf-8', errors='replace').splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                if data.get('type') == 'tool_call':
                    actions.append(data.get('tool_call', {}).get('arguments', {}).get('command'))
                elif data.get('type') == 'message' and data.get('role') == 'assistant':
                    content = data.get('content', '')
                    if isinstance(content, str):
                        final_text += content
            except Exception:
                pass

    print(f"\n--- Benchmark Summary ---")
    print(f"Elapsed Time: {elapsed:.2f}s")
    print(f"Actions Taken ({len(actions)}):")
    for a in actions:
        print(f"  -> {a}")
    print(f"\nFinal Assistant Response:\n{final_text[:500]}")

    return exit_code


if __name__ == '__main__':
    sys.exit(main())
