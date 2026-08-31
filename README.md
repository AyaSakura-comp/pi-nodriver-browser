# Pi Nodriver Browser

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10%2B-brightgreen.svg)](https://www.python.org/)
[![Nodriver: 0.50.3](https://img.shields.io/badge/Nodriver-0.50.3-orange.svg)](https://github.com/ultrafunkamsterdam/nodriver)
[![Platform: Linux](https://img.shields.io/badge/Platform-Linux%20%2F%20Xvfb-lightgrey.svg)]()

A high-performance, persistent browser automation and parallel web crawling extension for the [Pi coding agent](https://github.com/badlogic/pi-mono), powered by **Nodriver**, headful **Chrome/Chromium**, **Xvfb**, and an integrated **stealth and challenge-detection subsystem**.

Designed specifically for autonomous agent pair-programming, dynamic SPA interaction, real-time e-commerce comparison, and high-throughput research scraping on local hardware (optimized for AMD APUs & ROCm local inference).

---

## 📚 Design Documents

- [Semantic Browser Actions: Technical Design, Workflow, and Architecture](docs/semantic-actions-technical-design.md) — same-origin iframe and Shadow DOM refs, searchable native dropdowns, transactional option selection, failure semantics, tests, and the CoolPC end-to-end workflow.
- [Iframe Semantic Actions Implementation Plan](docs/plans/2026-08-23-iframe-semantic-actions.md) — the test-first implementation plan completed by commit `099de1b`.
- [Google Search Engine: Technical Design, Workflow, and Architecture](docs/google-search-workflow-and-architecture.md) — multi-directional parallel Google Search, DOM extraction engine, anti-bot interception, de-duplication, and benchmark verification.

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
        DAEMON --> URL_NORM["URL Typo Normalizer\n(momoshop.tw -> momoshop.com.tw)"]
        DAEMON --> GUARD["Per-Session Loop Guards\n(Repeated Observations + Consecutive Opens)"]
        DAEMON --> TAB_LRU["Global Tab LRU\n(20-Tab Capacity + Inactive Eviction)"]
        DAEMON --> SCROLL_GUARD["SCROLL_LOOP_GUARD\n(3-Consecutive / Ping-Pong Scroll Protector)"]
        DAEMON --> BREAKER["3.0s Per-Tab Circuit Breaker\n& Anti-Bot WAF Detection"]
        DAEMON --> AUTO_DISMISS["Auto-Dismiss Overlay Engine\n(MOMO / PChome App Banner Hooks)"]
        DAEMON --> ENGINE["Dual-Mode Execution Engine"]
    end

    subgraph StealthLayer["4. Stealth & Anti-Bot Subsystem"]
        ENGINE --> STEALTH_EXT["stealth-extension (Chrome Manifest V3)"]
        STEALTH_EXT --> S_STEALTH["stealth.js\n- WebGL NVIDIA RTX 4070 Spoof\n- navigator.webdriver Removal\n- window.chrome Runtime Mock\n- Plugins & Permissions Injections"]
        STEALTH_EXT --> S_SOLVER["turnstile_solver.js\n- Shadow DOM Inspection\n- Cloudflare Turnstile Auto-Click\n- Human-like Bezier Pointer Events"]
    end

    subgraph BrowserLayer["5. Chromium & Display Subsystem"]
        ENGINE -->|"Interactive Mode (iPhone / 500x1000)"| TAB_ACTIVE["Session Interactive Tab"]
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

#### 2. Stealth & Challenge-Detection Subsystem (`stealth-extension`)
Integrated directly into Chrome via `--load-extension` to reduce common automation fingerprints and detect challenge widgets. It does not solve visual hCaptcha challenges or use third-party CAPTCHA bypass services; unresolved challenges require human completion before the agent resumes.
* **`stealth.js`**:
  * **WebGL Hardware Spoofing**: Overrides WebGL `UNMASKED_VENDOR_WEBGL` and `UNMASKED_RENDERER_WEBGL` from software/Mesa drivers to `Google Inc. (NVIDIA)` / `NVIDIA GeForce RTX 4070 Direct3D11`.
  * **Bot Flag Erasure**: Completely removes `navigator.webdriver` and normalizes `navigator.plugins`, `navigator.languages` (`zh-TW`, `en-US`), and `Notification.permission`.
  * **Runtime Consistency**: Injects authentic `window.chrome.runtime`, `window.chrome.csi`, and `window.chrome.loadTimes` structures.
* **`turnstile_solver.js`**:
  * **Shadow DOM Scanner**: Traverses available Shadow DOM and iframe contexts to detect Cloudflare Turnstile, hCaptcha, and challenge checkboxes.
  * **Checkbox Interaction**: Can dispatch pointer events to an ordinary accessible checkbox, but pauses for human handoff when a visual challenge appears.

#### 3. Auto-Dismiss Overlay Engine & Taiwan E-Commerce Hooks
* **Silent Background Execution**: Executes automatically inside `wait_for_page_ready` on every `open` navigation before generating the initial DOM snapshot.
* **Native App Banner Dismissal**: Direct programmatic hooks (e.g. `window.backBtnWeb()`) and automatic destruction of backdrop overlays (`#blackBkforApp`, `#blackBk`, `.modal-backdrop`).
* **Taiwanese Banner & Cookie Dictionary**: Matches localized dismiss phrases including 「繼續使用網頁版」、「留在網頁版」、「前往網頁版」、「繼續瀏覽」、「不用謝謝」、「我知道了」、「關閉」.
* **Small-Model Coordinate Immunity**: Eliminates the need for 7B/14B/35B models to visually estimate pixel coordinates (`click 380 30`) on blocking interstitials.

#### 4. Navigation Guard & URL Typo Normalization
* **Domain Normalization**: Automatically repairs common domain typos during agent tool calls (e.g., `momoshop.tw` ➔ `momoshop.com.tw`, `pchome.tw` ➔ `pchome.com.tw`).
* **3.0s Circuit Breaker**: Wraps background tabs in `asyncio.wait_for(fetch(), timeout=3.0)` to instantly abort hung connections or WAF blockages.

#### 5. Command Lifecycle, Concurrency, and Tab Admission
Every command follows the same ownership and capacity workflow:

```mermaid
flowchart LR
    REQUEST["Pi tool request\ncommand + sessionId"] --> DEADLINE["Validate preflight deadline\nbefore command bookkeeping"]
    DEADLINE --> VALIDATE["Parse and validate\nsupported action"]
    VALIDATE --> OPEN_GUARD{"open action?"}
    OPEN_GUARD -->|Yes| STREAK["Check per-session\n2-open streak"]
    OPEN_GUARD -->|No| EXECUTE
    STREAK --> EXECUTE["Acquire per-session lock\nand mark exact active target"]
    EXECUTE --> PREFLIGHT{"Current target preflight\nfinishes before deadline?"}
    PREFLIGHT -->|Timeout| QUARANTINE["Quarantine only the poisoned\nsession target mapping"]
    PREFLIGHT -->|Ready or non-timeout error| NEW_TAB{"Needs or discovered\na new target?"}
    QUARANTINE --> NEW_TAB
    NEW_TAB -->|Yes| CAPACITY["Acquire tab-management lock\nreconcile live Chrome targets"]
    CAPACITY --> LRU["Reserve worker-created tab or\nadmit Chrome-created popup\nwith inactive-LRU eviction"]
    LRU --> COMMAND["Execute navigation / DOM / crawl"]
    NEW_TAB -->|No| COMMAND
    COMMAND --> CLEANUP{"Temporary or evicted\ntarget to close?"}
    CLEANUP -->|Yes| CLOSE["Confirm Chrome target closure\nbefore deleting registry state"]
    CLEANUP -->|No| RESPONSE
    CLOSE --> RESPONSE["Touch activity timestamp\nrelease locks and return result"]
```

* **Layered Locking**: A per-session lock serializes commands from one conversation. The server's browser-structure lock covers its selected structural command set (`open`, click variants, `download`, `press`, `close`, and `shutdown`); `tab_management_lock` independently makes capacity checks, worker-created tabs, popup admission, eviction, and cleanup atomic—including crawl lifecycle operations.
* **Exact-Target Activity Protection**: Only the tab currently used by a running command is protected. Idle tabs from the same session remain valid LRU candidates.
* **Hung-Target Quarantine**: Before each command, the worker bounds vision/context preflight to 2 seconds by default (`PI_NODRIVER_PREFLIGHT_TIMEOUT`, positive finite seconds; invalid values fail before open/repeat/activity bookkeeping or page-less browser initialization). At the deadline it cancels the preflight task, tracks it for eventual result consumption without awaiting cancellation completion, detaches only the poisoned target from that session, releases active-target accounting, invalidates its vision state, and lets the command continue or fail normally. A live popup opener is restored when available. Every `close` remains bound to the target captured before preflight, so reconciliation cannot redirect it to a healthy opener whether preflight succeeds, reaches its deadline, or raises a task-level error. Immediate `wait-popup-close` remains idempotent after both normal and quarantined popup closure, even when an older opener remains in a nested popup stack. The poisoned target remains marked as quarantined and excluded from popup admission until Chrome reports it gone or normal LRU closes it. It also remains registered while live, preserving the global tab cap, LRU eligibility, and in-progress download ownership; preflight task errors—including a nested `asyncio.TimeoutError` distinct from worker deadline expiry—preserve the current page mapping.
* **Global Capacity Invariant**: Before creating a managed page or crawl tab, the daemon reconciles its registry with Chrome and reserves capacity. Popups are created by Chrome first, then registered and admitted under the same capacity lock; admission evicts an eligible inactive tab or closes the popup as rollback. The default maximum is 20 tabs (`PI_NODRIVER_MAX_TABS`).
* **Transactional Eviction**: Registry, session, and download-routing metadata are removed only after Chrome confirms that the target closed. A thrown close exception propagates while preserving tracked state; if close returns but Chrome still reports the target as live, the daemon raises `TAB_LIMIT`. Both paths prevent silent capacity overflow.
* **Popup Recovery**: Popup opener stacks are maintained per session. If a popup closes externally or is evicted, the newest live opener becomes the active session page.
* **Download Isolation**: Target and frame ownership are tracked separately. Closing a tab removes only that target's frame routes; active downloads protect their owning session from eviction.
* **Guard Commit Semantics**: Failed `open` attempts count toward the consecutive-open limit. A non-`open` action resets the streak only after that action validates and succeeds.

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
    Note over LLM,Daemon: Step 1: Open with Inline Auto-Dismiss & Auto-Snapshot
    LLM->>EXT: browser("open https://24h.pchome.com.tw/")
    EXT->>Daemon: {command: "open ...", sessionId}
    Daemon->>Chrome: Navigate & Fast-Path Settle (80ms)
    Daemon->>Chrome: Execute Background Auto-Dismiss (Kill App Banners)
    Daemon->>Chrome: Execute SNAPSHOT_JS
    Daemon-->>EXT: Returns clean DOM snapshot with @refs (@e1 Search, @e2 Cart)
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

### 1. Fast 2-Step Interactive Pattern (`fill-submit`)
Traditional agent browser tools take 5–6 roundtrips (`open` → `snapshot` → `fill` → `press Enter` → `wait` → `snapshot`). `pi-nodriver-browser` compresses this into **2 atomic turns**:
1. **`open <url>`**: Automatically cleans overlays, waits for DOM readiness, and **inlines the interactive element snapshot with compact `@refs`** (`@e1`, `@e2`, ...) directly into the turn-1 return payload.
2. **`fill-submit @e1 "query"`**: Atomically clears the literal target ref, dispatches cancellation-aware keyboard and change events, requires an associated form, executes `form.requestSubmit()`, auto-settles the resulting page, and returns the updated DOM snapshot in turn 2. It never guesses or clicks an unrelated fallback button.

> **Literal ref syntax:** If a snapshot prints `@e16`, send exactly `click @e16`. Never send `click <@e16>`; angle brackets in generic notation are placeholders, not characters to type. The parser accepts the older wrapped form only as a compatibility fallback.
>
> **Form safety:** `fill`, `type`, and `fill-submit` reject `<label>` refs and non-text controls instead of typing into whichever field was previously focused. Hidden native checkbox/radio controls remain actionable through their visible label proxy, and snapshots expose `control`, `checked`, `required`, and `disabled` state so optional marketing consent can be audited before submission.

#### Semantic-First Iframe Interaction

See [Semantic Browser Actions: Technical Design, Workflow, and Architecture](docs/semantic-actions-technical-design.md) for the ref lifecycle, recursive resolver, dropdown transaction protocol, security boundaries, and sequence diagrams.

`snapshot -i` recursively traverses accessible same-origin iframes and labels nested controls with `frame="…"`. `fill`, `type`, `select`, `fill-submit`, and `click-js` resolve those refs inside their owning frame instead of querying only the top document. The agent must use this priority order:

1. `snapshot -i` and an exact `@ref` (`fill`, `select`, or `click`).
2. Semantic fallback with `click-text` or `click-css`.
3. Direct DOM fallback with `click-js @ref`.
4. After **three consecutive legitimate semantic click failures on the same page**, the browser reports `VISION_FALLBACK_UNLOCKED`. Only then, for canvas or inaccessible visual-only controls, use the mandatory vision-correct sequence: `screenshot` → inspect image → `vision-mark <x> <y>` → inspect the marked image → re-mark until correct → `vision-click <preview-token>`.

Raw `click <x> <y>` is blocked, and agents must not fabricate failures merely to unlock fallback. The threshold is fixed at 3. Malformed commands, invalid selectors, stale-guard retries, infrastructure failures, and raw-coordinate attempts do not count; only explicit semantic target-resolution failures count. A successful semantic click, completed vision click, navigation, close, document reload, or different page/tab context locks fallback again.

Once unlocked, `vision-mark` interprets `x y` directly in the returned screenshot's pixel coordinate system, draws the crosshair onto a copied PNG outside the untrusted page, and converts that point to dispatch coordinates using trusted CDP visual-viewport metrics. It returns a one-time token tied to the session, active tab, loader/document, URL, scroll/visual viewport, rendered-image hash, and a short TTL. Immediately before mouse dispatch, `vision-click` brings the tab forward, captures the viewport again, and requires the trusted state and clean screenshot hash to match. A newer marker or any mismatch permanently invalidates the older token. A full-page overview (`snapshot -i --full` or `screenshot --full`) intentionally cannot arm coordinate confirmation because scaled document coordinates are not current-viewport interaction coordinates.

### 2. Multi-Spec Variant Selection & In-Page Modal Sheet Handling
E-commerce platforms (MOMO, Shopee, Amazon) often present product variations (e.g. 度數 200度~800度, 顏色, 尺寸) in dynamic bottom sheets or in-page spec drawers:
* **In-Page Spec Recognition**: Explicit instructions guide the agent to select product specifications first (`click @ref 400度` or `click @ref 請選擇商品規格`), avoiding mistaken window popup commands (`wait-popup`).
* **Instant Confirmation**: Clicks confirmation inside the spec drawer to add items to cart cleanly in 1 step.

### 3. Native CDP Multi-File & Image Upload Subsystem (`upload`)
Modern Single-Page Applications (SPAs) frequently hide raw `<input type="file">` elements behind styled `<label>`, `<button>`, or Drag-and-Drop dropzones.
* **Smart File Input Resolution**: Traverses container DOMs, labels (`for` attribute), and dropzones to locate the underlying file input.
* **CDP Native Injection**: Calls `DOM.setFileInputFiles` with local absolute paths.
* **Event Dispatch & Multi-File Support**: Automatically fires synthetic `input` and `change` events and accepts multiple paths (`upload @e1 /path/1.png /path/2.pdf`) in a single invocation.

### 4. Smart Nested Container Scroll Penetration (`scroll`)
Chat interfaces (Gemini, ChatGPT, Claude), data tables, and modern SPAs often lock the outer `window` (`overflow: hidden`) and place conversations inside nested `<div style="overflow-y: auto">` containers.
* **Smart Container Penetration**: Prioritizes `Page Window` for standard article scrolling while dynamically penetrating inner containers when window scrolling reaches physical bounds.
* **Instant Teleportation (`scroll bottom` / `scroll top`)**: Provides 1-step teleportation to the newest streamed AI response or top of page.
* **100% Physical Boundary Feedback**: Returns exact positions and boundary states (e.g. `Reached bottom of div#chat (100%), cannot scroll further down`), eliminating blind back-and-forth guessing.
* **`SCROLL_LOOP_GUARD`**: Hard 3-consecutive-scroll and ping-pong detector prevents infinite scrolling loops.

### 5. DOM Image Discovery, Parallel Delivery & Cross-Origin Rendering
* **Rendered-DOM Image Sidecar**: `get text`, `get images`, and every successful crawl extract ranked image metadata from the already-rendered DOM before returning clean text. Candidates combine ordered Open Graph image blocks, Twitter fallback, bounded Schema.org JSON-LD image traversal, `img.currentSrc` / `src` / `srcset`, common lazy-load attributes, `<figure>` captions, dimensions, visibility, and video posters. Exact URLs are deduplicated; tiny tracking pixels plus obvious logos, icons, avatars, sprites, placeholders, badges, and other utility assets are rejected. No image bytes are downloaded during discovery.
* **Explicit Discovery State**: Results distinguish `imageCandidates` / `imageCount` from downloaded attachments and report `imageDiscoveryStatus` (`ok`, `timeout`, `error`, or `not-run`). A successful inspection prints `Images found: N candidates (metadata only; not downloaded)`; timeouts and evaluation errors are never misreported as zero matches.
* **Bounded Model-Visible Sidecars**: Candidate summaries are placed before crawl text but capped at 6,000 UTF-8 bytes per page view and 12,000 UTF-8 bytes per multi-page crawl, preserving at least most of the 50 KiB head-truncated tool budget for readable page content. Full bounded candidate objects remain in structured details.
* **Source-Agnostic Image Delivery**: After `web_search`, `crawl`, or interactive browser work discovers a direct image URL, `fetch_image` downloads it without requiring or inspecting an open browser page, validates the actual bytes with Pillow, and returns an inline image attachment. It therefore bypasses page vision preflight and quarantine.
* **Parallel Multi-Image Delivery**: `fetch_images` accepts up to four unique direct URLs and dispatches their independent secure fetches with `Promise.allSettled`. A daemon-wide four-request semaphore bounds network/write concurrency, a separate four-slot decoder semaphore stays occupied until cancelled background Pillow threads actually finish, and the final marker-selected files are capped at 40 MiB total. Partial success is preserved and only generated local paths become outbox markers. Batch delivery intentionally returns text markers without reinjecting multiple image byte payloads into the next model turn, avoiding provider-specific WebP/GIF or multi-image failures; use single-image `fetch_image` when the model itself must inspect an inline candidate.
* **PiWeb / Discord Transfer**: Each result includes a session-isolated local path and the exact `[[image: <path>]]` outbox marker the agent must emit in its final reply, so the user receives images rather than inaccessible filesystem links. Saves use exclusive file creation, collision-safe names, and a bounded sanitized filename stem.
* **SSRF-Safe Default**: URL credentials and port zero are rejected. For the initial URL and every manually handled redirect, the async resolver is called once and every returned address must be safe global unicast; loopback, private, link-local, site-local, reserved/compatible/translated IPv6, metadata, multicast, unsafe IPv4-mapped/6to4/Teredo targets, and private IPv4 embedded in the well-known NAT64 prefix are blocked. The connection is then made only to one of those validated numeric addresses with `AI_NUMERICHOST`, so the original hostname is never resolved again during connect. HTTPS retains default certificate verification and uses the original hostname for SNI and certificate matching. Fetches do not use an HTTP client or opener, so `http_proxy`, `HTTP_PROXY`, `HTTPS_PROXY`, `no_proxy`, and other environment proxy settings have no effect. Redirect bodies are closed without reading and at most 3 redirects are followed. `PI_NODRIVER_ALLOW_PRIVATE_IMAGE_URLS=1` explicitly disables only the address-class block for trusted/local fixtures; do not enable it for untrusted URLs.
* **Bounded Async Fetching**: One absolute asyncio timeout (`PI_NODRIVER_IMAGE_FETCH_TIMEOUT`, 15 positive finite seconds by default) covers DNS, numeric-address connect and TLS, request drain, status line, all headers, every redirect, and the complete body. Status and individual header lines are limited to 8 KiB, response headers to 64 KiB and 100 fields, redirects to 3, and decoded transfer bytes to `PI_NODRIVER_IMAGE_MAX_BYTES` (20 MiB by default). The HTTP/1.0/1.1 parser supports validated `Content-Length`, chunked transfer coding, and connection-close bodies; it rejects conflicting or malformed framing and non-identity content encoding. Body reads use chunks of at most 64 KiB.
* **Cancellation Semantics**: Cancellation of DNS, connect/TLS, status/header parsing, or body reads propagates promptly and closes any active stream. `loop.getaddrinfo` may leave its already-running platform resolver call in the event loop executor after the await is cancelled; that residual call has no fetch side effects and is neither awaited nor allowed to connect. A cancelled Pillow decode thread may finish in the background but cannot write a file. File writes use an exclusively created path and inode-aware cleanup, receive a cancellation signal, and get a bounded cleanup wait; an old writer cannot unlink a later same-name replacement. Releasing the cancelled task also releases the daemon session lock and client queue normally.
* **Decode Limits**: Only PNG, JPEG, GIF, and WebP are accepted. Before full frame loading, decoded metadata is limited to 8192 pixels in either dimension (`PI_NODRIVER_IMAGE_MAX_WIDTH`, `PI_NODRIVER_IMAGE_MAX_HEIGHT`), 100 frames (`PI_NODRIVER_IMAGE_MAX_FRAMES`), and 40,000,000 cumulative frame pixels (`PI_NODRIVER_IMAGE_MAX_TOTAL_PIXELS`). These settings and the byte cap must be positive integers. Every accepted frame is fully loaded; PNG chunks and CRCs plus terminal JPEG/GIF/WebP container structure are checked. Every accepted format is decoded and re-encoded into a canonical PNG/JPEG/GIF/WebP container before saving, preventing Pillow-tolerated malformed or duplicate-chunk source bytes from being attached. MIME type and dimensions come from image bytes, not response headers.
* **Browser Command Parity**: `fetch-image <url>` exposes single-image delivery through the `browser` command interface. `get images` inspects the active page without repeating page text, while `get text` returns both text and the same candidate sidecar.
* **Cross-Origin Rendering**: Live external images can still be embedded via `![alt](image_url)` or `<img src="..." referrerpolicy="no-referrer" />`; the fetched-image path is preferred when the image must be delivered reliably through PiWeb or Discord.

### 6. Context-Aware Autonomous Intent Routing
The agent uses semantic tool guidelines to automatically determine tool necessity without requiring explicit user instructions (e.g. "please use browser"):
* **Autonomous Browser Activation**: Real-time e-commerce prices (MOMO, PChome, Amazon), live stock, dynamic reservation portals, transportation schedules, and exchange rates.
* **Direct Generation (Zero Overhead)**: Programming theory, code generation, algorithm optimization, math calculations, and general knowledge answer directly from internal weights without browser startup overhead.
* *Evaluated across a 20-scenario benchmark with 100.0% routing accuracy (20/20).*

### 7. Per-Session Open Loop Guard
To prevent a runaway agent from repeatedly opening the same site, each session may attempt at most **2 consecutive `open` actions to the same origin**. The 3rd same-origin `open` returns `OPEN_LOOP_GUARD` without launching a tab. A valid non-`open` browser action or a different-origin `open` resets the streak, while unsupported commands do not; for multiple same-site URLs, prefer one batched `crawl` call.

### 8. Global Tab LRU
Chrome is capped at **20 tabs globally** by default (`PI_NODRIVER_MAX_TABS`). Each tab stores an immutable creation time and a `time.monotonic()` last-activity timestamp. Every page operation refreshes activity; when a new tab needs capacity, the least-recently-used inactive tab is closed first. Registry and download-routing state is removed only after Chrome confirms closure, preventing failed closes from bypassing the cap or leaking stale frame ownership. A CDP target quarantined after a preflight timeout remains in the registry until Chrome confirms it has disappeared or normal LRU eviction closes it. Tabs belonging to commands currently running and sessions with in-progress downloads are protected. If every tab is protected, creation fails with `TAB_LIMIT` instead of exceeding the cap. Crawl creation uses the same registry and a bounded semaphore.

### 9. Parallel Multi-Tab Scraping (`crawl`)
* **Concurrent Execution**: `crawl <url1> [url2] [url3]...` launches parallel background tabs via `asyncio.gather`.
* **Text + Image Sidecar**: Each tab returns clean `document.body.innerText` plus bounded, ranked `imageCandidates`; the candidate evaluation reuses the rendered page and does not issue image downloads.
* **Desktop RWD Guarantee**: Each tab is forced to a **1920x1080 Full-Desktop Viewport** (`mobile=False`) via CDP to prevent mobile CSS from hiding tables and sidebars.
* **Fast-Path DOM Poller**: 80ms polling frequency returns page text as soon as `document.readyState` is interactive, averaging **~0.32s to 0.46s per page**.

### 10. Multi-Directional Parallel Google Search (`google_search`)
* **Multi-Directional Queries**: Dispatches up to **4 directional queries in parallel** (e.g. official docs, troubleshooting, benchmark comparisons) in a single turn via `google-search <json>`.
* **Zero External API Cost & Ultra-Low Latency**: Directly leverages persistent Chromium inside Xvfb with hardware stealth, achieving **~0.82s median latency**.
* **Clean DOM Card Extraction**: Evaluates `GOOGLE_RESULTS_JS` directly on the rendered Google SERP to extract un-redirected URLs, `h3` titles, and clean snippets (`[data-sncf="1"], .VwiC3b`).
* **Stealth & Anti-Bot Protection**: Backed by `stealth.js` (WebGL RTX 4070 spoofing, bot flag removal) with automated interception of `unusual traffic` / `verify you are human` challenges.
* **Balanced Diversity Re-ranking**: Uses `select_diverse_search_results` to interleave multi-direction results and deliver a balanced Top 10 to the agent context.

---

## 🎬 Real-World Autonomous Verification Case Studies

### 🛒 Case Study 1: Autonomous PChome 24h Cart Addition (Qwen 3.6 35B)
* **Goal**: Autonomously search for toothpaste on PChome 24h, select a product, add it to the cart, and visually verify cart status.
* **Model**: Local `local-llama/qwen3.6-35b-q4` running on AMD APU ROCm.
* **Execution Trace**:
  1. `browser("open https://24h.pchome.com.tw/")` ➔ Opened homepage with instant `@refs`.
  2. `browser("fill-submit @e6 牙膏")` ➔ 1-step atomic search form submission.
  3. `browser("click @e52")` ➔ Navigated to DARLIE 好來 雙重功效牙膏 (2+1 超值組).
  4. `browser("click @e60")` ➔ Clicked "加入購物車" (Add to Cart).
  5. `browser("click @e9")` ➔ Navigated to Cart Page (`https://ecssl.pchome.com.tw/fsrwd/cart`).
  6. `browser("screenshot")` ➔ Captured verified proof of DARLIE 牙膏 ($164, Qty: 1) in cart.
* **Total Execution Time**: **75.84s** (100% autonomous with 0 scroll loops).

---

### 👓 Case Study 2: MOMO 購物網 Prescription Goggles Spec Flow (Qwen 3.6 35B)
* **Goal**: Navigate to MOMO prescription swimming goggles (Product 8524087), select "400度" specification, click "加入購物車", and report status.
* **Execution Trace**:
  1. `browser("open https://www.momoshop.com.tw/product/8524087")` ➔ Auto-normalized domain and auto-dismissed floating backdrops.
  2. `browser("click @e20")` ➔ Expanded 「請選擇商品規格」 bottom sheet drawer.
  3. `browser("click @e39")` ➔ Selected 「400度」 directly in 1 turn (0 scroll loops).
  4. `browser("click @e35")` ➔ Clicked "加入購物車".
  5. MOMO server triggered 302 redirect to `/mymomo/login.momo` (mandatory member login policy).
  6. Agent accurately identified and reported the guest login requirement without getting stuck in popup timeouts.
* **Total Execution Time**: **95.56s** (Clean execution, down from 260s+ infinite hanging).

---

### 🏊 Case Study 3: PChome 24h Degree Goggles Full End-to-End Cart Verification
* **Goal**: Search for degree swimming goggles on PChome 24h, select "黑-200度", add to cart, and verify total cart contents.
* **Execution Trace**:
  1. `browser("open https://24h.pchome.com.tw/")` ➔ Opened store.
  2. `browser("fill-submit @e6 度數泳鏡")` ➔ Searched and selected TRANSTAR 度數泳鏡 ($490).
  3. `browser("click @e58")` ➔ Selected spec option `黑-200度`.
  4. `browser("click @e62")` ➔ Clicked "加入購物車" (Guest cart supported).
  5. `browser("click @e9")` ➔ Inspected Cart Page and verified 3 items accumulated ($45,090 total).
* **Total Execution Time**: **272.50s** (100% autonomous completion).

---

### 🎨 Case Study 4: Multi-Modal AI Image Generation & Auto-Upload
* **Goal**: Autonomously generate an anime illustration using local diffusion and upload it to Postimages via browser automation.
* **Execution Trace**:
  1. Local ROCm Anime Diffusion skill generated an 1184×1776 PNG (`/tmp/anime_sample.png`).
  2. `browser("open https://postimages.org/")` ➔ Located upload dropzone.
  3. `browser("upload @e2 /tmp/anime_sample.png")` ➔ Injected 2.04 MB image via CDP.
  4. Extracted public CDN direct link: `https://i.postimg.cc/FRXknHvy/anime-sample.png` (`HTTP 200 OK`).
* **Total Execution Time**: **42.1s**.

---

### 📸 Case Study 5: PChome 24h Tamron Lens Warranty Terms Zero-Scroll Extraction
* **Goal**: Retrieve parallel import (平輸) warranty terms for Tamron 28-200mm lens on PChome 24h without getting trapped in image thumbnail scroll loops.
* **Execution Trace**:
  1. `browser("open https://24h.pchome.com.tw/prod/DGBH50-A900BD5P0")` ➔ Opened product page.
  2. Main page prioritized for reading; extracted complete warranty terms (1-year store warranty / 一年店家保固).
* **Total Execution Time**: **115.15s** (0 scroll loops).

---

## 📊 Benchmark & Performance Evaluation Dashboard

> 🔗 **Interactive Dashboard & Benchmark Repository:** [pi-agent-benchmark-dashboard](https://github.com/AyaSakura-comp/pi-agent-benchmark-dashboard)  
> 📑 **Gist Permanent Evaluation Record:** [Gist debe1b74b89fe86e3fed726d3e81055c](https://gist.github.com/AyaSakura-comp/debe1b74b89fe86e3fed726d3e81055c)

This benchmark rigorously evaluates **Pi Agent with `pi-nodriver-browser`** against **Google AI Mode (Live Search Browser)** and **Firecrawl API (Sequential Scrape)** across 10 in-depth domain research scenarios and 16 real-world agentic interaction tasks.

---

### 🏆 Visual Performance & Latency Histograms

#### 1. Pure Scraping Latency per Web Page (Seconds, Lower is Better)
```text
pi-nodriver-browser  [██] 0.32s  (⚡ 14.0x Faster than Firecrawl API, 92.8% Time Saved)
Firecrawl API        [████████████████████████████] 4.50s
```

```mermaid
gantt
    title Average Scraping Time per Page (Seconds)
    dateFormat X
    axisFormat %s sec

    section Firecrawl API (Legacy)
    4.50 seconds per page : 0, 45

    section Nodriver Browser (Parallel)
    0.32 seconds per page (14.0x Faster) : 0, 3
```

#### 2. Cumulative Pipeline Execution Time across 10 Complex Research Tasks (Lower is Better)
```text
pi-nodriver-browser  [████████████████████] 15.14 mins (908.7s - ⏱️ 2.01x E2E Speedup)
Firecrawl API        [████████████████████████████████████████] 30.37 mins (1,822.2s)
```

```mermaid
gantt
    title 10 Scenarios Cumulative Pipeline Execution Time (Seconds)
    dateFormat X
    axisFormat %s sec

    section Firecrawl (Sequential)
    30.37 mins (1822s total) : 0, 1822

    section Nodriver Browser (Parallel)
    15.14 mins (908s total - 2.01x Speedup) : 0, 908
```

#### 3. Overall 5-Star Quality Score Comparison (Out of 5.0 Stars ⭐)

| Pipeline Engine | Star Rating Visual Bar | Quality Score | Ranking |
| :--- | :--- | :---: | :---: |
| **🥇 Nodriver Browser (Parallel)** | `█████████████████████████████████████████████████▉` | **4.80 / 5.0** ⭐ | **1st (Overall Winner)** |
| **🥈 Google AI Mode (Live Browser)** | `█████████████████████████████████████████████▋` | **4.55 / 5.0** ⭐ | **2nd** |
| **🥉 Firecrawl API (Sequential)** | `█████████████████████████████████████████████▍` | **4.53 / 5.0** ⭐ | **3rd** |

---

### ⚡ Performance Improvements & Speedup Metrics

| Performance Metric | Firecrawl API (Legacy) | Nodriver-Browser (Current) | Net Improvement |
| :--- | :---: | :---: | :---: |
| **Pure Scraping Latency (Per Page)** | **~4.50 seconds / page** | **~0.32 seconds / page** | **⚡ 14.0x Faster (92.8% Time Saved)** |
| **10 Scenarios Cumulative Scrape Time** | **261.0 seconds** | **18.7 seconds** | **⚡ 242.3 seconds Saved per 10 runs** |
| **10 Scenarios E2E Total Pipeline Time** | **1,822.2 seconds (30.37 mins)** | **908.7 seconds (15.14 mins)** | **⏱️ 2.01x E2E Speedup (Saved 15.23 mins)** |
| **Multi-URL Array Parallel Capacity** | Single-URL Only (`{"url": "..."}`) | **15+ URLs Concurrent Batch** | **🚀 100% Native Parallel Batching** |
| **Anti-Bot & Paywall Bypass Rate** | 85.0% (Blockage on Medium/Substack) | **100% (Headful Chromium + Stealth)** | **🛡️ +15% Reliability Boost** |
| **Operational Cost & Rate Limits** | API Quotas / HTTP 429 Risks | **$0 / Completely Local** | **💰 100% Free & Zero Rate Limits** |

---

### ⭐ 5-Star Rating Breakdown Across 10 Research Scenarios (Out of 5.0 Stars)

| # | Scenario Domain | Nodriver-Browser (Local Parallel) | Google AI Mode (Live Browser) | Firecrawl API (Sequential Scrape) | 🏆 Scenario Winner & Highlights |
| :-: | :--- | :-: | :-: | :-: | :--- |
| **1** | **Semiconductor CoWoS Packaging** | ⭐⭐⭐⭐⭐ **4.85** | ⭐⭐⭐⭐🌗 **4.70** | ⭐⭐⭐⭐🌗 **4.70** | 🏆 **Nodriver** (Full 2022-2026 capacity breakdown: 10k ➔ 135k wpm) |
| **2** | **Python 3.13 JIT Benchmarks** | ⭐⭐⭐⭐⭐ **4.85** | ⭐⭐⭐⭐ **4.40** | ⭐⭐⭐⭐ **4.40** | 🏆 **Nodriver** (Full code & benchmark tables without paywall stops) |
| **3** | **Kyoto Travel & Michelin Guide** | ⭐⭐⭐⭐🌗 **4.80** | ⭐⭐⭐⭐⭐ **4.85** | ⭐⭐⭐⭐🌗 **4.60** | 🏆 **Google AI Mode** (Superior Google Maps indexing) |
| **4** | **AI GPU Market Share (Nvidia/AMD)**| ⭐⭐⭐⭐🌗 **4.80** | ⭐⭐⭐⭐🌗 **4.80** | ⭐⭐⭐⭐🌗 **4.75** | 🤝 **Tie** (Exact SEC financial figures) |
| **5** | **Tesla FSD v13 Review** | ⭐⭐⭐⭐🌗 **4.75** | ⭐⭐⭐⭐🌗 **4.60** | ⭐⭐⭐⭐🌗 **4.55** | 🏆 **Nodriver** (HW3/AI4 hardware architecture details) |
| **6** | **React 19 & Next.js 15 Migration**| ⭐⭐⭐⭐🌗 **4.80** | ⭐⭐⭐⭐ **4.25** | ⭐⭐⭐⭐🌗 **4.65** | 🏆 **Nodriver** (Full code samples from official docs) |
| **7** | **LLM Architecture (DeepSeek/Claude)**| ⭐⭐⭐⭐🌗 **4.80** | ⭐⭐⭐⭐🌗 **4.60** | ⭐⭐⭐⭐🌗 **4.60** | 🏆 **Nodriver** (Fetched 12 research papers concurrently) |
| **8** | **Taiwan 5G Carrier Tariffs** | ⭐⭐⭐⭐🌗 **4.75** | ⭐⭐⭐⭐⭐ **4.80** | ⭐⭐⭐⭐🌗 **4.65** | 🏆 **Google AI Mode** (Local forum & NP discount indexing) |
| **9** | **Nintendo Switch 2 Launch** | ⭐⭐⭐⭐🌗 **4.70** | ⭐⭐⭐⭐⭐ **4.80** | ⭐⭐⭐⭐ **4.50** | 🏆 **Google AI Mode** (Spot-on release date & games) |
| **10**| **GLP-1 Weight Loss Clinical Studies**| ⭐⭐⭐⭐⭐ **4.85** | ⭐⭐⭐⭐🌗 **4.60** | ⭐⭐⭐⭐🌗 **4.70** | 🏆 **Nodriver** (Fetched 13 medical papers with full clinical trial data) |
| **Σ** | **Overall 5-Star Average Rating** | ⭐⭐⭐⭐⭐ **`4.80 / 5.0`** 👑 | ⭐⭐⭐⭐🌗 **`4.55 / 5.0`** 🥈 | ⭐⭐⭐⭐🌗 **`4.53 / 5.0`** 🥉 | **Nodriver Browser Wins Overall Quality & Depth!** |

---

### 🔬 Detailed 16 Real-World Interaction Use Case Benchmark Matrix

| # | Use Case & Task Scenario | `pi-nodriver-browser` | `Firecrawl API` | `Gemini / Cloud Browser` | Key Architectural Advantage |
|---|---|---|---|---|---|
| 1 | **PChome 24h Cart Addition** (Search '牙膏' -> Add to Cart -> Verify) | **75.8s (100% Success)** | ❌ Unsupported (Read-only) | ⚠️ 145.2s (Slow click loops) | Atomic `fill-submit` & Smart Cart Resolution |
| 2 | **Cloudflare Turnstile Protected Site** (Bypass & Extract Data) | **0.82s (100% Success)** | ⚠️ 8.90s (50% block rate) | ❌ Stalled on Cloudflare Challenge | Integrated `stealth-extension` + WebGL Spoofing |
| 3 | **Postimages Direct Image Upload** (Local PNG -> CDN Link) | **1.85s (100% Success)** | ❌ Unsupported (No local upload) | ❌ Unsupported | Native CDP `DOM.setFileInputFiles` Injection |
| 4 | **Multi-File Batch Attachment** (Upload 2 PDFs simultaneously) | **1.20s (100% Success)** | ❌ Unsupported | ❌ Unsupported | Batch multi-path file input resolver |
| 5 | **Gemini / Chat SPA Nested Scroll** (Scroll fixed overflow-y container) | **0.42s (100% Success)** | ❌ Truncated content | ⚠️ Stalled (Window scroll deadlocks) | Smart Nested Container Penetration + 100% Boundary |
| 6 | **Parallel 5-URL Scraping** (PChome, MOMO, Yahoo, Shopee, Amazon) | **0.48s Total (Concurrent)**| 4.60s Total | 18.5s Total (Sequential tabs) | `asyncio.gather` with 1920x1080 Desktop Viewport |
| 7 | **Taiwan Stock Real-time Quote** (TWSE / Yahoo Finance live price) | **0.35s (100% Success)** | 3.20s | 9.80s | 80ms fast-path DOM settling |
| 8 | **MOMO Shopping Price Extraction** (Extract dynamic discount price) | **0.44s (100% Success)** | ⚠️ 6.10s (Anti-bot rate limit) | 8.20s | Headful browser profile with persistent cookies |
| 9 | **OAuth Popup Flow** (Open popup -> Switch -> Close -> Resume) | **1.10s (100% Success)** | ❌ Unsupported | ⚠️ 24.0s (Popup tracking lost) | Automatic opener tracking & popup lifecycle hooks |
| 10 | **Cookie Banner & Promo Dismissal** (Dismiss overlays automatically) | **0.18s (100% Success)** | ⚠️ Overlays pollute Markdown | ⚠️ 12.0s (Manual click turns) | `dismiss overlays` heuristics |
| 11 | **Infinite Scroll Long Article** (Load lazy images and deep text) | **0.65s (100% Success)** | ⚠️ Truncated to first viewport | 14.2s (Repeated manual scrolls) | `scroll bottom` instant container teleportation |
| 12 | **PDF File Direct Download & Text Read** (Trigger download -> Extract) | **0.90s (100% Success)** | ⚠️ Raw binary URL | ❌ Download prompt block | CDP `DownloadWillBegin` + local `pdftotext` |
| 13 | **Dropdown Selection & Filtering** (Select region / product spec) | **0.25s (100% Success)** | ❌ Unsupported | 7.50s | Synthetic `change` + `input` event dispatch |
| 14 | **Anti-Bot Fingerprint Scanner** (BrowserScan / Incolumitas Test) | **100/100 (Pass)** | 62/100 (Headless flags) | 70/100 (Datacenter IP flagged) | RTX 4070 WebGL Spoof & `navigator.webdriver` removal |
| 15 | **Dense Technical Article Crawl** (Wikipedia / Arxiv markdown) | **0.32s (100% Success)** | 2.80s | 6.40s | Direct `innerText` high-density token extraction |
| 16 | **High Speed Rail Ticket Search** (Form fill with dates -> View seats)| **1.40s (100% Success)** | ❌ Unsupported (Dynamic form) | ⚠️ 32.0s (Timeout on calendar) | Atomic input typing & fast keyboard event dispatch |

---

## Searchable Dropdown Workflow

Large native dropdowns use progressive disclosure instead of dumping every `<option>` into the model context. `snapshot -i` reports the control's accessible/structural label, selected value, option count, and whether its options are numeric or textual. Labels are derived generically from `aria-label`, associated `<label>`, fieldset legends, table-row context, groups, and nearby siblings—including same-origin iframe controls.

```text
@e43 <select> label="Processor / CPU" selected="Choose a processor" options="48 text"
@e44 <select> label="Processor / CPU" selected="1" options="10 numeric"
Dropdown options are searchable without opening them: find-option "keywords", then copy the returned complete Select exactly command.
```

Search all dropdown options without opening them or crawling the full page:

```text
find-option "32GB 6000 CL30"
```

The worker normalizes Unicode, spacing, punctuation, casing, token order, and letter/number boundaries (`RTX5070` matches `RTX 5070`). Control labels participate in ranking, numeric model prefixes must remain adjacent (`RX 7800` does not match a price or a CPU `7800` elsewhere), and the top results are diversified across dropdowns so one category cannot crowd out all alternatives. If a requested model is unavailable, `find-option` performs one safe alpha-family relaxation and labels the results as alternatives instead of forcing the model into repeated guesses. A clear winner can be selected by fuzzy query. Similar variants produce `AMBIGUOUS_OPTION` rather than silently choosing the first option; use the returned stable index immediately:

```text
select @e43 "32GB 6000 CL30"
select @e43 --index=31 --fingerprint=8e2c1a7d29f2b612
```

Visible text outranks an unrelated exact `value`, numeric/model tokens require token-boundary matches, disabled controls/options (including inherited `<fieldset disabled>`) are excluded, and selection dispatches normal `input` and `change` events. Exact index commands include a text/value fingerprint; the final text/value verification and `selectedIndex` mutation occur atomically in the same frame evaluation, so a reordered or replaced option returns `STALE_OPTION` without changing the control. Hidden or frame-offscreen iframe and Shadow DOM branches are not traversed. This protocol is site-independent and works in the top document, visible open Shadow DOM, and visible same-origin iframes.

---

## 📖 Command Reference

| Command | Syntax | Output & Behavior | Viewport Scope |
|---|---|---|---|
| **`open`** | `open <url>` | Navigates to URL, **auto-dismisses blocking banners**, and **automatically returns interactive `@refs` snapshot**. Per session, the 3rd consecutive same-origin open is blocked; a different-origin open resets the streak. | Interactive Tab (500x1000 / iPhone) |
| **`fill-submit`** | `fill-submit @e1 "query"` | **Atomic search**: Clears, types, submits form, auto-settles, returns results DOM | Interactive Tab |
| **`upload`** | `upload @e1 <file1> [file2]...` | **Atomic file upload**: Injects local files via CDP into the literal file input, button, or dropzone ref | Interactive Tab |
| **`fetch-image` / `fetch_image`** | `fetch-image <http(s)://image-url>` | Fetches and validates one direct image URL, saves it in the session-isolated download directory, and returns an inline image plus a `[[image: <path>]]` delivery marker. | Session Scope |
| **`fetch_images`** | `fetch_images({ urls: [...] })` | Fetches up to four selected direct images concurrently, preserves partial success, and returns exact delivery markers without reinjecting image bytes into the next model turn. | Session Scope |
| **`crawl`** | `crawl <url1> [url2]...` | **Parallel multi-tab crawl** returning clean text plus ranked `imageCandidates`, with 3.0s circuit breaker and anti-bot challenge detection. | 1920x1080 Full-Desktop CDP Override |
| **`snapshot -i`** | `snapshot -i` | Returns compact `@refs` plus checkbox/radio `checked`, `required`, and `disabled` state in the current viewport | Interactive Tab |
| **`snapshot -i --full`** | `snapshot -i --full` | Returns vision-first layout overview; scroll and inspect | Interactive Tab |
| **`click`** | `click @e16` | Clicks the literal snapshot ref; raw coordinate form is blocked | Interactive Tab |
| **`long-press`** | `long-press @e16 [duration_ms]` | Long presses the literal snapshot ref for `duration_ms` (default `1000`ms, via Xvfb `mousedown` ➔ hold ➔ `mouseup`, `isTrusted: true`) | Interactive Tab |
| **`vision-mark`** | `vision-mark <x> <y>` | Draws a crosshair at screenshot-pixel coordinates on a copied current-viewport PNG without clicking; returns a one-time preview token | Interactive Tab |
| **`vision-click`** | `vision-click [preview-token]` | Consumes the visually confirmed marker token and clicks its stored viewport point (hardware click via Xvfb `xdotool`, `isTrusted: true`) | Interactive Tab |
| **`vision-long-press`** | `vision-long-press [preview-token] [duration_ms]` | Consumes the visually confirmed marker token and long presses at its stored viewport point for `duration_ms` (default `1000`ms, `isTrusted: true`) | Interactive Tab |
| **`vision-mark-drag`** | `vision-mark-drag <start_x> <start_y> <end_x> <end_y>` | Draws a visual drag trajectory (Green start circle ➔ Blue arrow ➔ Red end target) on screenshot for inspection and calibration without executing drag | Interactive Tab |
| **`vision-drag`** | `vision-drag [preview-token] [duration_ms]` | Executes smooth hardware drag on Xvfb along the visually confirmed trajectory (via `xdotool` interpolation, `isTrusted: true`) | Interactive Tab |
| **`fill`** | `fill @e6 "text"` | Clears and types only into a text-editable input/textarea/contenteditable ref; `<label>` refs fail closed | Interactive Tab |
| **`type`** | `type @e6 "text"` | Types into the literal input ref without clearing | Interactive Tab |
| **`find-option`** | `find-option <keywords>` | Searches every native dropdown internally with Unicode-normalized fuzzy token ranking, returning only the top labelled `@ref`/option-index candidates | Interactive Tab |
| **`select`** | `select @e43 <query\|--index=N --fingerprint=HASH>` | Selects from the literal dropdown ref; ambiguous queries return candidates instead of guessing, and the complete indexed command from `find-option` verifies the option has not changed | Interactive Tab |
| **`press`** | `press <key>` | Dispatches only control keys such as Enter, Tab, Space, or Backspace. Enter field text with `fill @e6 "text"` or `type @e6 "text"`. | Interactive Tab |
| **`scroll`** | `scroll <down|up|top|bottom|left|right> [px]` | **Smart container scroll**: Penetrates nested chat/table containers with 100% boundary feedback | Interactive Tab |
| **`get`** | `get text|images|url|title [@ref]` | `get text` returns innerText plus ranked image candidates; `get images` returns only candidate metadata; URL/title behavior is unchanged. | Interactive Tab |
| **`screenshot`** | `screenshot [--full]` | **Default**: Captures current Xvfb window (`500x1000` with Chrome UI, 1:1 coordinates for visual check & `vision-mark`).<br>**`--full`**: Captures entire scrollable long page via CDP specifically to assist non-vision DOM browser clicks (`@ref`, `click-text`). | Interactive Tab |
| **`dismiss overlays`** | `dismiss overlays` | Safely dismisses cookie banners and modal overlays | Interactive Tab |
| **`close`** | `close` | Closes active session tab | Session Scope |
| **`shutdown`** | `shutdown` | Stops persistent daemon and closes Chrome | Global Daemon Scope |

---

### 📸 Screenshot & Interaction Guide

- **預設截圖 (`screenshot`)**：
  - **適用情境**：使用者要求截圖、檢視當前可視範圍、檢查表單狀態、或進行 `vision-mark` 座標校準。
  - **運作機制**：直接從 Xvfb 擷取 `500x1000` 實體視窗畫面（包含 Chrome 分頁標籤、網址列與 1:1 實體像素座標）。
- **整頁長截圖 (`screenshot --full` / `snapshot -i --full`)**：
  - **適用情境**：**輔助 DOM 語意點擊 (Non-Vision Browser Click)**。當頁面很長且 Agent 需要一眼掌握全頁排版、尋找特定按鈕或標題以決定呼叫哪一個 `@ref` / `fill` / `click-text` 時使用。
  - **運作機制**：透過 Chrome Blink CDP 引擎在記憶體中拼接長圖，不提供 X11 物理座標（不能用於座標點擊）。

---

### Environment Variables & Display Configuration

| Variable | Default | Description |
|---|---|---|
| `PI_NODRIVER_SCREEN` | `500x1000x24` | Xvfb virtual display resolution (compact default fits Chrome UI + iPhone viewport without clipping). |
| `PI_NODRIVER_WINDOW_SIZE` | `500,1000` | Chrome startup `--window-size` in Xvfb (with `--start-maximized` and `--window-position=0,0`). |
| `PI_NODRIVER_XVFB_FORWARD_CLICK` | `1` | Enabled by default (`1`). Uses X11 native hardware mouse click forwarding (via `xdotool` on Xvfb, `isTrusted: true`) and Xvfb full-screen capture for `screenshot` and `vision-mark` (1:1 coordinate alignment). Set `0` to force CDP fallback. |
| `PI_NODRIVER_TOOLBAR_HEIGHT` | `76` | Chrome top toolbar height offset in pixels for X11 screen coordinates calculation. |
| `PI_NODRIVER_DEFAULT_LONG_PRESS_MS` | `1000` | Default duration for `long-press` and `vision-long-press` if omitted (e.g. `2s`, `1500ms`, `2.5`). |
| `PI_NODRIVER_FORCE_LONG_PRESS_MS` | (unset) | Globally force ALL `long-press` actions to a specific duration (e.g. `2s`, `3000ms`, `1.5`), overriding any command-line parameters. |
| `PI_NODRIVER_LONG_PRESS_JITTER` | `1` | Enabled by default (`1`). Adds subtle $\pm 2$px human-like micro-drift / pressure wobble during long press to emulate natural human touch/pointer kinematics. |
| `PI_NODRIVER_LONG_PRESS_JITTER_PX` | `2.0` | Maximum radius (in pixels) for human micro-jitter drift during long-press holding. |
| `PI_NODRIVER_ALLOW_PRIVATE_IMAGE_URLS` | `0` | Set `1` to allow fetching private/local IP images in test fixtures. |
| `PI_NODRIVER_CHROME` | (auto-detect) | Custom path to Chrome/Chromium executable. |
| `PI_NODRIVER_SOCKET` | `~/.pi/agent/nodriver-browser.sock` | Unix domain socket path for daemon IPC. |

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
2. Create an isolated Python venv and install dependencies (`nodriver==0.50.3`, `Pillow==12.3.0`, `idna==3.10`).
3. Deploy extension files, worker daemon, and the **Stealth & Turnstile Subsystem** to `~/.pi/agent/extensions/nodriver-browser`.
4. Automatically disable conflicting legacy browser packages.

Then reload Pi or launch a new session:
```text
/reload
```

---

## 🧪 Testing & Verification

The test strategy is layered so fast state-machine checks run on every change, while real Chrome tests remain available for lifecycle behavior that mocks cannot prove.

### Test Layers

| Layer | Main files | What it verifies | Default behavior |
|---|---|---|---|
| Pure logic | `tests/test_browser_logic.py`, `tests/test_popup_logic.py` | Parsing, snapshots, repeated-command guards, open streaks, LRU ordering, protected targets, download isolation | Always runs |
| Worker state machine | `tests/test_worker_integration.py` unit cases | 30-tab LRU simulation, failed-close rollback, stale-target reconciliation, hung-preflight isolation, durable popup quarantine, target-bound close races, nested popup opener recovery, active-target cleanup, download-route preservation, crawl slot reservation, frame-route cleanup | Always runs with fake tabs/browser |
| Installer | `tests/test_install.py` | Extension deployment and conflicting-package cleanup | Always runs in a temporary directory |
| Real browser | `tests/test_worker_integration.py`, `tests/test_daemon_integration.py` | Headful Chrome navigation, popups, downloads, multi-session isolation, cancellation, daemon persistence | Opt-in with `RUN_BROWSER_INTEGRATION=1` |
| Agent E2E | Manual release gate | Pi/Qwen tool routing, third-open rejection, real 30-tab LRU behavior, recently touched tab survival, eight-part CoolPC selection, same-origin iframe report generation | Run before deployment of lifecycle or semantic-action changes |

### Fast Suite

Use the extension's isolated Python environment so the Nodriver version matches production:

```bash
PYTHON="$HOME/.pi/agent/extensions/nodriver-browser/.venv/bin/python"
"$PYTHON" -m unittest discover -s tests -v
```

The current suite contains **237 tests**: 180 fast tests run by default and 57 real-browser tests are skipped unless explicitly enabled.

### Real Headful Chrome / Xvfb Suite

The integration fixtures launch workers under Xvfb themselves:

```bash
PYTHON="$HOME/.pi/agent/extensions/nodriver-browser/.venv/bin/python"
RUN_BROWSER_INTEGRATION=1 \
NODRIVER_PYTHON="$PYTHON" \
"$PYTHON" -m unittest discover -s tests -v
```

For a quicker lifecycle smoke test:

```bash
PYTHON="$HOME/.pi/agent/extensions/nodriver-browser/.venv/bin/python"
RUN_BROWSER_INTEGRATION=1 NODRIVER_PYTHON="$PYTHON" \
"$PYTHON" -m unittest \
  tests.test_worker_integration.WorkerIntegrationTests.test_opens_snapshots_clicks_and_reads_page \
  tests.test_worker_integration.WorkerIntegrationTests.test_popup_close_automatically_returns_to_its_opener -v
```

### Tab-Limit Release Scenario

Lifecycle changes should also pass this real-browser acceptance scenario:

1. Open 20 tabs across separate sessions (or run a successful non-`open` command between opens) so the per-session open guard is not the limiting factor; touch two older tabs to refresh their activity timestamps.
2. Open 10 additional tabs under the same guard-safe pattern.
3. Assert that Chrome never exceeds 20 tabs.
4. Assert that the two touched tabs survive and the oldest untouched inactive tabs are evicted.
5. Repeat with an active command and an in-progress download; the active target and every registered tab in the download-owning session must remain protected.
6. Simulate a failed close; the tab must remain registered and new-tab admission must fail instead of exceeding capacity.

### Pre-Commit Gates

Before committing or deploying:

```bash
PYTHON="$HOME/.pi/agent/extensions/nodriver-browser/.venv/bin/python"
"$PYTHON" -m py_compile browser_logic.py worker.py
"$PYTHON" -m unittest discover -s tests -q
git diff --check
```

Review the diff for credentials and unsafe process/shell changes, then run an independent logic review of command guards, tab ownership, popup rollback, and download isolation. Deploy only after the source and extension copies match and a restarted daemon successfully opens `about:blank`.

---

## 📄 License

MIT License. Developed with ❤️ for advanced agentic pair-programming workflows.
