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
drives the browser with JSON actions (click, type, select, press, upload, more, scroll,
back, google, open, done, stop) until the goal is met. Results are `TASK_DONE` (evidence
found on the pages seen), `TASK_UNVERIFIED`, `TASK_NEEDS_INPUT`, `TASK_NEEDS_CONFIRM`
(irreversible steps need `confirm=true`), `TASK_BLOCKED` (the site refuses),
`TASK_UNSUPPORTED` (the tool cannot do a step), `TASK_STOPPED` or `TASK_FAILED`, with a
final screenshot, the files the task produced and a trace of every model call and browser
command.

It is built for an inference engine with one slot whose state cannot be cut back (Strata):

- **One growing conversation.** Each model call repeats what was sent before and adds the
  result of the last action, so the engine reads only the new tokens. Nothing that changes
  per step stands before the page text.
- **Whole pages, read in parts.** A snapshot lists the page's text from top to end, not one
  screen of it, with interactive elements inline as `[eN] role "name"` (a price sits next to
  its product link). The model gets the first 6,000 characters of a new page, then 10,000
  at a time; `more` shows the next part. `scroll` only makes a page load more items.
- **Content before site chrome.** A nav, a page-level header, footer or aside, and blocks of
  more than 12 links inside them are listed last, under `Site menu:`; a block with a field
  to type in (the search box) stays where it is. A drawn value with an `aria-label` (a star
  rating) is read from the label.
- **Updates, not repeats.** An element keeps its ref for as long as its document lives, and
  refs keep counting over the pages of a session. After an action on the same page the
  model gets only the new or changed lines.
- **The conversation is the memory.** Earlier pages stay in it; `done` evidence may come
  from any page of the task. A link is given by its ref (`[e12]` in the answer becomes the
  address): addresses are not in the page text.
- **Several actions in one reply** for a form (an array); it stops at the first action that
  fails or leaves the page, and each action's new lines are shown, not only the last one's.
- **Waits follow the page.** After an action the worker waits for the page to react (a
  navigation that has started counts, however slow the server) and then to stand still; a
  page that shows no reaction within a second is taken as is. Changes under 1% (ad slots,
  tickers) do not restart the quiet period, a page with almost no text waits longer (an app
  shell before its results), and a page that says it is loading ("載入中") is waited for up
  to 3 s. After an action, a quiet page is waited for up to 3.5 s while an XHR/fetch to its
  own site that the action started is still out (other sites' requests are ads and
  trackers). `more` on a page with nothing new looks at it again for 1.5 s.
- **Walls.** A login or verification wall is pointed out to the model; when it is there
  again after the task tried another way in, the task ends (`TASK_NEEDS_INPUT` or
  `TASK_BLOCKED`) without another model call.
- **Guessed addresses.** `open` with an address the model wrote itself, next to a real link
  to the same page, opens the real link; after one guess on a site hit an error page or was
  sent to the home page, further guesses on that site are refused.
- **Evidence.** A quote counts when it is on a page seen in the task: whole, or as pieces in
  order with at most 40 characters between them (a quote that joins lines or drops an icon).
- **No loops.** An action already done on the same page state is not run again; the third
  time it is chosen the task stops.
