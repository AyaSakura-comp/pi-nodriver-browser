# Pi Nodriver Browser

A Pi extension that gives the agent a real, logged-in Chrome (headful, on Xvfb) for
web research and for doing things on websites.

Tools: `research` (default web lookup), `browser_intent` (browser autopilot),
`crawl`, `google_search` (only when the user asks for Google), `fetch_image(s)`.

## Layout

```
index.ts               Pi extension: tools, research guards, worker client
browser-intent-tool.ts browser_intent tool (Pi side)
browser-agent.ts       the autopilot loop (no Pi imports; runs standalone too)
worker-launch.ts       starts the worker in its own systemd unit
worker.py              socket server + BrowserService (sessions, crawl, search, research engine)
pibrowser/cdp.py       CDP client: one websocket, flat sessions, a timeout on every call
pibrowser/chrome.py    launch / probe / kill Chrome
pibrowser/page.py      one tab: navigate, wait, isolated-world scripts, mouse/keyboard
pibrowser/fetch.py     image and PDF downloads (public addresses only)
pibrowser/js/          snapshot, Google result and image-candidate scripts
research/              research pipeline (unchanged by the browser rewrite)
ops/                   watchdog that revives a dead worker
```

## How the browser stays usable

The worker owns one Chrome and judges it at **page level**: a fresh tab must run
JavaScript (`Chrome.probe`). On 2026-11-03 the kernel OOM killer took Chrome's GPU
process; the browser endpoint kept answering while every tab hung, so the old
browser-level health check called it healthy and every `open` timed out until the
worker was restarted by hand. Now:

- every CDP call has a timeout, so a hung page becomes an error, never a hang;
- a page command that times out triggers a probe; a failed probe kills the whole
  Chrome process group and starts a new Chrome, and `open`/`snapshot`/`crawl` are
  retried once on it (measured: frozen Chrome → `open` succeeded in 15.6 s);
- when idle, Chrome is probed every 5 minutes (`PI_BROWSER_IDLE_PROBE_SECONDS`) and a
  command arriving after 30 s idle is probed first, so a browser that wedged while
  nobody used it is replaced before or at the next request;
- one window per Pi conversation (a background tab pauses `requestAnimationFrame`, which
  left virtual lists half rendered); it stays open when the Pi process exits, so the next
  turn of the same conversation continues on its page (piweb ends Pi after each Life turn
  or 10 idle minutes), and closes after 30 idle minutes (`PI_BROWSER_IDLE_TAB_SECONDS`); crawl
  and search tabs are temporary and always closed, and more than 40 foreign tabs
  (left by other scripts) are trimmed;
- JavaScript dialogs are answered automatically (an open `alert` blocks the page);
  alerts are accepted, confirm/prompt dismissed; the snapshot says what was closed.

