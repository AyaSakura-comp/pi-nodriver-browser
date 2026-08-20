# Pi Nodriver Browser

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10%2B-brightgreen.svg)](https://www.python.org/)
[![Nodriver: 0.50.3](https://img.shields.io/badge/Nodriver-0.50.3-orange.svg)](https://github.com/ultrafunkamsterdam/nodriver)
[![Platform: Linux](https://img.shields.io/badge/Platform-Linux%20%2F%20Xvfb-lightgrey.svg)]()

A high-performance, persistent browser automation and parallel web crawling extension for the [Pi coding agent](https://github.com/badlogic/pi-mono), powered by **Nodriver**, headful **Chrome/Chromium**, **Xvfb**, and an integrated **Stealth & Anti-Bot Bypass Subsystem**.

Designed specifically for autonomous agent pair-programming, dynamic SPA interaction, real-time e-commerce comparison, and high-throughput research scraping on local hardware (optimized for AMD APUs & ROCm local inference).

---

## 🏛️ System & Software Architecture (SW Architecture)

`pi-nodriver-browser` employs a decoupled **Client-Daemon Multi-Session Architecture** that isolates the lightweight TypeScript agent harness from the heavyweight Python/Chromium execution engine.

```mermaid
flowchart TB
    subgraph ClientLayer["1. Pi Agent Client Layer (TypeScript)"]
        UI["User Prompt / Goal"] --> AGENT["Pi Agent (Qwen 3.6 35B / LLM)"]
        AGENT --> ROUTER["Context-Aware Intent Router"]
        ROUTER -->|"Live Web / E-Commerce"| EXT["index.ts Extension Client"]
        ROUTER -->|"Static Theory / Code"| DIRECT["Direct LLM Generation (0s Overhead)"]
        EXT --> IPC_CLIENT["Socket Client & Session Queue"]
    end

    subgraph IPCLayer["2. IPC & Process Boundary"]
        IPC_CLIENT <==>|"Unix Domain Socket (~/.pi/agent/nodriver-browser.sock)\nJSON Protocol with Streaming Markers"| DAEMON["worker.py Persistent Daemon"]
    end

    subgraph DaemonLayer["3. Python Daemon Core (worker.py)"]
        DAEMON --> SESSIONS["Session & Tab Manager\n(Strict Per-Session Isolation)"]
        DAEMON --> GUARD["Per-Session Loop Guard\n(3-Repeat Deadlock Protection)"]
        DAEMON --> BREAKER["3.0s Per-Tab Circuit Breaker\n& Anti-Bot WAF Detection"]
        DAEMON --> ENGINE["Dual-Mode Execution Engine"]
    end

    subgraph StealthLayer["4. Stealth & Anti-Bot Subsystem"]
        ENGINE --> STEALTH_EXT["stealth-extension (Chrome Manifest V3)"]
        STEALTH_EXT --> S_STEALTH["stealth.js\n- WebGL NVIDIA RTX 4070 Spoof\n- navigator.webdriver Removal\n- window.chrome Runtime Mock\n- Plugins & Permissions Injections"]
        STEALTH_EXT --> S_SOLVER["turnstile_solver.js\n- Shadow DOM Inspection\n- Cloudflare Turnstile Auto-Click\n- Human-like Bezier Pointer Events"]
    end

    subgraph BrowserLayer["5. Chromium & Display Subsystem"]
        ENGINE -->|"Interactive Mode (iPhone / 1600x1000)"| TAB_ACTIVE["Session Interactive Tab"]
        ENGINE -->|"Parallel Crawl Mode (1920x1080 Full-Desktop)"| TABS_POOL["Background Parallel Tabs 1..N\n(asyncio.gather)"]
        TAB_ACTIVE --> CHROME["Headful Google Chrome / Chromium"]
        TABS_POOL --> CHROME
        CHROME <--> XVFB["Xvfb Virtual X11 Display (:99)"]
        CHROME <--> PROFILE["Persistent Profile (~/.pi/agent/nodriver-profile)"]
    end
```

### Key Architectural Subsystems:

#### 1. Client-Daemon IPC & Session Isolation
* **Zero-Spawning Overhead**: A single persistent Python daemon (`worker.py`) runs in the background. Pi commands connect via Unix Domain Socket (`nodriver-browser.sock`), avoiding the 2–3s cold-start penalty of launching Chrome on every turn.
* **Per-Session Tab Routing**: Each Pi conversation maintains its own isolated `session_id` mapping. Session tabs, active viewports, and downloads operate independently without cross-session interference.
* **Non-Blocking Worker Queue**: Long-running page loads and crawls execute asynchronously; concurrent Pi subagents can query status without blocking.

#### 2. Stealth & Anti-Bot Subsystem (`stealth-extension`)
Integrated directly into Chrome via `--load-extension`, eliminating headless bot markers and automating challenge bypasses:
* **`stealth.js`**:
  * **WebGL Hardware Spoofing**: Overrides WebGL `UNMASKED_VENDOR_WEBGL` and `UNMASKED_RENDERER_WEBGL` from software/Mesa drivers to `Google Inc. (NVIDIA)` / `NVIDIA GeForce RTX 4070 Direct3D11`.
  * **Bot Flag Erasure**: Completely removes `navigator.webdriver` and normalizes `navigator.plugins`, `navigator.languages` (`zh-TW`, `en-US`), and `Notification.permission`.
  * **Runtime Consistency**: Injects authentic `window.chrome.runtime`, `window.chrome.csi`, and `window.chrome.loadTimes` structures.
* **`turnstile_solver.js`**:
  * **Shadow DOM Scanner**: Automatically traverses closed/open Shadow DOMs and iframe hierarchies to detect Cloudflare Turnstile, hCaptcha, and challenge checkboxes.
  * **Synthetic Human Pointer Dispatch**: Emulates natural `mousemove` → `mousedown` → `mouseup` → `click` sequences with randomized coordinate jitter.

#### 3. 3.0s Circuit Breaker & Diagnostic Feedback
* **Hard 3.0s Per-Tab Timeout**: Individual background tabs in parallel crawl jobs are wrapped with `asyncio.wait_for(fetch(), timeout=3.0)`. Slow network streams, deadlocks, or WAF stalls trip immediately without stalling the entire batch.
* **Anti-Bot WAF Challenge Detection**: Intercepts `Challenge Validation`, `Just a moment...`, and `Attention Required` pages.
* **Actionable Harness Guidance**: If a crawl job fails, structured diagnostics and fallback advice (e.g. switch to Google Finance or alternative search) are returned to the agent harness.

---

## ⚡ Accelerated Workflows (Workflow)

`pi-nodriver-browser` replaces traditional multi-turn browser loops with compressed, atomic agent workflows:

```mermaid
sequenceDiagram
    autonumber
    actor User as User (Pi / Web)
    participant LLM as Pi Agent (Qwen 3.6 35B)
    participant EXT as index.ts Client
    participant Daemon as worker.py Daemon
    participant Chrome as Chromium & DOM

    Note over User,LLM: Fast 2-Step E-Commerce Workflow
    User->>LLM: "現在 PChome 上 PS5 Slim 多少錢？"
    
    rect rgb(240, 248, 255)
    Note over LLM,Daemon: Step 1: Open with Inline Auto-Snapshot
    LLM->>EXT: browser("open https://24h.pchome.com.tw/")
    EXT->>Daemon: {command: "open ...", sessionId}
    Daemon->>Chrome: Navigate & Fast-Path Settle (80ms)
    Daemon->>Chrome: Execute SNAPSHOT_JS
    Daemon-->>EXT: Returns DOM snapshot with @refs (@e1 Search, @e2 Cart)
    EXT-->>LLM: DOM elements returned in Round 1
    end

    rect rgb(245, 255, 245)
    Note over LLM,Daemon: Step 2: Atomic Fill-Submit & Live Results Return
    LLM->>EXT: browser("fill-submit @e1 'PS5 Slim'")
    EXT->>Daemon: {command: "fill-submit @e1 'PS5 Slim'"}
    Daemon->>Chrome: Clear -> Type -> Dispatch Events -> requestSubmit()
    Daemon->>Chrome: Settle Results DOM
    Daemon-->>EXT: Returns search results DOM snapshot & price list
    EXT-->>LLM: Extracted PS5 prices in Round 2
    end

    LLM->>User: 📊 Report prices, inventory, and promotions (Completed in 2 turns!)
```

### 1. Fast 2-Step Interactive Pattern
Traditional agent browser tools take 5–6 roundtrips (`open` → `snapshot` → `fill` → `press Enter` → `wait` → `snapshot`). `pi-nodriver-browser` compresses this into **2 atomic turns**:
1. **`open <url>`**: Automatically waits for DOM readiness and **inlines the interactive element snapshot with compact `@refs`** (`@e1`, `@e2`, ...) directly into the turn-1 return payload.
2. **`fill-submit <@ref> <text>`**: Atomically clears the target input, dispatches keyboard and change events (`keydown`, `input`, `change`), executes `form.requestSubmit()` / click search, auto-settles the resulting results page, and returns the updated DOM snapshot in turn 2.

### 2. Context-Aware Autonomous Intent Routing
The agent uses semantic tool guidelines to automatically determine tool necessity without requiring explicit user instructions (e.g. "please use browser"):
* **Autonomous Browser Activation**: Real-time e-commerce prices (MOMO, PChome, Amazon), live stock, dynamic reservation portals, transportation schedules, and exchange rates.
* **Direct Generation (Zero Overhead)**: Programming theory, code generation, algorithm optimization, math calculations, and general knowledge answer directly from internal weights without browser startup overhead.
* *Evaluated across a 20-scenario benchmark with 100.0% routing accuracy (20/20).*

### 3. Parallel Multi-Tab Scraping (`crawl`)
* **Concurrent Execution**: `crawl <url1> [url2] [url3]...` launches parallel background tabs via `asyncio.gather`.
* **Desktop RWD Guarantee**: Each tab is forced to a **1920x1080 Full-Desktop Viewport** (`mobile=False`) via CDP to prevent mobile CSS from hiding tables and sidebars.
* **Fast-Path DOM Poller**: 80ms polling frequency returns page text as soon as `document.readyState` is interactive, averaging **~0.32s to 0.46s per page**.

---

## 📖 Command Reference

| Command | Syntax | Output & Behavior | Viewport Scope |
|---|---|---|---|
| **`open`** | `open <url>` | Navigates to URL and **automatically returns interactive `@refs` snapshot** | Interactive Tab (1600x1000 / iPhone) |
| **`fill-submit`** | `fill-submit <@ref> <text>` | **Atomic search**: Clears, types, submits form, auto-settles, returns results DOM | Interactive Tab |
| **`crawl`** | `crawl <url1> [url2]...` | **Parallel multi-tab crawl** with 3.0s circuit breaker and anti-bot challenge detection | 1920x1080 Full-Desktop CDP Override |
| **`snapshot -i`** | `snapshot -i` | Returns compact `@refs` for elements in current viewport | Interactive Tab |
| **`snapshot -i --full`** | `snapshot -i --full` | Returns vision-first layout overview; scroll and inspect | Interactive Tab |
| **`click`** | `click <@ref>` | Clicks snapshot element (auto-returns updated DOM snapshot) | Interactive Tab |
| **`click` (coords)** | `click <x> <y>` | Clicks viewport coordinates (fallback for canvas / shadow DOM) | Interactive Tab |
| **`fill`** | `fill <@ref> <text>` | Clears input field and types text | Interactive Tab |
| **`type`** | `type <@ref> <text>` | Types text without clearing | Interactive Tab |
| **`select`** | `select <@ref> <val>` | Selects dropdown option by value or visible label | Interactive Tab |
| **`press`** | `press <key>` | Dispatches Enter, Tab, Space, Backspace, or raw key | Interactive Tab |
| **`scroll`** | `scroll <up\|down> [px]` | Scrolls active viewport | Interactive Tab |
| **`get`** | `get text\|url\|title [@ref]` | Extracts innerText, current URL, or title | Interactive Tab |
| **`screenshot`** | `screenshot [--full]` | Captures viewport or full-page PNG/JPG screenshot | Interactive Tab |
| **`dismiss overlays`** | `dismiss overlays` | Safely dismisses cookie banners and modal overlays | Interactive Tab |
| **`close`** | `close` | Closes active session tab | Session Scope |
| **`shutdown`** | `shutdown` | Stops persistent daemon and closes Chrome | Global Daemon Scope |

---

## 🚀 Installation & Setup

### Prerequisites
* Linux (x86_64 or aarch64)
* Google Chrome or Chromium installed (`google-chrome`, `google-chrome-stable`, or `chromium`)
* `xvfb-run` and `python3` (3.10+)
* [Pi coding agent](https://github.com/badlogic/pi-mono)

### One-Step Automated Installation:
```bash
git clone git@github.com:AyaSakura-comp/pi-nodriver-browser.git
cd pi-nodriver-browser
./install.sh
```

The installer will:
1. Validate system dependencies (`python3`, `xvfb-run`, `google-chrome`).
2. Create an isolated Python venv and install dependencies (`nodriver==0.50.3`, `mss`, `websockets`).
3. Deploy extension files, worker daemon, and the **Stealth & Turnstile Subsystem** to `~/.pi/agent/extensions/nodriver-browser`.
4. Automatically disable conflicting legacy browser packages.

Then reload Pi or launch a new session:
```text
/reload
```

---

## 🧪 Testing & Verification

### Run Pure Unit Tests (74 test cases):
```bash
/home/chihmin/.pi/agent/extensions/nodriver-browser/.venv/bin/python -m unittest discover -s tests -v
```

### Run Real Headful Chrome / Xvfb Integration Tests:
```bash
RUN_BROWSER_INTEGRATION=1 xvfb-run -a python3 -m unittest discover -s tests -v
```

---

## 📄 License

MIT License. Developed with ❤️ for advanced agentic pair-programming workflows.
