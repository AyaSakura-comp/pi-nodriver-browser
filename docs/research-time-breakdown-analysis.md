# Research Time Breakdown Analysis & Evaluation Methodology

> **Standard Directive**: Whenever benchmarking, profiling, or diagnosing research performance, latency regressions, or prefill efficiency, **ALWAYS** use the Time Breakdown Analysis tool (`benchmarks/time_breakdown/`). Single end-to-end duration numbers (`total_seconds`) are explicitly prohibited as standalone performance evidence.

---

## 1. Why Time Breakdown Analysis is Mandatory

In an autonomous multi-stage agent pipeline (`Model Planning` ➔ `Web Search` ➔ `Parallel Crawl` ➔ `Evidence Chunking & BM25` ➔ `GPU KV-Cache Prefill` ➔ `Answer Thinking` ➔ `Answer Generation`), aggregate execution time conflates completely orthogonal subsystems:

1. **Network & External I/O**: Search API roundtrip, target website HTTP latency, Cloudflare challenge gates.
2. **Local Concurrency & IPC**: Chrome DevTools Protocol (CDP) concurrency, tab pool lifecycle locks, Python/Node.js Unix socket IPC.
3. **GPU Compute & Cache Affinity**:
   - Intermediate chunk speculative prefill (`n_predict: 0`)
   - Prefix cache matching vs. cache invalidation
   - Trailing GPU settlement wait (how long the harness waits after the tool ends for the GPU warm queue to catch up)
   - Final prompt prefill latency (tokens processed / ms)
4. **LLM Generation Dynamics**:
   - Model thinking tokens (`thinkingBudgets`: e.g. minimal 128t vs high 4096t)
   - Output token volume (a 3,500-token detailed answer takes longer than a 500-token brief answer, even if the research pipeline was 3x faster)

Without multi-lane breakdown, optimizations in one area (e.g. shaving 2.5s off prefill) can be obscured by sampling divergence (e.g. the model deciding to write 400 more tokens).

---

## 2. Telemetry Ingestion Architecture

The analysis fuses three independent event streams aligned on system timestamps:

```
┌────────────────────────────────┐     ┌───────────────────────────────┐     ┌───────────────────────────────────┐
│   trace.jsonl (Worker/CDP)     │     │ *.events.jsonl (Pi Agent RPC) │     │ journalctl -u qwen-mtp (llama.cpp)│
├────────────────────────────────┤     ├───────────────────────────────┤     ├───────────────────────────────────┤
│ • search_start / search_end    │     │ • tool_execution_start        │     │ • prompt eval time = X ms / Y tok │
│ • crawl_start / crawl_end      │     │ • tool_execution_end          │     │ • speculative warm (dtok <= 1)    │
│ • image_start / image_end      │     │ • text_start (TTFT)           │     │ • eval time = X ms / Y tok        │
│ • job_start / job_end          │     │ • agent_end (Total)           │     │ • timestamp alignment             │
└────────────────┬───────────────┘     └───────────────┬───────────────┘     └─────────────────┬─────────────────┘
                 │                                     │                                       │
                 └─────────────────────────────────────┼───────────────────────────────────────┘
                                                       │
                                                       ▼
                                      ┌───────────────────────────────────┐
                                      │   plot_breakdown.py Analyzer      │
                                      └────────────────┬──────────────────┘
                                                       │
                                                       ▼
                                      ┌───────────────────────────────────┐
                                      │ Multi-Lane Gantt Chart & Metrics  │
                                      └───────────────────────────────────┘
```

---

## 3. Key Metrics to Report in Evaluations

When comparing implementations (e.g. Passive vs. Active Prefill, concurrency sweeps, prompt updates), always extract and report:

1. **TTFT (Time To First Token / 首字出現時間)**: The exact elapsed time from user prompt submission until `text_start` is emitted to the user.
2. **Tool Execution Duration (工具執行耗時)**: Time between `tool_execution_start` and `tool_execution_end`.
3. **Concurrent Crawl Window (爬文時間窗)**: `max(crawl_end) - min(crawl_start)` across all parallel tabs.
4. **Warming Volume & Duration (預填吞吐量)**: Number of background warms, total tokens evaluated, and total GPU warm time while crawling.
5. **Tool Settlement Wait (工具結束後等待 GPU 結算時間)**: Latency from `tool_execution_end` until the in-flight GPU prefill settles. (Active prefill minimizes this to ~1.0s vs 2.5s in passive mode).
6. **Final Prefill Latency (最終 Prefill 延遲)**: Must remain under 0.5s when KV-cache reuse is functioning properly.
7. **Pre-Answer Thinking Duration (回答前思考時間)**: Governed by the thinking budget (128t minimal takes ~1.0–2.0s).
8. **Generation Output Tokens & Rate**: Tokens generated and duration, confirming answer richness.

---

## 4. How to Perform the Analysis

Refer to the tool suite in `benchmarks/time_breakdown/`:

### Automated A/B Evaluation:
```bash
bash benchmarks/time_breakdown/run_ab_comparison.sh /tmp/my-eval
```

### Manual Run & Custom Questions:
```bash
# 1. Run evaluation with active prefill
python3 benchmarks/time_breakdown/bench_resident.py /tmp/eval-run \
  --questions "這禮拜南部有什麼活動" "Apple 新的 Mac mini 規格如何" \
  --active-prefill \
  --thinking minimal

# 2. Render Gantt chart
python3 benchmarks/time_breakdown/plot_breakdown.py /tmp/eval-run \
  --out /tmp/eval-run/gantt.png \
  --title "Time Breakdown Analysis"
```