- **Outside the page.** What a click does is not always on the page, and a snapshot shows
  only the page: a click on ibon's "新增檔案" opened Chrome's file chooser, the planner saw
  "no visible change", and the task ended as "the site blocks automation" with the chooser
  left open (2026-11-14). The worker now follows every way an action leaves the page, and
  each one is either done or said in the action's result:

  | what the action did | how the worker knows | what happens |
  |---|---|---|
  | opened a file chooser (`<input type=file>`, hidden or made by script) | `Page.fileChooserOpened`; the chooser is intercepted, so no system window opens | `upload` gives it the files; a plain `click` is told how to go on |
  | a box that only takes a dropped file | no chooser and no file field after the click | `upload` drops the file onto it (`Input.dispatchDragEvent`) |
  | started a download | `Browser.downloadWillBegin/Progress` | saved under `nodriver-downloads/<session>/`, path in the result |
  | called `print()` | `print` is replaced before the page's scripts run (Chrome's preview holds the click until a person closes it: 10 s timeout measured) | the page is saved as a PDF, path in the result |
  | opened a confirm box | `Page.javascriptDialogOpening` | answered Cancel and shown; the same action with `"dialog":"ok"` answers OK |
  | a link or script to another program (`mailto:`, `tel:`, `line://`) | the link's address; `Page.frameRequestedNavigation` | the link is not clicked and its address is given; a script's try is reported |
  | copied to the clipboard | the display's clipboard before and after | the copied text is in the result |
  | opened a new tab | `Target.targetCreated` | the session moves to it |
  | a server's HTTP sign-in box | a 401 with `WWW-Authenticate` on the main document | `TOOL LIMIT`: named as what it is |
  | the File System Access picker | a chooser event without an input | `TOOL LIMIT` |
  | any other system window | a new dialog-type window on the worker's X display (a plain click opens none) | `TOOL LIMIT`: its title is reported and it is closed |

  A stop right after a `TOOL LIMIT` line ends the task as `TASK_UNSUPPORTED`, whatever the
  planner calls it: Pi tells the user which step is left to do by hand instead of saying
  that the site refuses automation. `upload` takes only file paths written in the goal, so
  neither a page nor the planner can pick a file.
  None of this is in the planner's rules. Three rule lines about uploads, confirm boxes and
  tool limits made the planner act more on tasks that have none of them (THSR: 20 model
  calls instead of 5; UDN: 2 instead of 1; with the rules as before, 5 and 1 again, run back
  to back). Each result line says how to go on when it happens, and a goal that names an
  existing local file gets the upload action in its first message. A canvas and a frame of another site are listed as
  lines that say their content cannot be read. The looks at the display cost 8-10 ms per
  action (15 clicks each way against main).
  Not handled: content inside a frame of another site or a closed shadow root, anything
  drawn on a canvas, menus that open only on hover, drag gestures, double-click and
  right-click.
- **Login details as names.** piweb (🔑) gives login details as names
  (`$PIWEB_SECRET_GITHUB_PASS`) and its Pi extension puts the values in this process's
  environment. The model types the name; only the command sent to Chrome carries the value,
  every page read back (a filled field's `value=`, a "Hi <account>" greeting, an error
  message) has the values turned into their names, and the worker's events log leaves out
  what a `type` command typed. So the planner, its engine's cache and the result Pi stores
  hold names only. A name the goal does not give is not typed. The final screenshot can
  still show an account typed into a page (password fields show dots).
- **The engine is held for the task.** Requests carry Strata's `"hold": {key, seconds,
  keep: false}`: other clients wait between the task's steps instead of each step waiting
  behind them, and no session file is written for the task. `POST /v1/hold` lets go at the
  end. The time budget (150 s) counts the task's own work, not time queued (300 s at most).

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
`upload <ref> <path>...`, `scroll down|up|top|bottom`, `back`. `--ok` on click, press,
upload or a submitting type answers OK to a confirm box the action opens. A result's
`notes` say what the action did outside the page and `files` list what it saved. Others: `get text|links`, `screenshot`,
`session-cleanup`, `crawl <urls>`, `google-search <json>`, `fetch-image <url>`,
`browser-health`, `shutdown`, `research` and `research-add-query`.

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
| `PI_AGENT_DIR` | `~/.pi/agent` | worker profile, logs, events and downloads; `browser-config.json` |
| `PI_NODRIVER_SYSTEMD` | on | `0` starts the worker as a plain child instead of its own unit |
| `RESEARCH_ACTIVE_PREFILL` | on | `0` stops warming the model with research evidence while it arrives |

`browser-config.json` in `PI_AGENT_DIR`: `{"crawlGuards": false}` exempts `crawl` from the
research guards (by default `crawl` is blocked before research unless the user gave a URL or
asked for a crawl, and after research until the user asks for more).

## History

Before 2026-11-04 the browser was a 10,600-line nodriver worker plus a separate intent
router service (Laya model, embeddings, judge, OmniParser vision fallback and more). It
was replaced by this design; see git history for the old code.
