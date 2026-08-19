# Pi Nodriver Browser

A global [Pi coding agent](https://github.com/badlogic/pi-mono) extension that registers a persistent `browser` tool backed by **Nodriver**, a real headful **Chrome/Chromium** instance, and **Xvfb**.

This project is intended for Linux Pi installations that need interactive browser automation—opening pages, inspecting interactive elements, clicking, typing, selecting options, scrolling, and returning screenshots to the model—without using Chrome's headless mode.

> The extension reduces obvious headless-browser fingerprints, but it does **not** guarantee bypassing CAPTCHA, bot detection, authentication, or a site's terms of service.

---

## 🎯 Architectural Overview & Dual-Mode System Design

`pi-nodriver-browser` implements a decoupled **Dual-Mode System Architecture** that separates **Interactive Browser Driving** from **High-Throughput Parallel Scraping (`crawl`)**.

```mermaid
flowchart TD
    subgraph Pi Agent Context
        U[User Request / Goal] --> LLM[Local Qwen 3.6 35B / Flagship LLM]
        LLM -->|Tool Call| EXT[index.ts Extension Client]
    end

    subgraph Daemon & IPC Layer
        EXT -->|Unix Socket IPC| SOCK[~/.pi/agent/nodriver-browser.sock]
        SOCK --> WORKER[worker.py Persistent Daemon]
        WORKER --> SESSIONS[Session ID & Active Tab Manager]
    end

    subgraph Dual-Mode Execution Engine
        SESSIONS -->|Interactive Command| INTERACTIVE[Interactive Mode: 1600x1000 Viewport]
        SESSIONS -->|Crawl Array Command| CRAWL[Parallel Crawl Mode: 1920x1080 CDP Viewport]

        INTERACTIVE -->|Click, Type, Snapshot| TAB_MAIN[Session Main Tab]
        CRAWL -->|asyncio.gather| TABS_PARALLEL[Background Parallel Tabs 1..N]

        TABS_PARALLEL -->|Fast-Path DOM Polling| DOM_READY[Adaptive 80ms Fast-Path DOM Ready]
        DOM_READY -->|innerText Extraction| TEXT_RES[Structured Markdown Multi-Page Result]
        TABS_PARALLEL -->|Auto Close| DISPOSE[Tab Disposed & Memory Freed]
    end

    subgraph Chrome & Display Layer
        TAB_MAIN --> CHROME[Headful Chrome / Chromium]
        TABS_PARALLEL --> CHROME
        CHROME --> XVFB[Xvfb Virtual X11 Display]
        CHROME <--> PROFILE[(~/.pi/agent/nodriver-profile)]
```

---

## ⚡ Core Component & Workflow Design

### 1. Dual-Mode Viewport & Mode Decoupling Architecture

The architecture enforces strict separation between interactive page driving and bulk text crawling to optimize both human-in-the-loop inspection and LLM context extraction:

* **Interactive Mode (`open`, `click`, `snapshot`, `screenshot`, `fill`)**:
  * **Default Viewport**: `1600 x 1000` (or user-configured session viewport).
  * **Mobile Emulation Support**: Supports CDP `Network.setUserAgentOverride` and `Emulation.setDeviceMetricsOverride(mobile=True)` for Mobile Web testing (e.g. PChome 24h Mobile UI).
  * **Target Use Case**: Form filling, login flows, SPA navigation, and visual verification.

* **Parallel Crawl Mode (`crawl [url1, url2, ...]`)**:
  * **Dedicated Viewport**: Forced **`1920 x 1080 Full-Desktop Viewport`** (`mobile=False`) via CDP per background tab.
  * **RWD Protection**: Guaranteeing 100% desktop Multi-Column RWD layout rendering so text, sidebars, and data tables are never hidden by mobile CSS `@media` rules.
  * **Tab Isolation**: Each target URL is spawned in an isolated temporary background tab (`about:blank`, `new_tab=True`), sets its own CDP metrics override, extracts `document.body.innerText`, and closes immediately upon completion.
  * **Zero Mutex Contamination**: Crawl background tabs operate independently without mutating or polluting the active interactive session tab or its viewport mode.

---

### 2. Fast-Path DOM-Ready Adaptive Polling Engine

Traditional browser automation waits for `window.onload` or full network idle, incurring 4–10 second delays due to external ads, trackers, and unoptimized media.

`pi-nodriver-browser` incorporates an **Adaptive Fast-Path DOM Poller**:

```python
async def wait_for_page_ready(self, page, timeout_sec=4.0, poll_interval=0.08):
    deadline = asyncio.get_running_loop().time() + timeout_sec
    while asyncio.get_running_loop().time() < deadline:
        state = await page.evaluate("document.readyState")
        if state in ("interactive", "complete"):
            has_content = await page.evaluate(
                "Boolean(document.body && (document.body.innerText.length > 0 || document.body.children.length > 0))"
            )
            if has_content:
                await asyncio.sleep(0.05)
                return
        await asyncio.sleep(poll_interval)
```

* **80ms Polling Frequency**: Checks `document.readyState` and body DOM node existence every 80ms.
* **Early Exit**: Returns as soon as text is readable, cutting single-page scrape latency to **~0.32 - 0.40 seconds**.
* **`asyncio.gather` Concurrency**: Crawls 5–15 URLs simultaneously, delivering 14.0x faster throughput than sequential scraping API pipelines.

---

### 3. Loop Guard & Safety System Design

To prevent LLM loop lock when a web element is missing or unclickable:

* **Per-Session Loop Guard**:
  * Tracks consecutive verbatim commands per Pi session.
  * Observing commands (`wait`, `snapshot`, `screenshot`, `get`, `downloads`, `download-info`) are rejected on the **3rd verbatim repeat** with a `LOOP_GUARD` error, forcing the LLM to leave the browser loop and fallback to web search.
  * State-changing commands (`click`, `fill`, `scroll`) reset the loop counter.

* **Stale Reference Auto-Recovery**:
  * Snapshot elements are assigned short, token-efficient `@e1`, `@e2` references.
  * If a DOM mutation renders a reference stale, the tool rejects the action, invalidates old refs, and atomically returns a fresh current-viewport DOM structure + viewport JPG for LLM vision inspection.

---

## 🛠️ Features

- Registers the standard Pi tool name `browser`
- Persistent Chrome/Xvfb daemon that survives Pi tasks and session shutdowns
- Headful Chrome rendered on an Xvfb virtual display
- Persistent browser profile for shared cookies/local state, with one active tab per Pi session ID
- Compact current-viewport `@e1`, `@e2`, … references generated by `snapshot -i`, including custom controls and open Shadow DOM
- Vision-first `snapshot -i --full` overview that emits no page-wide DOM dump; scroll and re-snapshot relevant viewports to interact
- Click by reference, text, CSS selector, or viewport coordinates, plus fill, type, select, keyboard, scroll, wait, text extraction, screenshots, and safe overlay dismissal
- Supports new tabs opened by clicks
- Serializes commands within each Pi session while other sessions remain responsive
- Disconnects Pi clients on session shutdown without closing Chrome
- Replaces the conflicting `npm:pi-agent-browser` package during installation

---

## 📋 Requirements & Prerequisites

| Dependency | Purpose | Required |
|---|---|---|
| Linux | Current supported platform | Yes |
| Pi coding agent | Loads `index.ts` and exposes the tool | Yes |
| Python 3.10+ | Runs the Nodriver worker | Yes |
| `venv` + `pip` | Creates the isolated Python environment | Yes |
| Nodriver 0.50.3 | Controls Chrome through the DevTools protocol | Yes; installed automatically |
| Google Chrome / Chromium | Browser engine | Yes |
| Xvfb / `xvfb-run` | Virtual X11 display for headful Chrome | Yes |

---

## 📖 Browser Command Reference

| Command | Description | Mode / Viewport Scope |
|---|---|---|
| `crawl <url1> [url2] ...` | **Parallel multi-tab crawl** across 1..N URLs | **1920x1080 Full-Desktop CDP Override** |
| `open <url>` | Navigate to a URL in the session active tab | Interactive Session Viewport (Default: 1600x1000) |
| `snapshot -i` | List compact interactive elements intersecting current viewport (`@eN`) | Interactive Session Viewport |
| `snapshot -i --full` | Return full-page visual overview; scroll and inspect | Interactive Session Viewport |
| `click <@ref>` | Click a snapshot element, including custom controls and Shadow DOM | Interactive Session Viewport |
| `click <x> <y>` | Click viewport coordinates; fallback for canvas / cross-origin iframes | Interactive Session Viewport |
| `fill <@ref> <text>` | Clear an input field and type text | Interactive Session Viewport |
| `type <@ref> <text>` | Type text without clearing | Interactive Session Viewport |
| `select <@ref> <value>` | Select a dropdown option by value or visible text | Interactive Session Viewport |
| `press <key>` | Send Enter, Tab, Space, Backspace, or text | Interactive Session Viewport |
| `scroll <direction> [px]` | Scroll up, down, left, or right | Interactive Session Viewport |
| `get text [@ref]` | Extract page or element innerText | Interactive Session Viewport |
| `dismiss overlays` | Dismiss cookie banners and modal overlays safely | Interactive Session Viewport |
| `screenshot [--full]` | Return an inline PNG/JPG screenshot | Interactive Session Viewport |
| `close` | Close only the active Pi session's tab | Interactive Session Viewport |
| `shutdown` | Stop persistent browser daemon and close Chrome | Daemon Level |

---

## 🚀 Automated Installation & Setup

```bash
git clone git@github.com:AyaSakura-comp/pi-nodriver-browser.git
cd pi-nodriver-browser
./install.sh
```

Then reload Pi or start a new session:

```text
/reload
```

---

## 🧪 Testing & Verification

Run pure unit tests:

```bash
python3 -m unittest discover -s tests -v
```

Run real Chrome/Xvfb integration tests:

```bash
RUN_BROWSER_INTEGRATION=1 NODRIVER_PYTHON="$PWD/.venv/bin/python"   python3 -m unittest discover -s tests -v
```

---

## 📄 License

MIT License
