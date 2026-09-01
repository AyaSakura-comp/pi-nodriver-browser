import { spawn } from "node:child_process";
import { existsSync, readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { extname, join } from "node:path";
import { createConnection, type Socket } from "node:net";
import { fileURLToPath } from "node:url";
import { createInterface } from "node:readline";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { DEFAULT_MAX_BYTES, DEFAULT_MAX_LINES, formatSize, truncateHead } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const ROOT = fileURLToPath(new URL(".", import.meta.url));
const PYTHON = join(ROOT, ".venv", "bin", "python");
const WORKER = join(ROOT, "worker.py");
const MARKER = "__PI_NODRIVER__";
const MAX_BATCH_IMAGE_BYTES = 40 * 1024 * 1024;
const SOCKET = process.env.PI_NODRIVER_SOCKET || join(homedir(), ".pi", "agent", "nodriver-browser.sock");
const TIME_SENSITIVE_SEARCH_PATTERN = /(?:\b(?:19|20)\d{2}\b|\b(?:today|tomorrow|yesterday|now|current|currently|latest|recent|recently|upcoming|ago|date|time|timezone|schedule|deadline|release|price|stock|availability|exchange\s+rate|weather)\b|\bthis\s+(?:week|month|year)\b|\b(?:last|next)\s+(?:week|month|year)\b|今天|明天|昨天|現在|當下|目前|最新|最近|即將|本週|這週|上週|下週|本月|上月|下月|今年|去年|明年|日期|時間|時區|時程|排程|截止|發布|上市|價格|庫存|供貨|匯率|天氣|活動)/iu;
const FRESH_TIME_MAX_AGE_MS = 10 * 60 * 1000;

function parseGettimeValue(value: string): number | undefined {
  const match = value.trim().match(/^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\s+([+-]\d{2})(\d{2})(?:\s+\S+)?$/);
  if (!match) return undefined;
  const timestamp = Date.parse(`${match[1]}T${match[2]}${match[3]}:${match[4]}`);
  return Number.isFinite(timestamp) ? timestamp : undefined;
}

const DESCRIPTION = `Autonomous live browser automation (permanently fixed in iPhone Mobile Mode 390x844).
ROUTING GUIDELINES:
- WHEN TO USE BROWSER: Automatically invoke this tool when the user request requires live web data, real-time e-commerce pricing/promotions (MOMO, PChome, Amazon, Shopee), current stock availability, real-time exchange rates/schedules, dynamic web portals, interactive form submissions, UI flows, or login/OAuth authentication. No explicit user command like "use browser" is needed.
- WHEN NOT TO USE BROWSER: Do NOT use this tool for general knowledge, programming theory, algorithm design, historical facts, conceptual architecture questions, math calculations, or static knowledge that can be answered directly.
Guidelines:
- REF SYNTAX IS LITERAL: snapshot outputs refs like @e16. Use 'click @e16', 'fill @e6 "text"', or 'fill-submit @e2 "query"' exactly; never wrap refs in '<' or '>'. Angle brackets in generic documentation denote placeholders, not characters to type.
- Fast 2-Step Pattern: 'open <url>' automatically returns interactive page elements with @refs (no need to call snapshot -i). Then use a literal ref, for example 'fill-submit @e1 "query"', to fill and submit forms in 1 atomic step.
- Goal-Driven: Stop once the required info (price, stock, specs) is found, but for a concrete subject do not finalize until 1–3 genuinely useful image candidates already returned by get text/crawl have been delivered with fetch_images. This delivery step is completion, not over-exploration.
- Incidental Image Completion: Do not finalize a concrete-subject answer as text-only when get text/crawl returned relevant representative or content candidates. Call fetch_images with 1–3 non-duplicate direct URLs even when the user did not mention images; skip only irrelevant, logo/icon/ad/tracking, or low-confidence assets.
- One-Shot Overview & DOM Semantic Assist: For long pages, use 'screenshot --full' (or 'snapshot -i --full') to capture the entire scrollable page layout in 1 step. Its primary purpose is to visually locate elements, text, and structure to assist non-vision DOM browser clicks (e.g. click @ref, fill @ref, click-text); it is NOT for coordinate clicking. When a user requests 'screenshot', always default to the current Xvfb window screenshot (500x1000 viewport with full Chrome UI).
- Semantic-First Iframes: Controls inside same-origin iframes receive normal @refs plus frame labels. Use fill/select/click @ref, click-text, or click-css; never guess viewport coordinates for ordinary iframe controls.
- Searchable Dropdowns: Native <select> controls show their label, selected value, option count, and option type. Do not click them open or infer their contents from the first option. Use find-option "fuzzy keywords", then copy a returned complete 'Select exactly' command; its index and fingerprint prevent stale-option mistakes.
- Progressive Disclosure: Never crawl or get the full page merely to inspect a dropdown. find-option searches every option internally, includes control-label context, and diversifies the top candidates across dropdowns; ambiguous queries return choices instead of guessing.
- Preserve Form State: After selecting options, do not navigate, reload, or click recalculation/reset controls unless the user explicitly requires it; dynamic quote/configurator pages may clear selections. Verify with snapshot or screenshot instead.
- Form Control Safety: Snapshot annotates checkbox/radio label proxies with control type plus checked="true|false", required, and disabled state. Never fill or type into a <label> ref; fill/type accepts only text-editable input, textarea, or contenteditable refs.
- Exact Ref Before Text: When snapshot shows the desired control, click its @ref. Use click-text only when no suitable ref exists; 1–2 character queries require an exact match and longer fallback matches are prefix-only, preventing X from matching Next.
- Vision-Correct Coordinates: Raw coordinate clicks are blocked, and vision fallback stays locked until the browser records 3 consecutive legitimate semantic target-resolution failures on the same page/document. Invalid selectors, stale-guard retries, infrastructure errors, and fabricated failures do not count. After the browser reports VISION_FALLBACK_UNLOCKED, run 'screenshot', inspect the image, use its pixel coordinates with 'vision-mark <x> <y>', inspect the attached marked screenshot, re-mark until correct, then run the exact 'vision-click <preview-token>' command returned by the latest preview.
- No Wait: All actions auto-settle DOM/network synchronously; do not call wait.
- Open Loop Guard: At most 2 consecutive open actions to the same origin are allowed per session. A different-origin open resets the streak; the 3rd same-origin open is blocked until a non-open action or different-origin open runs.
- Tab LRU: Chrome is capped at 20 tabs globally. When capacity is needed, the least-recently-used inactive tab is evicted; recently operated, active-command, and in-progress-download tabs are protected.
Workflow: open URL (auto-returns DOM @refs) → fill-submit @input "query" (auto-returns results DOM) → report answer.
Commands:
  google-search <json> - Run up to four directional Google queries in parallel and return a globally de-duplicated Top 10
  crawl <url1> [url2]... - Crawl one or multiple URLs in parallel and return clean page text plus ranked image candidates
  open <url> - Navigate to URL (automatically returns interactive elements snapshot with @refs)
  fill-submit @e1 "query" - Clear, type, and submit form / press Enter in 1 atomic step (returns updated results snapshot)
  snapshot -i - List interactive elements and form-control state in the current viewport with compact @refs
  snapshot -i --full - Return a visual full-page overview only; then scroll and snapshot each relevant viewport
  click @e16 - Click the literal snapshot ref @e16, including custom controls and open Shadow DOM
  long-press @e16 [duration_ms] - Long press the literal snapshot ref for duration_ms (default 1000ms, sends trusted X11 mousedown -> hold -> mouseup)
  vision-mark <x> <y> - Draw a crosshair at screenshot-pixel coordinates on a copied current-viewport PNG without clicking; requires a fresh screenshot
  vision-click <preview-token> - Click the latest marked point only after inspecting the attached marked screenshot
  vision-mark-drag <start_x> <start_y> <end_x> <end_y> - Draw a visual drag trajectory (Green start circle -> Blue arrow -> Red end target) on screenshot for inspection and calibration without executing drag
  vision-drag [preview-token] [duration_ms] - Execute smooth hardware drag on Xvfb along the confirmed trajectory (isTrusted: true)
  vision-long-press [preview-token] [duration_ms] - Execute hardware long press at marked point for duration_ms (default 1000ms, isTrusted: true)
  click-text <text> - Click exact short text or a safe exact/prefix visible label match
  click-css <selector> - Click the first visible element matching a CSS selector, including open Shadow DOM
  click-js @e16 - Dispatch a deferred DOM click for the literal snapshot ref when a site's native mouse handler poisons CDP
  download-info @e16 - Inspect a download target without clicking it
  download @e16 [ms] - Click and wait for a verified completed download
  wait-download [ms] - Wait for the active or most recent download
  downloads [limit] - List recent files and in-progress download percentages
  download-latest - Return metadata and the absolute path of the newest completed file
  fetch-image <url> - Fetch and validate a direct HTTP(S) image URL, then return it inline with a sendable local path
  upload @e1 <file1> [file2]... - Upload local file(s) into the literal file-input/button/dropzone ref
  fill @e6 "text" - Clear a text-editable input/textarea/contenteditable ref and type; labels are rejected
  type @e6 "text" - Append text to a text-editable ref without clearing; labels are rejected
  find-option <keywords> - Fuzzy-search options across all labelled dropdowns and return ranked @ref/index candidates
  select @e43 <query|--index=N --fingerprint=HASH> - Fuzzy-select a confident option from the literal dropdown ref, or safely choose the exact candidate returned by find-option
  press <key> - Press only Enter, Tab, Space, or Backspace. To enter text, use fill or type with a literal ref
  scroll <down|up|top|bottom|left|right> [px] - Smart scroll page or nested container (returns position & 100% boundary feedback)
  get text|images|url|title [@ref] - Get page text with image candidates, image candidates only, URL, or title
  wait-popup [ms] - Wait for an OAuth/login popup and switch to it
  wait-popup-close [ms] - Wait for the active popup to close and return to its opener
  switch opener - Return to the popup's opener without closing the popup
  dismiss overlays [--cookies=accept|reject-optional|ignore] - Safely dismiss cookie and modal overlays
  screenshot [--full] - Default: capture current Xvfb window (500x1000 with Chrome UI, used for visual checks & vision-mark). With --full: capture complete scrollable page via CDP to assist non-vision DOM browser clicks (e.g. click @ref).
  close - Close only the current Pi session tab
  shutdown - Close Chrome and stop the persistent browser daemon
Use quoted text when an argument contains spaces. Re-run snapshot -i after navigation or major DOM changes. A missing/stale ref automatically returns a fresh DOM snapshot plus a viewport JPG for joint visual inspection, without performing the action; never retry the stale ref, and use only literal @eN refs from the authoritative recovery snapshot, which are available immediately.
Never send the same observing command (snapshot, screenshot, get, downloads, download-info) twice in a row: it cannot return anything new, and a third identical repeat is rejected with LOOP_GUARD. Never issue more than 2 consecutive open actions to the same origin: the 3rd same-origin attempt is rejected until a non-open action or different-origin open runs. On any loop guard, change target/approach or stop rather than retrying.`;

type WorkerResponse = {
  id: number;
  ok: boolean;
  text?: string;
  action?: string;
  screenshotPath?: string;
  imagePath?: string;
  mimeType?: string;
  error?: string;
  [key: string]: unknown;
};

class NodriverWorker {
  private socket?: Socket;
  private connecting?: Promise<Socket>;
  private nextId = 1;
  private pending = new Map<number, {
    resolve: (value: WorkerResponse) => void;
    reject: (error: Error) => void;
    timer: NodeJS.Timeout;
    removeAbortListener: () => void;
  }>();

  private openSocket(): Promise<Socket> {
    return new Promise((resolve, reject) => {
      const socket = createConnection(SOCKET);
      const timer = setTimeout(() => {
        socket.destroy();
        reject(new Error(`Timed out connecting to browser daemon: ${SOCKET}`));
      }, 500);
      socket.once("connect", () => {
        clearTimeout(timer);
        resolve(socket);
      });
      socket.once("error", (error) => {
        clearTimeout(timer);
        socket.destroy();
        reject(error);
      });
    });
  }

  private attach(socket: Socket) {
    this.socket = socket;
    const lines = createInterface({ input: socket });
    lines.on("line", (line) => {
      if (!line.startsWith(MARKER)) return;
      let response: WorkerResponse;
      try {
        response = JSON.parse(line.slice(MARKER.length));
      } catch {
        return;
      }
      const request = this.pending.get(response.id);
      if (!request) return;
      clearTimeout(request.timer);
      request.removeAbortListener();
      this.pending.delete(response.id);
      if (response.ok) request.resolve(response);
      else request.reject(new Error(response.error || "Nodriver command failed"));
    });
    socket.on("error", () => undefined);
    socket.on("close", () => {
      if (this.socket !== socket) return;
      this.socket = undefined;
      const error = new Error("Browser daemon connection closed");
      for (const request of this.pending.values()) {
        clearTimeout(request.timer);
        request.removeAbortListener();
        request.reject(error);
      }
      this.pending.clear();
    });
  }

  private async connectOrStart(): Promise<Socket> {
    if (!existsSync(PYTHON)) {
      throw new Error(`Nodriver browser environment is missing. Run: python3 -m venv ${join(ROOT, ".venv")} && ${PYTHON} -m pip install nodriver`);
    }
    let child: ReturnType<typeof spawn> | undefined;
    try {
      const socket = await this.openSocket();
      this.attach(socket);
      return socket;
    } catch {
      try {
        unlinkSync(SOCKET);
      } catch {}
      const screen = process.env.PI_NODRIVER_SCREEN || "500x1000x24";
      child = spawn(
        "xvfb-run",
        ["-a", "-s", `-screen 0 ${screen}`, PYTHON, WORKER, "--server", SOCKET],
        { cwd: ROOT, stdio: "ignore", detached: true },
      );
      child.unref();
    }

    let lastError: unknown;
    for (let attempt = 0; attempt < 100; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, 100));
      try {
        const socket = await this.openSocket();
        this.attach(socket);
        return socket;
      } catch (error) {
        lastError = error;
      }
    }
    if (child?.pid) {
      try {
        process.kill(-child.pid, "SIGTERM");
      } catch {
        // Process may already have exited while the final connection attempt ran.
      }
    }
    throw lastError instanceof Error ? lastError : new Error("Browser daemon did not start");
  }

  private async connection(): Promise<Socket> {
    if (this.socket && !this.socket.destroyed) return this.socket;
    if (!this.connecting) {
      this.connecting = this.connectOrStart().finally(() => {
        this.connecting = undefined;
      });
    }
    return this.connecting;
  }

  private async sendRequest(command: string, sessionId: string, signal?: AbortSignal): Promise<WorkerResponse> {
    const socket = await this.connection();
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      let settled = false;
      const sendCancel = () => {
        const cancelId = this.nextId++;
        socket.write(`${JSON.stringify({ id: cancelId, cancelId: id, sessionId })}\n`);
      };
      const finishWithError = (error: Error, cancel: boolean) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        signal?.removeEventListener("abort", abort);
        this.pending.delete(id);
        if (cancel) sendCancel();
        reject(error);
      };
      const abort = () => finishWithError(new Error("Browser command cancelled"), true);
      const timer = setTimeout(
        () => finishWithError(new Error(`Browser command timed out: ${command}`), true),
        90_000,
      );
      const removeAbortListener = () => signal?.removeEventListener("abort", abort);
      this.pending.set(id, { resolve, reject, timer, removeAbortListener });
      signal?.addEventListener("abort", abort, { once: true });

      if (signal?.aborted) {
        abort();
        return;
      }
      socket.write(`${JSON.stringify({ id, command, sessionId })}\n`, (error) => {
        if (error) finishWithError(error, false);
      });
    });
  }

  async request(command: string, sessionId: string, signal?: AbortSignal, retryCount = 1): Promise<WorkerResponse> {
    try {
      return await this.sendRequest(command, sessionId, signal);
    } catch (error) {
      if (
        retryCount > 0 &&
        error instanceof Error &&
        (error.message.includes("closed") ||
          error.message.includes("reset") ||
          error.message.includes("did not start") ||
          error.message.includes("connecting"))
      ) {
        this.disconnect();
        return await this.request(command, sessionId, signal, retryCount - 1);
      }
      throw error;
    }
  }

  disconnect() {
    const socket = this.socket;
    this.socket = undefined;
    socket?.destroy();
  }
}

