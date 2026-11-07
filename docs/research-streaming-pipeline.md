# Research: streaming search → crawl → prefill pipeline

The `research` tool turns one agent tool call into source-labelled evidence, and
overlaps every stage it can with the model that is still writing or waiting:

```
agent writes tool call ── q1 ──── q2 ── q3 ── q4 ─┐ toolcall_end
                           │ research job starts    │ (execute adopts the running job)
search (3 × 4get + Google) ├─ q1 ─┐ q2 ┐ q3 ┐ q4 ┐  │
crawl (≤32 tabs)            └──────┴────┴────┴────┴── a page starts as soon as its search returns
evidence (append-only)      snippets → page passages → images → answer rules
GPU prefill (Pi core API)       warm … warm … warm … → final request reuses the slot
```

The engine is the `research/` package, run inside `worker.py`:

| module | holds |
|---|---|
| `jobs.py` | `ResearchConnection` (`run`, `add_query`, `close`) and the job: search, crawl, journal, progress frames, packet |
| `providers.py` | `fourget()`, `google()`, `resolve_google_redirect()`, `web_url()` |
| `passages.py` | `ProgressivePassages`: append-only BM25 passage selection |
| `images.py` | `ResearchImages`: background image downloads |
| `capture.py` | `research_capture_js()`: the page text capture `crawl_one` evaluates |

## Request

`index.ts` sends `{jobId, question, queries, clock, answer, crawlConcurrency: 32,
crawlWordBudget: 20000}`; any other key, or a value outside its range (1–4 queries of
≤200 characters, `answer` breadth or depth, concurrency 1–64, word budget
1000–100000, a clock with a zone), is `invalid_research_input`. `jobId` is also the
name of the job's artifact directory.

Speculative job: when `"q1": "…"` of the streaming tool call is complete, the
extension starts the job with `streaming: true`; `research-add-query` delivers q2–q4
by index and `{done: true}` closes the stream. `execute()` adopts the job and replays
the evidence committed so far into its prefill handle. A call that never executes is
cancelled. One job runs per worker connection, at most two per worker.

## Search → crawl (`jobs.py`)

- Query index 0–2 goes to 4get (its DuckDuckGo scraper on 127.0.0.1:8088), index 3 to
  Google in a temporary background tab of the shared browser (`search_one`). Google's
  opaque `/goto` links are resolved to their 302 target, so a Google and a 4get result
  for the same page are recognized as one. The same query on the same engine is searched
  once; a job runs at most 8 searches.
