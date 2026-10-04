#!/usr/bin/env python3
"""Time breakdown analysis and Gantt chart generator for research benchmark runs.

Reconstructs the full multi-lane execution timeline from:
1. cases.jsonl & trace.jsonl (crawling, search, image downloads, focus filtering)
2. *.events.jsonl (Pi tool execution, thinking, streaming text tokens)
3. llama-server journalctl logs (prompt eval ms / tokens, decode eval ms / tokens, speculative warms)

Outputs a publication-quality Gantt chart with TTFT, settlement, and search milestones.
"""

import argparse
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager

# Configure CJK font with fallback
FONT_CANDIDATES = [
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc',
    '/usr/share/fonts/truetype/noto/NotoSansTC-Regular.otf',
]
font_loaded = False
for fpath in FONT_CANDIDATES:
    if os.path.exists(fpath):
        try:
            font_manager.fontManager.addfont(fpath)
            plt.rcParams['font.family'] = 'Noto Sans CJK JP'
            font_loaded = True
            break
        except Exception:
            pass
if not font_loaded:
    plt.rcParams['font.sans-serif'] = ['DejaVu Sans', 'Arial', 'sans-serif']

fmt = lambda t: datetime.datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M:%S')

ROWS = [
    'kv-cache-manager（還原／存檔／檢查）',
    '模型：思考＋呼叫 gettime',
    'gettime 工具',
    '模型：思考＋呼叫 research',
    '搜尋',
    '爬文',
    '背景下載圖片',
    '邊爬邊預填',
    '工具結束後：等 GPU 預填完',
    '模型：思考＋呼叫 crawl',
    '模型：思考＋呼叫 fetch_images',
    '最終 prefill',
    '模型：回答前思考',
    '生成答案（使用者看得到）'
]

COLORS = {
    'kv-cache-manager（還原／存檔／檢查）': '#9a958a',
    '模型：思考＋呼叫 gettime': '#7a9ad8',
    'gettime 工具': '#c9c6bd',
    '模型：思考＋呼叫 research': '#1c5aa6',
    '搜尋': '#1baf7a',
    '爬文': '#eda100',
    '背景下載圖片': '#3aa6b9',
    '邊爬邊預填': '#e87ba4',
    '工具結束後：等 GPU 預填完': '#f3c4d6',
    '模型：思考＋呼叫 crawl': '#c0504d',
    '模型：思考＋呼叫 fetch_images': '#7c4dff',
    '最終 prefill': '#2a78d6',
    '模型：回答前思考': '#8e8bb0',
    '生成答案（使用者看得到）': '#5f5e57'
}