Events (start, probe failures, restarts, timeouts) go to stderr (the unit's journal)
and `~/.pi/agent/nodriver-browser-events.jsonl`; Chrome's own log is
`~/.pi/agent/nodriver-chrome.log`.

## browser_intent

`{url?, goal, confirm?}`. One LLM (the conversation's model when it speaks
`/chat/completions`, otherwise `PI_BROWSER_AGENT_URL`/`PI_BROWSER_AGENT_MODEL`, default
local `qwen3.8-flash` on :8090) reads a page snapshot and answers with one JSON action
per step: `click`, `type` (+submit), `select` (dropdowns; on anything else it is a click), `press`, `scroll`, `back`,
`google` (Google search), `open` (only URLs written in the goal), `done`, `stop`. At most 20 steps /
150 s. The snapshot lists visible text in page order with interactive elements inline
as `[eN] role "name"`, so a price sits next to its product link.

Results: `TASK_DONE` (answer whose evidence quotes were found on the page),
`TASK_UNVERIFIED`, `TASK_NEEDS_INPUT` (login/OTP the goal does not give),
`TASK_NEEDS_CONFIRM` (buy/pay/order/delete/log out, or add-to-cart not asked for —
mechanically gated, `confirm=true` lets it through), `TASK_BLOCKED` (CAPTCHA, login or
anti-bot wall), `TASK_STOPPED`, `TASK_FAILED`. A final screenshot is attached.
Secrets (password/OTP/card fields) are typed only when the value is in the goal.

Live check on 22 Taiwanese sites (PChome 24h, momo, Shopee, Yahoo購物, 博客來, 露天,
中央氣象署, 高鐵, 台鐵, 台北捷運, 104, 591, Yahoo新聞, 聯合新聞網, PTT, Dcard, Mobile01,
巴哈姆特, 臺灣銀行匯率, Yahoo股市, 電子發票, 中華電信): `tests/live/agent-harness.ts`.

## Worker commands

Socket `~/.pi/agent/nodriver-browser.sock`, one JSON line per request
`{"id","command","sessionId"}`, reply `__PI_NODRIVER__{json}`.
`open <url>`, `snapshot [--viewport]`, `click <ref>`, `type <ref> <text> [--submit]`,
`select <ref> <option>`, `press <key>`, `scroll down|up|top|bottom`, `back`, `forward`,
`reload`, `wait <ms>`, `get url|title|text`, `screenshot`, `close`, `session-cleanup`,
`crawl <urls>`, `google-search <json>`, `fetch-image <url>`, `browser-health`,
`shutdown`, `research*` (research jobs). Page commands return the new snapshot.

## Install / deploy

```bash
./install.sh            # copies the runtime to ~/.pi/agent/extensions/nodriver-browser, venv, deps
./install.sh --stage DIR  # copy only
```

Deploying over a running worker: copy the files, then stop the
`pi-nodriver-browser-*` user unit; the next tool call (or the watchdog) starts a new
worker. Restarting the worker closes all browser tabs.

Tests: `python -m unittest discover -s tests` (unit), `PI_BROWSER_LIVE=1 python -m
unittest tests.test_worker_live` (real Chrome on local fixture pages).

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PI_NODRIVER_PROFILE` | `~/.pi/agent/nodriver-profile` | Chrome profile (logins live here) |
| `PI_NODRIVER_COMMAND_TIMEOUT` | 75 | seconds per worker command |
| `PI_NODRIVER_MAX_TABS` | 12 | session tabs before the least recently used session is closed |
| `PI_BROWSER_CRAWL_TABS` | 48 | concurrent crawl/search tabs |
| `PI_BROWSER_IDLE_TAB_SECONDS` | 1800 | idle session tab lifetime |
| `PI_BROWSER_HEALTH_INTERVAL` | 60 | background health loop period |
| `PI_BROWSER_IDLE_PROBE_SECONDS` | 300 | probe age that triggers a check while idle |
| `PI_BROWSER_AGENT_URL` / `_MODEL` | :8090 / qwen3.8-flash | autopilot model when the session model has no chat endpoint |
| `PI_NODRIVER_CHROME` | `/opt/google/chrome/chrome` | Chrome binary |

## Research (default web lookup)

`research` is the agent's default web search. The agent writes only four keyword
queries (`q1`–`q4`, concrete dates). `q1` is the exact search: the subject copied
verbatim (一字不差) from the user's message, nothing added; the tool searches (3 fourget + 1 Google),
crawls results in search order through a dedicated 32-tab pool, and returns
append-only, source-labelled evidence (~6,000-token budget) ending with an
explicit "answer now" footer.

Every stage overlaps the next:

- **Speculative start** — the job starts as soon as `q1` has streamed out of the
  tool call; `q2`–`q4` join while they are still being written, and `execute()`
  adopts the running job.
- **Search → crawl pipeline** — each result is crawled the moment its search
  returns (first crawl ≤10 ms after the first result).
- **Crawl → prefill** — evidence is committed while pages arrive and Pi's
  `ctx.prefill` warms the model with it, so the final request only processes the tail.
- **Images** — while pages are crawled, their main images are downloaded in the
  background (they finish inside the crawl window); up to 3 are listed as
  `[[image: …]]` markers and the answer embeds them in the paragraphs they illustrate.

The extension adds nothing to Pi's system prompt: tool rules travel in the tool
descriptions and answer style in the evidence closure. The evidence closes by telling the agent it is enough and how to answer (direct
answer first, only what serves the question, no repetition), and the extension
restates the user's question after the evidence so a 20k-token packet does not
pull the answer off topic. The answer ends by
offering the next step instead of taking it: a deep crawl of specific sources
(full page text) or another research round. The agent never crawls on its own
after research — research already crawled those pages. Guards: `RESEARCH_FIRST` blocks
`google_search`/`crawl` before research unless the user asked for Google or gave a
URL; `RESEARCH_DONE_GUARD` blocks follow-up `crawl`/`google_search`/`fetch_image(s)`/
`research` (and a second `browser_intent`) in the same user message unless the user asks
for more, and tells a retrying agent to stop calling tools. "Asks for more" also
covers 深入/深度/再研究 and a bare yes (好啊/要/OK…) to the agent's "deep crawl or
research more?" offer, and an
image-generation turn (a `create-image`/`qwen-image`/`photo-editing`/`create-gif`/
`image-to-3d` skill was read) may still `crawl`/`fetch_image(s)`/`browser_intent` for
references. `"crawlGuards": false` in `browser-config.json` exempts `crawl` from both
guards; the agent is then only *told* (tool description, evidence closure, guard
messages) not to crawl after research and to offer it instead.

Tunables (all in the [Configuration Reference](#-configuration-reference)): `RESEARCH_CRAWL_CONCURRENCY` (32),
`RESEARCH_CRAWL_TOP_PER_QUERY` (5; 0 = off — each query's top 5 keep answer quality
and cut ~30 % of evidence), `RESEARCH_EVIDENCE_BUDGET` (6000), `RESEARCH_CRAWL_TIMEOUT` (3 s), `RESEARCH_IMAGES` (3; 0 = off).

Design, settings and measurements: [research-streaming-pipeline.md](docs/research-streaming-pipeline.md).
Earlier designs (planner / Laya ranking / extension-side prefill) are kept for
history in [research-ranked-workflow.md](docs/research-ranked-workflow.md) and
[research-progressive-prefill-validation.md](docs/research-progressive-prefill-validation.md).

## History

Before 2026-11-04 the browser was a 10,600-line nodriver worker plus a separate intent
router service (Laya model, e5 embeddings, judge, OmniParser vision fallback, Lens image
search, PDF wiki, Android identity mode, many loop/provenance guards). It was replaced
by this design; see git history for the old code.
