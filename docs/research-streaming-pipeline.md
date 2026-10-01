# Research: streaming search → crawl → prefill pipeline (2026-10-02)

The `research` tool turns one agent tool call into source-labelled evidence, and
overlaps every stage it can with the model that is still writing or waiting:

```
agent writes tool call ── q1 ──── q2 ── q3 ── q4 ─┐ toolcall_end
                           │ research job starts    │ (execute adopts the running job)
search (3 × 4get + Google) ├─ q1 ─┐ q2 ┐ q3 ┐ q4 ┐  │
crawl (32 pool tabs)        └──────┴────┴────┴────┴── first page ≤10 ms after first result
evidence (append-only)      snippets → page passages → "End of evidence" footer
GPU prefill (Pi core API)       warm … warm … warm … → final request reuses the slot
```

## Agent contract

- Parameters are four strings `q1`–`q4` (2–6 keywords each, concrete dates), not
  an array: llama.cpp streams one finished string parameter at a time, so q1
  reaches the extension while q2–q4 are still being decoded.
- The extension injects the local `Today:` line into the system prompt, so the
  agent resolves 今天/明天/這禮拜 itself; no `gettime` call is needed before research.
- `research` is the default web lookup (`promptSnippet`/`promptGuidelines`);
  `google_search` is for explicit Google requests only.
- The evidence ends with `End of evidence …` telling the agent to answer now.
  Status, stop reason, unread sources and the artifact path stay in the tool
  `details`; printing them in the text sent agents off to crawl again.
- `RESEARCH_DONE_GUARD` (`tool_call` hook): after a successful research in the
  current user message, `crawl`, `google_search`, `fetch_image(s)` and a second
  `research` are blocked unless that message asks for more (URL, Google, 爬, 圖, 再查…).

## Speculative job

`message_update` watches the streaming tool call. When `"q1": "…"` is complete the
extension starts the job in streaming mode (`worker.research(..., streaming: true)`);
later queries join with the `research-add-query` worker action and `toolcall_end`
closes the stream. `execute()` adopts the job, replays the evidence committed so
far into the prefill handle and awaits it. A tool call that never executes is
cancelled at `message_end`.

## Search → crawl

- Ranker `none` (default): results are crawled in search order, interleaving each
  query's 1st result, then 2nd, …; duplicate URLs are crawled once.
- `rank_while_searching`: a page is dispatched as soon as its search returns, not
  after the whole search wave (`RESEARCH_SEARCH_CRAWL_PIPELINE=1`, default).
- Crawls use a dedicated tab pool that is not part of the interactive tab LRU
  (`PI_NODRIVER_CRAWL_POOL_TABS`, default 48); leftover pool tabs are swept when a
  job starts. Pool tabs are created outside `tab_management_lock`.
- Research crawls reuse a browser liveness probe up to 30 s old
  (`ensure_browser(max_age=…)`), probed once in the background at job start. A
  per-page CDP probe held the lifecycle lock for ~0.26 s and serialized the first
  crawl wave behind it.

## Evidence

`research/passages.py` `ProgressivePassages` (BM25 over ~900-char chunks, CJK
bigrams). Top-12 search snippets are committed at search time; in `anchor` mode a
snippet only locates its passage in the crawled page. Committed text is
append-only, so every prefix already sent to the GPU stays valid.
`RESEARCH_EVIDENCE_BUDGET` (default 6000) caps the committed evidence.

## Prefill

Prefill is owned by the Pi harness (`ctx.prefill`, pi-coding-agent `feat/prefill-api`,
`prefill` setting). The extension only reports committed text
(`begin` / `append` / `end`); Pi previews the exact next request, warms its slot
and pins the final request. Sibling tool calls in the same assistant message
(e.g. `gettime` + `research` in parallel) are supported: the preview waits for
the real results of calls placed before `research` and ignores calls after it.
Verified live: with parallel `gettime` + `research` the final request prefills
69 tokens instead of ~5,400.

## Settings

| Variable | Default | Effect |
|---|---|---|
| `RESEARCH_CRAWL_CONCURRENCY` | 32 | parallel crawl tabs per job |
| `RESEARCH_CRAWL_WORD_BUDGET` | 20000 | stop dispatching crawls after this many words |
| `RESEARCH_CRAWL_TIMEOUT` | 3 | per-page load timeout (s) |
| `RESEARCH_CRAWL_TOP_PER_QUERY` | 0 (off) | crawl only each query's top-N results |
| `RESEARCH_EVIDENCE_BUDGET` | 6000 | committed evidence budget |
| `RESEARCH_SNIPPET_MODE` | `anchor` | `anchor` or `commit` for top-12 snippets |
| `RESEARCH_SEARCH_CRAWL_PIPELINE` | 1 | crawl as each search returns |
| `RESEARCH_PARTIAL_ON_TIMEOUT` | unset | capture partially loaded pages on timeout (hurt quality) |
| `PI_NODRIVER_CRAWL_POOL_TABS` | 48 | crawl pool size |

## Measurements (qwen-mtp 35B on gfx1151, real Pi binary, live 4get/Google/Chrome)

Cold-start Pi with only the research tool, thinking off, 10 questions, medians:

| | tool done | first answer token | total |
|---|---|---|---|
| search only after the tool call is written | 6.92 s | — | 13.68 s |
| speculative job (q1) + browser-probe fix | 6.05 s | 7.70 s | 13.11 s |

First crawl after the first search result: ≤0.01 s in 10/10 runs (was up to
0.27 s before the probe fix). Concurrency sweep 8/16/24/32: crawl window
5.5 → 3.4 s at a 75–82 % page success rate; median page load ~1.4 s, p90 ~2.5 s.

Per-query cap (`RESEARCH_CRAWL_TOP_PER_QUERY`), same 10 questions interleaved:

| cap | pages tried | pages ok | evidence tokens | first answer token |
|---|---|---|---|---|
| off | 32.5 | 17 | 6,729 | 8.17 s |
| 5 | 20 | 17 | 5,360 | 7.56 s |
| 3 | 12 | 11 | 4,920 | 7.16 s |
| 2 | 8 | 7 | 4,242 | 6.84 s |

Pages beyond the top 5 were mostly ones that missed the 3 s timeout. Answer
quality (one run per arm, subjective 0–5 per question) did not drop with a cap of 5.

Resident Pi (RPC) with the full `~/.pi/agent` config: the ~23k-token system prompt
is cached, but every evidence token is prefilled at that depth, so prefill falls
from ~1,700 to ~1,050 tok/s and ~3 s of prefill remain after the tool ends
(first answer token 7.70 → 10.83 s median). A smaller system prompt or less
evidence shortens this directly.

## Fixed along the way

- Crawled pages can contain raw U+2028/U+0085 (kept by `json.dumps(ensure_ascii=False)`).
  A `str.splitlines()` reader of the evidence JSONL split a record there and failed
  the job with `JSONDecodeError`; every remaining reader splits bytes. Regression
  test: `test_progressive_job_survives_unicode_line_separators_in_page_text`.