def load_case(root: Path, case_name: str, systemd_unit: str):
    case_dir = root / case_name
    cases_file = case_dir / 'cases.jsonl'
    events_file = case_dir / f'{case_name}.events.jsonl'
    trace_file = case_dir / 'trace.jsonl'

    if not cases_file.exists():
        raise FileNotFoundError(f"Missing {cases_file}")
    if not events_file.exists():
        raise FileNotFoundError(f"Missing {events_file}")

    cs = [json.loads(l) for l in cases_file.open()]
    t0 = cs[0]['time']
    end = cs[-1]['time']
    ev = [json.loads(l) for l in events_file.open()]
    tr = [json.loads(l) for l in trace_file.open()] if trace_file.exists() else []

    # Query llama-server logs via journalctl
    q = []
    pending = None
    try:
        proc = subprocess.run(
            ['journalctl', '-u', systemd_unit, '--since', fmt(t0 - 1), '--until', fmt(end + 1), '--no-pager', '-o', 'short-unix'],
            capture_output=True, text=True, check=False
        )
        for l in proc.stdout.splitlines():
            m = re.match(r'([\d.]+) .*?(prompt eval|       eval) time =\s+([\d.]+) ms /\s+(\d+) tokens', l)
            if not m or float(m[1]) < t0:
                continue
            if m[2] == 'prompt eval':
                pending = (float(m[3]) / 1000, int(m[4]))
            elif pending:
                fin = float(m[1])
                dd = float(m[3]) / 1000
                pd, pt = pending
                pending = None
                q.append(dict(end=fin, dstart=fin - dd, pstart=fin - dd - pd, ptok=pt, dtok=int(m[4]), warm=int(m[4]) <= 1))
    except Exception as exc:
        print(f"Warning: Failed to read journalctl logs: {exc}", file=sys.stderr)

    reqs = [r for r in q if not r['warm']]
    warms = [r for r in q if r['warm']]

    tools = []
    for r in ev:
        e = r['event']
        if e.get('type') == 'tool_execution_start':
            tools.append(dict(name=e.get('toolName'), start=r['time']))
        if e.get('type') == 'tool_execution_end':
            matches = [t for t in tools if t['name'] == e.get('toolName') and 'end' not in t]
            if matches:
                matches[0]['end'] = r['time']

    text_start = next((
        r['time'] for r in ev
        if r['event'].get('assistantMessageEvent', {}).get('type') == 'text_start'
        and r['time'] > max([t['end'] for t in tools] or [t0])
    ), None)

    iv = {}
    lab = {}
    research_end = t0

    def add(k, a, b, l=None):
        iv.setdefault(k, []).append((a - t0, b - t0))
        if l:
            lab[k] = l

    prev = t0
    for i, r in enumerate(reqs):
        called = [t for t in tools if t['start'] >= r['end'] - 0.05 and t['start'] <= r['end'] + 0.5]
        tool = next((t for t in called if t['name'] == 'research'), called[0] if called else None)
        add('kv-cache-manager（還原／存檔／檢查）', prev, r['pstart'])
        for t in called:
            if t is not tool:
                add(f'{t["name"]} 工具', t['start'], t['end'])
        if tool:
            names = '＋'.join(t['name'] for t in called)
            add(f'模型：思考＋呼叫 {tool["name"]}', r['pstart'], r['end'],
                f'{r["end"] - r["pstart"]:.2f}s · 讀 {r["ptok"]} · 寫 {r["dtok"]} token' + (f'（同時呼叫 {names}）' if len(called) > 1 else ''))
            if tool['name'] == 'research':
                js = min([x['time'] for x in tr if x['event'] == 'job_start'] or [t0])
                for x in tr:
                    if x['event'] in ('search_end', 'google_search_end'):
                        add('搜尋', x['time'] - x['seconds'], x['time'])
                cr = [(x['time'] - x['seconds'], x['time']) for x in tr if x['event'] == 'crawl_end']
                if cr:
                    add('爬文', min(a for a, _ in cr), max(b for _, b in cr),
                        f'{max(b for _, b in cr) - min(a for a, _ in cr):.2f}s · {len(cr)} 頁')
                im = [x for x in tr if x['event'] in ('image_end', 'image_error')]
                for x in im:
                    add('背景下載圖片', x['time'] - x['seconds'], x['time'])
                if im:
                    lab['背景下載圖片'] = f"{len(im)} 張 · 成功 {sum(1 for x in im if x['event'] == 'image_end')} · 最長 {max(x['seconds'] for x in im):.2f}s"
                ws = [w for w in warms if js <= w['end'] and (i + 1 >= len(reqs) or w['end'] <= reqs[i + 1]['pstart'])]
                for w in ws:
                    add('邊爬邊預填', w['pstart'], w['end'])
                if ws:
                    lab['邊爬邊預填'] = f'{len(ws)} 次 · {sum(w["ptok"] for w in ws):,} token · {sum(w["end"] - w["pstart"] for w in ws):.1f}s'
                wend = max([w['end'] for w in ws] + [tool['end']])
                add('工具結束後：等 GPU 預填完', tool['end'], wend, f'{wend - tool["end"]:.2f}s')
                prev = wend
                research_end = tool['end']
            else:
                add(f'{tool["name"]} 工具', tool['start'], tool['end'])
                prev = tool['end']
        else:
            add('最終 prefill', r['pstart'], r['dstart'], f'{r["dstart"] - r["pstart"]:.2f}s · {r["ptok"]} token')
            ts = text_start or r['dstart']
            add('模型：回答前思考', r['dstart'], ts)
            add('生成答案（使用者看得到）', ts, r['end'], f'{r["end"] - ts:.1f}s · 共 {r["dtok"]} token')

    kv = iv.get('kv-cache-manager（還原／存檔／檢查）', [])
    if kv:
        lab['kv-cache-manager（還原／存檔／檢查）'] = f'{len(kv)} 次 · 共 {sum(b - a for a, b in kv):.2f}s'

    searches = [x['time'] for x in tr if x['event'] in ('search_end', 'google_search_end')]
    first_search = min(searches) - t0 if searches else 0
    first_answer = (text_start or 0) - t0
    total_time = end - t0

    return iv, lab, total_time, first_answer, research_end - t0, first_search


