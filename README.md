# Pi Nodriver Browser

A Pi extension that gives the agent a real, logged-in Chrome (headful, on Xvfb) for web
research and for doing things on websites.

Tools: `research` (default web lookup), `browser_intent` (browser autopilot), `crawl`,
`google_search` (only when the user asks for Google), `fetch_image(s)`.

## Layout

```
index.ts               Pi extension: tools, guards, worker client
browser-intent-tool.ts browser_intent tool (Pi side)
browser-agent.ts       the autopilot loop (no Pi imports; runs standalone too)
worker-launch.ts       starts the worker in its own systemd unit
worker.py              socket server + BrowserService (sessions, crawl, search, research engine)
pibrowser/cdp.py       CDP client: one websocket, flat sessions, a timeout on every call
pibrowser/chrome.py    launch / probe / kill Chrome
pibrowser/page.py      one tab: navigate, wait, isolated-world scripts, mouse/keyboard
pibrowser/fetch.py     image and PDF downloads (public addresses only)
pibrowser/js/          page snapshot, Google result and image-candidate scripts
research/              research engine (runs inside the worker)
ops/                   watchdog that revives a stopped worker
```

## How the browser stays usable

One worker owns one Chrome for every Pi process and judges it at **page level**: a fresh
tab must run JavaScript (`Chrome.probe`). On 2026-11-03 the kernel OOM killer took
Chrome's GPU process; the browser endpoint kept answering while every tab hung, so a
browser-level health check called it healthy and every `open` timed out. Now:

- every CDP call has a timeout, so a hung page becomes an error, never a hang;
- a page command that times out triggers a probe (one that misses 5 s gets a second, 20 s
  probe: a loaded machine is not a wedged browser, and a restart closes every
  conversation's window); a failed probe kills Chrome's process group, starts a new
  Chrome and retries `open`/`snapshot`/`get`/`screenshot`/`crawl`/`google-search` once;
- idle Chrome is probed every 5 minutes, and a command after 30 s idle is probed first;
- each Pi conversation gets its own window (a background tab pauses
  `requestAnimationFrame`, which left virtual lists half rendered). It stays open when
  the Pi process exits, so the next turn continues on its page, and closes after 30 idle
  minutes. Crawl and search tabs are temporary; more than 40 foreign tabs are trimmed;
- JavaScript dialogs are answered automatically (alerts accepted, confirm/prompt
  dismissed) and listed in the snapshot; open dialogs are listed first;
- a page behind a Cloudflare-style interstitial gets up to 8 s to pass it by itself
  (never reloaded); a crawled challenge page is reported as blocked.

Events (start, probe failures, restarts, timeouts) go to stderr (the unit's journal) and
`~/.pi/agent/nodriver-browser-events.jsonl`; Chrome's own log is
`~/.pi/agent/nodriver-chrome.log`. Why the worker has its own systemd unit:
[docs/shared-browser-restart-isolation.md](docs/shared-browser-restart-isolation.md).

## browser_intent

`{url?, goal, confirm?}`. One LLM (the conversation's model, or local `qwen3.8-flash`)
reads a page snapshot and answers with one JSON action per step (click, type, select,
press, scroll, back, google, open, done, stop) until the goal is met. The snapshot lists
visible text in page order with interactive elements inline as `[eN] role "name"`, so a
price sits next to its product link; item links show where they go (`→ /path`). Results
are `TASK_DONE` (evidence found on the page), `TASK_UNVERIFIED`, `TASK_NEEDS_INPUT`,
`TASK_NEEDS_CONFIRM` (irreversible steps need `confirm=true`), `TASK_BLOCKED`,
`TASK_STOPPED` or `TASK_FAILED`, with a final screenshot.

## research

The default web lookup. The agent writes a few keyword queries; the worker searches
(4get and Google), crawls the results in a dedicated tab pool and streams back
source-labelled evidence with an "answer now" closing. See `research/`.

## Worker protocol

Socket `~/.pi/agent/nodriver-browser.sock` (`worker.py --server <socket>`). One JSON line
per request `{"id", "command", "sessionId", "research"?}`; replies are
`__PI_NODRIVER__{json}` lines with the same id; `{"cancelId": id}` cancels a request.

Page commands (they return the new snapshot): `open <url>`, `snapshot`, `click <ref>`,
`type <ref> <text> [--submit]`, `select <ref> <option>`, `press <key>`,
`scroll down|up|top|bottom`, `back`. Others: `get text|links`, `screenshot`,
`session-cleanup`, `crawl <urls>`, `google-search <json>`, `fetch-image <url>`,
`browser-health`, `shutdown`, and `research`, `research-add-query`, `research-prefetch`.

## Install / deploy

```bash
./install.sh              # copy to ~/.pi/agent/extensions/nodriver-browser, venv, stop the old worker
./install.sh --stage DIR  # copy only
```

The worker keeps running across Pi restarts; new worker code takes effect when it
restarts (`install.sh` shuts it down; or `systemctl --user stop 'pi-nodriver-browser-*'`).
The next tool call starts a new worker. Restarting the worker closes all browser tabs.

Watchdog (revives a stopped worker on the 2nd failed check a minute apart, logs outages
to `~/.local/state/browser-agent-watchdog/events.jsonl`):

```bash
cp ops/browser-agent-watchdog.{service,timer} ~/.config/systemd/user/
systemctl --user enable --now browser-agent-watchdog.timer
```

## Tests

```bash
.venv/bin/python -m unittest discover -s tests                        # Python unit tests
node --experimental-strip-types --no-warnings --test tests/*.test.ts tests/*.mjs
PI_BROWSER_LIVE=1 .venv/bin/python -m unittest tests.test_worker_live  # real Chrome, local fixtures
RUN_SYSTEMD_INTEGRATION=1 .venv/bin/python -m unittest tests.test_systemd_browser_unit
node tests/live/agent-harness.ts <socket> <tasks.json> <out.json>     # browser_intent on real sites
```

The live tests run their own worker, profile and socket in a temporary directory.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PI_NODRIVER_SOCKET` | `~/.pi/agent/nodriver-browser.sock` | worker socket (Pi side, installer, watchdog) |
| `PI_NODRIVER_PROFILE` | `~/.pi/agent/nodriver-profile` | Chrome profile (logins live here) |
| `PI_AGENT_DIR` | `~/.pi/agent` | where the worker keeps its profile, logs, events and downloads |

## History

Before 2026-11-04 the browser was a 10,600-line nodriver worker plus a separate intent
router service (Laya model, embeddings, judge, OmniParser vision fallback and more). It
was replaced by this design; see git history for the old code.