- As soon as a search returns, its first 5 new results are queued. The queue is ordered
  by position within the query (every query's 1st result, then its 2nd, …) and up to
  `crawlConcurrency` pages load at once. A page is crawled once (host case and fragment
  do not make a new page), at the exact URL a search engine returned: the queue is the
  only way to a crawl, so a job never opens anything but http(s) search results.
- Once the crawled text reaches `crawlWordBudget` words (rough count: a CJK character
  is a word), no new crawl or search starts; pages already loading are kept in full.
- Each page gets `crawl_one` with a 3 s load deadline and a capture of up to 1M UTF-16
  units; a truncated or failed page commits no passage. A search or crawl that takes
  more than 30 s fails. The job reuses a browser liveness probe up to 30 s old and starts
  one full probe in the background (a per-page probe held the lifecycle lock for ~0.26 s
  and serialized the first crawls). A page that fails with `browserSuspect` waits for a
  full probe and is retried once if Chrome was relaunched: after hours idle the first
  research once got 0 of 15 pages from a hung Chrome.
- Every snippet and full page is written to `research-artifacts/<jobId>/evidence.jsonl`
  (directory mode 0700): the artifact behind a packet that is too large.

## Evidence (`passages.py`)

BM25 over ~900-character chunks with CJK bigrams, scored with corpus statistics
(search snippets and every page so far). The snippets of the first 12 results are
committed when their search returns; a snippet otherwise only locates its passage in
the crawled page. Each of those 12 results reserves budget until its page arrives or
fails, so a slow official page still finds room; half the 6000-token budget is free for
whichever pages arrive first. A page commits its best passage and up to two more.
Committed text is never changed or reordered, so every prefix already sent to the GPU
stays valid, and nothing is added after the crawl.

Every change sends a progress frame `{type: "progress", id, jobId, phase: "evidence",
prefix}`; `prefix` is the local date line plus everything committed so far.

## Images (`images.py`)

Each crawled page offers its image candidates (extracted by the crawler). Up to 2 per
page (main/content images; no logos, icons, SVGs, thumbnails or images under 200 px),
ranked by role and alt/caption overlap with the question, are downloaded in the
background through `run_fetch_image` into the Pi session's download folder, at most 6
per job. A finished download from a page whose passage is already committed is
appended at once; after crawling, downloads still running get 1 s, and up to 3 images
in total (duplicates by content hash dropped) are listed as `[[image: …]]` markers,
which piweb and piscord render.

## Packet and result

The packet is the last prefix, any remaining images, then the answer rules: focus on
the question asked, breadth (an overview of every item, one or two lines each) or depth
(the item asked about in full), no repetition, a `[來源](URL)` after every key fact, and
a closing line that offers a deeper crawl or another round instead of taking it. Status,
stop reason and the artifact path stay out of the text: printed there they sent agents
off to crawl again.

The reply carries `action: "research"`, `jobId`, `text`, `status` (`collected` once a
page was read, else `incomplete`), `reason` (`searches_done`,
`crawl_word_budget_reached` or `packet_too_large`), `fullEvidenceDelivered`, and the
details `artifactPath`, `sha256`, `budgetUsed`, `attempts`, `sources`,
`authorizations` (each exact URL with the searches that returned it), `errors`,
`crawlWords`, `crawlWordBudget` and `crawlConcurrency`. A packet over 2 MiB or 20,000
lines is not delivered: `packet_too_large`, with the journal kept for recovery.

A cancelled job cancels its searches and crawls, waits for them to close their tabs,
then closes anything else its owner id still holds (`cleanup_research_owner`). A second
cancel cannot interrupt that. If it does not finish within 5 s the task is kept in
`research_cleanup_tasks` until it does and the job fails with
`research_cleanup_incomplete`. Results that arrive after a job ended are dropped.

## Prefill

Prefill is owned by the Pi harness (`ctx.prefill`, pi-coding-agent `feat/prefill-api`).
The extension calls `begin` before the job, `append` for each newly committed part of
the prefix, `end` on the reply and `cancel` when nothing is delivered;
`RESEARCH_ACTIVE_PREFILL=0` turns this off. On providers without slot prefill
(cloud models) `begin()` returns nothing and the tool runs unchanged.

## Measurements & Time Breakdown Evaluation Standard

> **Mandatory Evaluation Standard**: When analyzing latency, regressions, or prefill effectiveness, **always use the Time Breakdown Analysis tool** in [`benchmarks/time_breakdown/`](../benchmarks/time_breakdown/) (see [`docs/research-time-breakdown-analysis.md`](research-time-breakdown-analysis.md)). Single end-to-end duration numbers conflate search, network crawl, GPU prefill, thinking budget, and generation volume. All performance reports must include the multi-lane Gantt chart and metrics (TTFT, settlement wait, warm volume, final prefill).

The numbers below were measured on the earlier implementation of this same pipeline
(2026-10); they explain the fixed values above.

Cold-start Pi with only the research tool, thinking off, 10 questions, medians:

| | tool done | first answer token | total |
|---|---|---|---|
| search only after the tool call is written | 6.92 s | — | 13.68 s |
| speculative job (q1) + browser-probe fix | 6.05 s | 7.70 s | 13.11 s |

First crawl after the first search result: ≤0.01 s in 10/10 runs (was up to
0.27 s before the probe fix). Concurrency sweep 8/16/24/32: crawl window
5.5 → 3.4 s at a 75–82 % page success rate; median page load ~1.4 s, p90 ~2.5 s.

Per-query cap, same 10 questions interleaved:

| cap | pages tried | pages ok | evidence tokens | first answer token |
|---|---|---|---|---|
| off | 32.5 | 17 | 6,729 | 8.17 s |
| 5 | 20 | 17 | 5,360 | 7.56 s |
| 3 | 12 | 11 | 4,920 | 7.16 s |
| 2 | 8 | 7 | 4,242 | 6.84 s |

Pages beyond the top 5 were mostly ones that missed the 3 s timeout. Answer
quality (one run per arm, subjective 0–5 per question) did not drop with a cap of 5.

Background images (resident Pi, full config, thinking minimal 64, cap 5 per query;
"southern events", "new Mac mini", "Taipei 101 fireworks"):

| | events | Mac mini | 101 fireworks |
|---|---|---|---|
| images fetched (ok) | 6 (6) | 6 (6) | 6 (4) |
| slowest download | 1.21 s | 0.80 s | 1.35 s |
| crawl window | 3.53 s | 4.04 s | 3.85 s |
| final prefill | 389 tok | 86 tok | 532 tok |
| first answer character | 12.2 s | 14.7 s | 11.5 s |
| images embedded in answer | 2 | 1 | 1 |

Every download finished inside the crawl window, so the 1 s wait was never used.
The cost is the image list itself (~300–450 tokens of final prefill when it is appended
after crawling). Embedding went from 1/3 answers to 3/3 after content-hash
de-duplication and an explicit closing instruction.
