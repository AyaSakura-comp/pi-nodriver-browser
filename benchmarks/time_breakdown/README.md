# Research Time Breakdown Analysis Tool

A microsecond-accurate, multi-lane execution timeline and Gantt chart analysis tool for `pi-nodriver-browser` research runs.

---

## 🎯 Why Time Breakdown Analysis?

Standard end-to-end benchmark timing (`total_seconds`) is insufficient for diagnosing research performance. A run that takes 45 seconds might be slow because of:
1. Network search/crawl latency
2. Model thinking overhead before issuing tool calls
3. GPU KV-cache thrashing or lack of prefix cache hits
4. Trailing prefill delay when evidence is committed
5. Long answer generation token decoding

This tool fuses three synchronized telemetry streams to reconstruct the exact multi-lane timeline:
- **Nodriver Browser Trace (`trace.jsonl`)**: Precise search durations, per-page parallel crawl windows, image download latencies, and focus-filter operations.
- **Pi Agent RPC Events (`*.events.jsonl`)**: Tool execution boundaries, agent thinking durations, and user-visible streaming text tokens (TTFT - Time To First Token).
- **Inference Server Log (`journalctl -u qwen-mtp` or llama-server)**: Exact prompt evaluation durations (`prompt eval time`), token counts, speculative background warms (`n_predict: 0`), and decode rates.

---

## 📊 Visualized Timeline Lanes

| Timeline Lane | Color | Description |
|---|---|---|
| `kv-cache-manager` | `#9a958a` | State restore, disk checkpointing, and cache slot verification |
| `模型：思考＋呼叫 research` | `#1c5aa6` | Model thinking phase before issuing the research tool call |
| `搜尋` | `#1baf7a` | 4get / Google search API roundtrip |
| `爬文` | `#eda100` | Parallel tab-pool page crawling (shows count and total duration) |
| `背景下載圖片` | `#3aa6b9` | Concurrent background content-image fetching and validation |
| `邊爬邊預填` | `#e87ba4` | Incremental GPU KV-cache warming in the background during crawl |
| `工具結束後：等 GPU 預填完` | `#f3c4d6` | Settlement wait time between tool completion and GPU warm completion |
| `最終 prefill` | `#2a78d6` | Final prompt evaluation latency and token count before answer generation |
| `模型：回答前思考` | `#8e8bb0` | Internal thinking/planning buffer (governed by `thinkingBudgets`) |
| `生成答案（使用者看得到）` | `#5f5e57` | Streaming text decoding phase seen by the user |

**Milestone Markers**:
- **第一個搜尋結果 (Green dotted)**: Timestamp when the first search result settles.
- **工具結束 (Red solid)**: Timestamp when the research tool finishes and delivers evidence.
- **答案第一個字 (Purple thick)**: **TTFT (Time To First Token)** when the user starts seeing output text.
- **總計 (Black dashed)**: Total end-to-end duration.

---

## 🚀 Quickstart Usage

### 1. Automated A/B Comparison (Passive vs Active Prefill)

Run a complete side-by-side benchmark comparing Passive Prefill (`RESEARCH_ACTIVE_PREFILL=0`) against Active Prefill (`RESEARCH_ACTIVE_PREFILL=1`):

```bash
bash benchmarks/time_breakdown/run_ab_comparison.sh /tmp/my-ab-eval
```

Generates:
- `/tmp/my-ab-eval/passive/time-breakdown.png`
- `/tmp/my-ab-eval/active/time-breakdown.png`

### 2. Run a Custom Benchmark

Run specific questions with custom thinking budgets and prefill modes:

```bash
# Active Prefill (Default)
python3 benchmarks/time_breakdown/bench_resident.py /tmp/eval-active \
  --questions "這禮拜南部有什麼活動" "Apple 新的 Mac mini 規格如何" \
  --active-prefill \
  --thinking minimal

# Passive Prefill
python3 benchmarks/time_breakdown/bench_resident.py /tmp/eval-passive \
  --questions "這禮拜南部有什麼活動" "Apple 新的 Mac mini 規格如何" \
  --no-active-prefill \
  --thinking minimal
```

### 3. Generate or Re-generate Gantt Charts

Analyze any completed benchmark directory containing `cases.jsonl` and `*.events.jsonl`:

```bash
# Auto-detects all cases and writes to <run_dir>/time-breakdown.png
python3 benchmarks/time_breakdown/plot_breakdown.py /tmp/eval-active

# Custom title and output path
python3 benchmarks/time_breakdown/plot_breakdown.py /tmp/eval-active q1,q2 \
  --out ./active-gantt.png \
  --title "Time Breakdown: Active Prefill (minimal 128t)"
```