def render_gantt(cases_data, case_names, out_path: Path, title: str):
    active_rows = [r for r in ROWS if any(r in iv for iv, _, _, _, _, _ in cases_data)]
    xmax = max(tot for _, _, tot, _, _, _ in cases_data) + 4
    fig, axes = plt.subplots(len(case_names), 1, figsize=(13, 4.6 * len(case_names) + 0.9), sharex=True, facecolor='#fcfcfb')
    axes = [axes] if len(case_names) == 1 else axes

    for ax, name, (iv, lab, tot, ft, je, fs) in zip(axes, case_names, cases_data):
        ax.set_facecolor('#fcfcfb')
        for r, row_name in enumerate(active_rows):
            for a, b in iv.get(row_name, []):
                ax.barh(r, max(b - a, 0.04), left=a, height=0.6, color=COLORS.get(row_name, '#888'), edgecolor='#fcfcfb')
            if iv.get(row_name):
                e = max(b for _, b in iv[row_name])
                s = min(a for a, _ in iv[row_name])
                ax.text(e + 0.25, r, lab.get(row_name) or f'{e - s:.2f}s', va='center', fontsize=9.5, color='#3d3d3a', zorder=4,
                        bbox=dict(facecolor='#fcfcfb', edgecolor='none', pad=1.0))
        if fs > 0:
            ax.axvline(fs, color='#1baf7a', lw=1.1, ls=':')
            ax.text(fs + 0.1, -0.75, f'第一個搜尋結果 {fs:.1f}s', color='#137a55', fontsize=9)
        if je > 0:
            ax.axvline(je, color='#b3261e', lw=1.1)
            ax.text(je + 0.1, len(active_rows) - 0.35, '工具結束', color='#b3261e', fontsize=9)
        if ft > 0:
            ax.axvline(ft, color='#7b3fbf', lw=2.2, zorder=2)
            ax.text(ft + 0.1, -0.75, f'答案第一個字 {ft:.1f}s', color='#7b3fbf', fontsize=10.5, fontweight='bold')
        ax.axvline(tot, color='#0b0b0b', ls=(0, (4, 3)))
        ax.text(tot + 0.25, 1, f'總計 {tot:.1f}s', fontweight='bold', bbox=dict(facecolor='#fcfcfb', edgecolor='none', pad=1.0))

        ax.set_yticks(range(len(active_rows)))
        ax.set_yticklabels(active_rows)
        ax.set_ylim(len(active_rows) - 0.3, -1.1)
        ax.set_title(f'Case: {name}', loc='left', fontweight='bold')
        ax.grid(axis='x', color='#e4e3dc')
        ax.set_axisbelow(True)
        for s in ('top', 'right', 'left'):
            ax.spines[s].set_visible(False)

    axes[-1].set_xlabel('時間（秒，從送出訊息起算）')
    axes[-1].set_xlim(0, xmax)
    fig.suptitle(title, x=0.012, ha='left', fontsize=14, fontweight='bold')
    fig.text(0.012, 0.985 - 0.5 / (4.6 * len(case_names) + 0.9),
             '時間來源：Pi 事件流 (*.events.jsonl)、Nodriver trace.jsonl 與 llama-server prompt eval 記錄。',
             fontsize=10, color='#5f5e57', va='top')
    fig.tight_layout(rect=(0, 0, 1, 1 - 1.3 / (4.6 * len(case_names) + 0.9)))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=140)
    print(f"Saved Gantt chart: {out_path}")


def main():
    parser = argparse.ArgumentParser(description="Generate Time Breakdown Gantt Chart from benchmark run.")
    parser.add_argument("run_dir", type=Path, help="Directory containing case subdirectories (e.g. q1, q2)")
    parser.add_argument("cases", nargs="?", default="", help="Comma-separated case list (e.g. q1,q2). Default: auto-detect.")
    parser.add_argument("--out", type=Path, default=None, help="Output PNG path (default: <run_dir>/time-breakdown.png)")
    parser.add_argument("--title", default="Time Breakdown Analysis", help="Gantt chart title")
    parser.add_argument("--unit", default=os.environ.get("LLAMA_SYSTEMD_UNIT", "qwen-mtp"), help="llama-server systemd unit name")
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    if not run_dir.exists():
        sys.exit(f"Error: run_dir does not exist: {run_dir}")

    if args.cases:
        case_names = [c.strip() for c in args.cases.split(',') if c.strip()]
    else:
        case_names = sorted([
            d.name for d in run_dir.iterdir()
            if d.is_dir() and (d / 'cases.jsonl').exists() and d.name != 'warmup'
        ])

    if not case_names:
        sys.exit(f"Error: No cases found in {run_dir}")

    out_file = args.out or (run_dir / "time-breakdown.png")
    print(f"Analyzing {len(case_names)} cases in {run_dir}: {case_names}")

    cases_data = []
    for c in case_names:
        data = load_case(run_dir, c, args.unit)
        cases_data.append(data)
        _, lab, tot, ft, je, fs = data
        print(f"\n--- Case {c} ---")
        print(f"  First Search  : {fs:.2f}s")
        print(f"  Tool Execution: {je:.2f}s")
        print(f"  First Token   : {ft:.2f}s (TTFT)")
        print(f"  Total Duration: {tot:.2f}s")
        for k, v in lab.items():
            print(f"    • {k}: {v}")

    render_gantt(cases_data, case_names, out_file, args.title)


if __name__ == '__main__':
    main()