export default function (pi: ExtensionAPI) {
  const worker = new NodriverWorker();
  let queue = Promise.resolve<unknown>(undefined);

  pi.registerTool({
    name: "browser",
    label: "Browser (Nodriver + Xvfb)",
    description: DESCRIPTION,
    promptSnippet: "Interact with web pages using a persistent Nodriver-controlled Chrome browser",
    promptGuidelines: [
      "Use browser for interactive web tasks that require clicking, typing, selecting, scrolling, or screenshots on a live page.",
      "For reading or scraping full content from one or multiple URLs, prefer using the crawl tool (or browser command 'crawl <urls...>') which runs multi-tab parallel extraction without opening persistent tabs.",
      "After a web_search, judge whether the snippets actually answer the question. When they do not — the answer needs figures, quotes, code, or detail the snippet only alludes to — follow up with crawl on the promising result URLs instead of answering from snippets alone.",
      "Batch that follow-up into ONE crawl call carrying every URL you want. The pages themselves fetch in well under a second either way; what costs real time is the agent round-trip around each call, so one call with ten URLs finishes in a fraction of the time ten calls take.",
      "Do not open browser for general research or factual look-ups that web_search already answers.",
      "Never repeat an identical browser command; if a command returned nothing useful, change approach instead of retrying, and if two different approaches fail, leave the browser and answer by other means rather than continuing to poll.",
      "Never issue more than 2 consecutive browser open actions to the same origin. OPEN_LOOP_GUARD blocks the 3rd same-origin open until a non-open action or different-origin open runs; use the current page or batch same-site URLs with crawl instead.",
      "Browser enforces a global 20-tab LRU limit. Inactive least-recently-used tabs may be evicted automatically; tabs currently executing commands or downloading are protected.",
      "Do NOT scroll repeatedly back and forth looking for terms or sections. If looking for product specs, warranty terms, or details on a long page, use 'get text' to extract all text and ranked image candidates from the page in 1 step, or 'screenshot --full' to view the entire layout.",
      "After opening the selected page for a concrete product, person, place, animal, or event, use 'get text' once; when its image candidates are genuinely useful, call fetch_images with 1–3 non-duplicate candidates and include the returned markers even when the user did not explicitly ask for images.",
      "Do not finalize a concrete-subject answer as text-only after get text or crawl returned relevant representative/content image candidates; fetching those candidates is part of answer completion, not extra browsing.",
      "For e-commerce pages with specs or options (e.g. degrees, sizes, colors), select the spec first (e.g. click @ref for '400度' or '請選擇商品規格'), then click @ref to add to cart. Spec selection drawers are in-page modals; run snapshot -i after opening, and do NOT use wait-popup.",
      "A LOOP_GUARD or SCROLL_LOOP_GUARD error means the browser is not making progress: stop scrolling, and use 'get text', 'screenshot --full', or answer with your own knowledge.",
      "Browser refs are literal tokens such as @e16: send `click @e16` or `fill @e6 \"text\"`; never type angle brackets around a ref.",
      "Never fill or type into a <label> ref. Use only a snapshot ref whose tag is input, textarea, or contenteditable; checkbox/radio label proxies are for click and expose checked state.",
      "Use browser press only for control keys such as Enter, Tab, Space, or Backspace. To enter text, use fill or type with a literal ref; never use press for an email address or other field value.",
      "With browser, run snapshot -i before referencing page elements and re-run it after navigation or major DOM changes; normal snapshots include only the current viewport.",
      "Use snapshot -i --full only for a visual overview: inspect the image first, then scroll up/down and run snapshot -i in each relevant viewport; do not claim an object is missing before checking likely sections and the relevant page boundary.",
      "A missing or stale @ref does not perform the action and automatically returns both a fresh authoritative DOM snapshot and viewport image for joint inspection; never retry the old ref, and use the returned fresh refs immediately after reassessing the page.",
      "Raw coordinate clicks are blocked. Vision fallback unlocks only after the browser records 3 consecutive legitimate semantic target-resolution failures on the same page/document; invalid selectors, stale-guard retries, infrastructure errors, and fabricated failures do not count. Once unlocked, use the image-bearing sequence: screenshot → inspect → vision-mark <x> <y> → inspect the attached marked screenshot → re-mark if needed → vision-click <preview-token>. Never confirm a marker before visually checking the image returned by vision-mark.",
      "Send exactly one browser command per tool call; never combine commands with &&, ||, ;, or pipes.",
      "For downloads, inspect with download-info and prefer a literal command such as `download @e16` over clicking and guessing; use downloads to check progress.",
      "To deliver a screenshot or downloaded file to the user on PiWeb / Discord, you MUST emit '[[image: <path>]]' or '[[file: <path>]]' in your reply prose. Do NOT use markdown '![alt](/tmp/...)' and do NOT rely on 'read'.",
      "Browser close affects only the current Pi session tab; browser shutdown stops the shared daemon for every session.",
      "If the browser daemon is down or restarting, simply re-run your browser command (e.g. `open <url>`); the extension auto-spawns and recovers the browser daemon automatically without any external service commands or skills.",
    ],
    parameters: Type.Object({
      command: Type.String({ description: "Nodriver browser command, without a prefix" }),
    }),

    async execute(_toolCallId, params, signal, _onUpdate, ctx) {
      const sessionId = ctx.sessionManager.getSessionId();
      const run = async () => worker.request(params.command.trim(), sessionId, signal);
      const responsePromise = queue.then(run, run);
      queue = responsePromise.catch(() => undefined);
      const response = await responsePromise;
      const text = response.text || "(no output)";

      if (response.screenshotPath) {
        const image = readFileSync(response.screenshotPath);
        const extension = extname(response.screenshotPath).toLowerCase();
        const mimeType = extension === ".jpg" || extension === ".jpeg" ? "image/jpeg" : "image/png";
        const returnText = `${text}\n(To send this screenshot to user, include '[[image: ${response.screenshotPath}]]' in your reply)`;
        return {
          content: [
            { type: "text" as const, text: returnText },
            { type: "image" as const, data: image.toString("base64"), mimeType },
          ],
          details: response,
        };
      }

      if (response.imagePath) {
        const image = readFileSync(response.imagePath);
        const mimeType = response.mimeType || "image/png";
        const marker = `[[image: ${response.imagePath}]]`;
        const returnText = text.includes(marker)
          ? text
          : `${text}\n(To send this image to user, include '${marker}' in your reply)`;
        return {
          content: [
            { type: "text" as const, text: returnText },
            { type: "image" as const, data: image.toString("base64"), mimeType },
          ],
          details: response,
        };
      }

      const truncation = truncateHead(text, { maxLines: DEFAULT_MAX_LINES, maxBytes: DEFAULT_MAX_BYTES });
      let output = truncation.content;
      if (truncation.truncated) {
        output += `\n\n[Output truncated: ${truncation.outputLines}/${truncation.totalLines} lines, ${formatSize(truncation.outputBytes)}/${formatSize(truncation.totalBytes)}.]`;
      }
      return {
        content: [{ type: "text" as const, text: output }],
        details: { ...response, truncated: truncation.truncated },
      };
    },
  });

  pi.registerTool({
    name: "fetch_image",
    label: "Fetch Image",
    description: "Fetch and validate one direct HTTP/HTTPS image URL discovered by web_search, crawl, or browser. Returns the image inline plus a local path that can be sent to PiWeb or Discord.",
    promptSnippet: "Fetch a direct image URL found by search, crawl, or browser and return the image inline",
    promptGuidelines: [
      "Use fetch_image after web_search, crawl, or browser when the user asks to see or receive a discovered image.",
      "Pass fetch_image a direct HTTP/HTTPS image URL, not an article, gallery, search-results, or HTML page URL.",
      "After fetch_image succeeds, include the exact '[[image: <path>]]' marker it returns in the final reply so PiWeb or Discord actually receives the image.",
    ],
    parameters: Type.Object({
      url: Type.String({ description: "Direct HTTP/HTTPS image URL discovered by web_search, crawl, or browser" }),
    }),
    async execute(_toolCallId, params, signal, _onUpdate, ctx) {
      const sessionId = ctx.sessionManager.getSessionId();
      const command = `fetch-image ${JSON.stringify(params.url)}`;
      const run = async () => worker.request(command, sessionId, signal);
      const responsePromise = queue.then(run, run);
      queue = responsePromise.catch(() => undefined);
      const response = await responsePromise;
      if (!response.imagePath) {
        throw new Error("Browser worker did not return a fetched image path");
      }
      const image = readFileSync(response.imagePath);
      const mimeType = response.mimeType || "image/png";
      const marker = `[[image: ${response.imagePath}]]`;
      const responseText = response.text || "Image fetched";
      const text = responseText.includes(marker)
        ? responseText
        : `${responseText}\nSend it to the user with exactly: ${marker}`;
      return {
        content: [
          { type: "text" as const, text },
          { type: "image" as const, data: image.toString("base64"), mimeType },
        ],
        details: response,
      };
    },
  });

  pi.registerTool({
    name: "fetch_images",
    label: "Fetch Images in Parallel",
    description: "Securely fetch and validate up to four direct HTTP/HTTPS image URLs in parallel. Returns exact PiWeb/Discord delivery markers without injecting image bytes into the next model turn.",
    promptSnippet: "Fetch several selected direct image URLs in parallel and return their delivery markers",
    promptGuidelines: [
      "Use fetch_images for 1–3 useful, non-duplicate candidates returned by crawl or browser; use at most four.",
      "For concrete products, people, places, animals, or events, include useful images even when the user did not explicitly ask for images, unless the images are irrelevant, low-confidence, logos, icons, ads, or tracking assets.",
      "Pass only direct HTTP/HTTPS image URLs. Do not pass article, gallery, search-results, blob, data, or HTML page URLs.",
      "After fetch_images succeeds, include each exact '[[image: <path>]]' marker it returns in the final reply.",
    ],
    parameters: Type.Object({
      urls: Type.Array(Type.String({ description: "Direct HTTP/HTTPS image URL selected from crawl or browser candidates" }), {
        minItems: 1,
        maxItems: 4,
        description: "One to four unique direct image URLs to fetch concurrently",
      }),
    }),
    async execute(_toolCallId, params, signal, _onUpdate, ctx) {
      const sessionId = ctx.sessionManager.getSessionId();
      const urls = Array.from(new Set((params.urls || []).map((url) => String(url).trim()).filter(Boolean))).slice(0, 4);
      if (urls.length === 0) {
        throw new Error("fetch_images requires at least one direct image URL");
      }
      if (signal?.aborted) {
        throw new Error("fetch_images cancelled");
      }
      const settled = await Promise.allSettled(
        urls.map((url) => worker.request(`fetch-image ${JSON.stringify(url)}`, sessionId, signal)),
      );
      const successes = settled
        .filter((result): result is PromiseFulfilledResult<WorkerResponse> => result.status === "fulfilled")
        .map((result) => result.value)
        .filter((response) => Boolean(response.imagePath));
      const failedCount = settled.length - successes.length;
      if (signal?.aborted) {
        throw new Error("fetch_images cancelled");
      }
      if (successes.length === 0) {
        throw new Error(`All ${urls.length} parallel image fetches failed`);
      }

      let attachmentBytes = 0;
      const deliverable: Array<{ response: WorkerResponse; size: number }> = [];
      for (const response of successes) {
        const size = statSync(String(response.imagePath)).size;
        if (attachmentBytes + size > MAX_BATCH_IMAGE_BYTES) continue;
        attachmentBytes += size;
        deliverable.push({ response, size });
      }
      if (deliverable.length === 0) {
        throw new Error("Parallel image results exceed the aggregate attachment byte limit");
      }
      const omittedForBudget = successes.length - deliverable.length;
      const markers = deliverable.map(({ response }) => `[[image: ${response.imagePath}]]`);
      const text = [
        `Fetched ${deliverable.length}/${urls.length} images in parallel${failedCount ? `; ${failedCount} failed validation or download` : ""}${omittedForBudget ? `; ${omittedForBudget} omitted by the aggregate attachment limit` : ""}.`,
        "Include each successful image in the final reply with exactly these markers:",
        ...markers,
      ].join("\n");
      return {
        content: [{ type: "text" as const, text }],
        details: {
          successCount: deliverable.length,
          failedCount,
          omittedForBudget,
          totalCount: urls.length,
          attachmentBytes,
          results: deliverable.map(({ response }) => response),
        },
      };
    },
  });

  pi.registerTool({
    name: "google_search",
    label: "Directional Google Search",
    description: "Search Google directly through Nodriver with one to four distinct query directions in parallel. Returns a globally de-duplicated, diversity-balanced Top 10 in the same title/URL/snippet format as web_search.",
    promptSnippet: "Run 1–4 directional Google searches in parallel and return a de-duplicated Top 10",
    promptGuidelines: [
      "Use google_search when broad research benefits from multiple non-overlapping Google query directions; use web_search for a single fast discovery query.",
      "For google_search, choose two to four task-appropriate directions. Good defaults are official or primary sources; current news or date-specific updates; independent reviews or community experience; and alternatives, risks, or counter-evidence.",
      "Do not mechanically use all four defaults when they do not fit. For shopping, prefer official specifications, retailer availability, independent reviews, and competing products; for technical research, prefer official docs, recent changes, implementation experience, and known limitations.",
      "For every google_search involving a date, time, relative time, recency, schedule, release, current price/stock, or other time-sensitive fact, first call gettime with action now, then copy its complete output into google_search.currentTime. The tool rejects missing or stale timestamps. Use the confirmed current year when a year improves retrieval; never default to 2025 from model memory.",
      "google_search already searches all supplied directions concurrently and removes duplicate destination URLs globally, so call it once with the complete direction set rather than issuing sibling google_search calls.",
      "After google_search, use crawl once with all promising result URLs when snippets are insufficient for the requested analysis or exact details.",
    ],
    parameters: Type.Object({
      currentTime: Type.Optional(Type.String({
        description: "For time-related searches, the complete fresh timestamp returned by gettime(action: now), e.g. 2026-08-30 15:52:06 +0800 CST",
      })),
      searches: Type.Array(
        Type.Object({
          direction: Type.String({ description: "Short label for this distinct search direction" }),
          query: Type.String({ description: "Google query for this direction" }),
        }),
        {
          minItems: 1,
          maxItems: 4,
          description: "One to four distinct search directions executed concurrently",
        },
      ),
    }),
    async execute(_toolCallId, params, signal, _onUpdate, ctx) {
      const sessionId = ctx.sessionManager.getSessionId();
      const seen = new Set<string>();
      const searches = (params.searches || [])
        .map((search) => ({
          direction: String(search.direction || "").trim(),
          query: String(search.query || "").trim(),
        }))
        .filter((search) => {
          const key = search.query.toLocaleLowerCase();
          if (!search.direction || !search.query || seen.has(key)) return false;
          seen.add(key);
          return true;
        })
        .slice(0, 4);
      if (searches.length === 0) {
        throw new Error("google_search requires at least one non-empty directional query");
      }
      const isTimeSensitive = searches.some((search) =>
        TIME_SENSITIVE_SEARCH_PATTERN.test(`${search.direction} ${search.query}`),
      );
      let confirmedCurrentTime: string | undefined;
      if (isTimeSensitive) {
        confirmedCurrentTime = String(params.currentTime || "").trim();
        const suppliedTimestamp = parseGettimeValue(confirmedCurrentTime);
        if (suppliedTimestamp === undefined || Math.abs(Date.now() - suppliedTimestamp) > FRESH_TIME_MAX_AGE_MS) {
          throw new Error('Time-sensitive google_search requires a fresh gettime(action: "now") result in currentTime. Call gettime first, copy its complete output, and retry.');
        }
      }
      const command = `google-search ${JSON.stringify(searches)}`;
      const run = async () => worker.request(command, sessionId, signal);
      const responsePromise = queue.then(run, run);
      queue = responsePromise.catch(() => undefined);
      const response = await responsePromise;
      const text = response.text || "(no Google results)";
      const truncation = truncateHead(text, { maxLines: DEFAULT_MAX_LINES, maxBytes: DEFAULT_MAX_BYTES });
      let output = truncation.content;
      if (confirmedCurrentTime) {
        output = `[Current time confirmed: ${confirmedCurrentTime}]\n\n${output}`;
      }
      if (truncation.truncated) {
        output += `\n\n[Output truncated: ${truncation.outputLines}/${truncation.totalLines} lines, ${formatSize(truncation.outputBytes)}/${formatSize(truncation.totalBytes)}.]`;
      }
      return {
        content: [{ type: "text" as const, text: output }],
        details: { ...response, confirmedCurrentTime, truncated: truncation.truncated },
      };
    },
  });

  pi.registerTool({
    name: "crawl",
    label: "Parallel Browser Crawl",
    description: "Crawl one or multiple web pages in parallel using local headful Chromium with fast-path DOM ready. Returns clean page text plus ranked image candidates without downloading image bytes.",
    promptSnippet: "Crawl one or multiple web URLs in parallel to extract clean page text plus ranked image candidates",
    promptGuidelines: [
      "Use crawl when you need the full content of one or multiple web pages.",
      "Reach for it right after web_search whenever the snippets are too thin to answer from: crawl the promising result URLs rather than guessing at what the pages say.",
      "Always pass every URL you want in a SINGLE call. Splitting them costs one agent round-trip per URL, which dwarfs the fetch itself — measured on this setup, four real pages came back in about 1.5s in one call, so the fetching was never the bottleneck.",
      "Do not pre-filter down to a single 'best' URL out of caution. Crawling several and comparing is cheap here, and a failed page is reported per-URL without affecting the others.",
      "Crawl executes JavaScript, bypasses anti-bot barriers, and extracts clean readable text plus ranked image candidates from all pages simultaneously.",
      "When crawl returns genuinely useful image candidates for a concrete product, person, place, animal, or event, proactively choose 1–3 non-duplicate images and call fetch_images before the final answer, even when the user did not explicitly ask for images; do not merely list image URLs. Skip logos, icons, ads, tracking assets, and low-confidence candidates.",
      "After fetch_images succeeds, copy every returned [[image: <path>]] marker into the final reply so PiWeb or Discord attaches the selected images.",
    ],
    parameters: Type.Object({
      urls: Type.Array(Type.String({ description: "URL to crawl" }), {
        description: "List of HTTP/HTTPS URLs to crawl concurrently in parallel, e.g. [\"https://example.com/1\", \"https://example.com/2\"]",
      }),
    }),
    async execute(_toolCallId, params, signal, _onUpdate, ctx) {
      const sessionId = ctx.sessionManager.getSessionId();
      let rawText = "";
      if (Array.isArray(params.urls)) {
        rawText = params.urls.map((u) => String(u)).join(" ");
      } else if (typeof params.urls === "string") {
        rawText = params.urls;
      } else if (params.urls) {
        rawText = JSON.stringify(params.urls);
      }
      const matched = rawText.match(/https?:\/\/[^\s"'\]\[\<\>]+/g);
      const urlList = matched ? Array.from(new Set(matched.map((u) => u.replace(/[.,;)]+$/, "")))) : [];
      if (urlList.length === 0) {
        return {
          content: [{ type: "text" as const, text: "Error: No valid URLs provided to crawl." }],
          details: { error: "No valid URLs provided to crawl" },
        };
      }
      const command = `crawl ${JSON.stringify(urlList)}`;
      const run = async () => worker.request(command, sessionId, signal);
      const responsePromise = queue.then(run, run);
      queue = responsePromise.catch(() => undefined);
      const response = await responsePromise;
      const text = response.text || "(no output)";

      const truncation = truncateHead(text, { maxLines: DEFAULT_MAX_LINES, maxBytes: DEFAULT_MAX_BYTES });
      let output = truncation.content;
      if (truncation.truncated) {
        output += `\n\n[Output truncated: ${truncation.outputLines}/${truncation.totalLines} lines, ${formatSize(truncation.outputBytes)}/${formatSize(truncation.totalBytes)}.]`;
      }
      return {
        content: [{ type: "text" as const, text: output }],
        details: { ...response, truncated: truncation.truncated },
      };
    },
  });

  pi.on("session_shutdown", async () => {
    worker.disconnect();
  });
}
