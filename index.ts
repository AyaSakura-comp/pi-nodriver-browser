import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
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
const SOCKET = process.env.PI_NODRIVER_SOCKET || join(homedir(), ".pi", "agent", "nodriver-browser.sock");

const DESCRIPTION = `Autonomous live browser automation (permanently fixed in iPhone Mobile Mode 390x844).
ROUTING GUIDELINES:
- WHEN TO USE BROWSER: Automatically invoke this tool when the user request requires live web data, real-time e-commerce pricing/promotions (MOMO, PChome, Amazon, Shopee), current stock availability, real-time exchange rates/schedules, dynamic web portals, interactive form submissions, UI flows, or login/OAuth authentication. No explicit user command like "use browser" is needed.
- WHEN NOT TO USE BROWSER: Do NOT use this tool for general knowledge, programming theory, algorithm design, historical facts, conceptual architecture questions, math calculations, or static knowledge that can be answered directly.
Guidelines:
- Fast 2-Step Pattern: 'open <url>' automatically returns interactive page elements with @refs (no need to call snapshot -i). Then use 'fill-submit <@ref> <text>' to fill and submit forms in 1 atomic step.
- Goal-Driven: Stop and report immediately once the required info (price, stock, specs) is found in search results or current view. Do not over-explore sub-pages.
- One-Shot Overview: For long pages, use 'snapshot -i --full' or 'screenshot --full' to capture the entire layout in 1 step rather than scrolling up and down in loops.
- No Wait: All actions auto-settle DOM/network synchronously; do not call wait.
Workflow: open URL (auto-returns DOM @refs) → fill-submit @input "query" (auto-returns results DOM) → report answer.
Commands:
  crawl <url1> [url2]... - Crawl one or multiple URLs in parallel and return clean extracted markdown
  open <url> - Navigate to URL (automatically returns interactive elements snapshot with @refs)
  fill-submit <@ref> <text> - Clear, type, and submit form / press Enter in 1 atomic step (returns updated results snapshot)
  snapshot -i - List interactive elements in the current viewport with compact @refs
  snapshot -i --full - Return a visual full-page overview only; then scroll and snapshot each relevant viewport
  click <@ref> - Click a snapshot element, including custom controls and open Shadow DOM
  click <x> <y> - Click viewport coordinates (fallback for canvas, cross-origin iframes, and visual-only controls)
  click-text <text> - Click the best visible element matching text or accessible label
  click-css <selector> - Click the first visible element matching a CSS selector, including open Shadow DOM
  click-js <@ref> - Dispatch a deferred DOM click when a site's native mouse handler poisons CDP
  download-info <@ref> - Inspect a download target without clicking it
  download <@ref> [ms] - Click and wait for a verified completed download
  wait-download [ms] - Wait for the active or most recent download
  downloads [limit] - List recent files and in-progress download percentages
  download-latest - Return metadata and the absolute path of the newest completed file
  upload <@ref> <file1> [file2]... - Upload local file(s) into file input or button/dropzone wrapper
  fill <@ref> <text> - Clear and type
  type <@ref> <text> - Type without clearing
  select <@ref> <value> - Select dropdown option
  press <key> - Press Enter, Tab, Space, Backspace, or text
  scroll <up|down|left|right> [px] - Scroll page
  get text|url|title [@ref] - Get information
  wait-popup [ms] - Wait for an OAuth/login popup and switch to it
  wait-popup-close [ms] - Wait for the active popup to close and return to its opener
  switch opener - Return to the popup's opener without closing the popup
  dismiss overlays [--cookies=accept|reject-optional|ignore] - Safely dismiss cookie and modal overlays
  screenshot [--full] - Capture screenshot and return it inline
  close - Close only the current Pi session tab
  shutdown - Close Chrome and stop the persistent browser daemon
Use quoted text when an argument contains spaces. Re-run snapshot -i after navigation or major DOM changes. A missing/stale ref automatically returns a fresh DOM snapshot plus a viewport JPG for joint visual inspection, without performing the action; ref-based commands remain blocked until you run snapshot -i, so never retry the old ref.
Never send the same observing command (snapshot, screenshot, get, downloads, download-info) twice in a row: it cannot return anything new, and a third identical repeat is rejected with LOOP_GUARD. On LOOP_GUARD, leave the browser and answer with web search or your own knowledge rather than retrying.`;

type WorkerResponse = {
  id: number;
  ok: boolean;
  text?: string;
  action?: string;
  screenshotPath?: string;
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
      child = spawn(
        "xvfb-run",
        ["-a", "-s", "-screen 0 1600x1000x24", PYTHON, WORKER, "--server", SOCKET],
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

  async request(command: string, sessionId: string, signal?: AbortSignal): Promise<WorkerResponse> {
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
      "A LOOP_GUARD error means the same observing command was repeated and the browser is not making progress: stop using browser for this question and answer with web search or your own knowledge.",
      "With browser, run snapshot -i before referencing page elements and re-run it after navigation or major DOM changes; normal snapshots include only the current viewport.",
      "Use snapshot -i --full only for a visual overview: inspect the image first, then scroll up/down and run snapshot -i in each relevant viewport; do not claim an object is missing before checking likely sections and the relevant page boundary.",
      "A missing or stale @ref does not perform the action and automatically returns both a fresh DOM snapshot and viewport image for joint inspection; run snapshot -i next to unlock ref-based commands, and never retry the old ref.",
      "Send exactly one browser command per tool call; never combine commands with &&, ||, ;, or pipes.",
      "For downloads, inspect with download-info and prefer download <@ref> over clicking and guessing; use downloads to check progress.",
      "Browser close affects only the current Pi session tab; browser shutdown stops the shared daemon for every session.",
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
        return {
          content: [
            { type: "text" as const, text },
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
    name: "crawl",
    label: "Parallel Browser Crawl",
    description: "Crawl one or multiple web pages in parallel using local headful Chromium with fast-path DOM ready. Replaces Firecrawl with zero rate-limit and faster multi-tab speed.",
    promptSnippet: "Crawl one or multiple web URLs in parallel to extract full clean page text",
    promptGuidelines: [
      "Use crawl when you need the full content of one or multiple web pages.",
      "Reach for it right after web_search whenever the snippets are too thin to answer from: crawl the promising result URLs rather than guessing at what the pages say.",
      "Always pass every URL you want in a SINGLE call. Splitting them costs one agent round-trip per URL, which dwarfs the fetch itself — measured on this setup, four real pages came back in about 1.5s in one call, so the fetching was never the bottleneck.",
      "Do not pre-filter down to a single 'best' URL out of caution. Crawling several and comparing is cheap here, and a failed page is reported per-URL without affecting the others.",
      "Crawl executes JavaScript, bypasses anti-bot barriers, and extracts clean readable text from all pages simultaneously.",
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
        return { content: [{ type: "text" as const, text: "Error: No valid URLs provided to crawl." }] };
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
