import { readBrowserConfig } from "./browser-config.ts";
import { launchWorker, type LaunchedWorker } from "./worker-launch.ts";
import registerBrowserIntent from "./browser-intent-tool.ts";
import { randomUUID } from "node:crypto";
import { ResearchPlanner, hostClock } from "./research-model.ts";
import { existsSync, readFileSync, statSync, unlinkSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { createConnection, type Socket } from "node:net";
import { fileURLToPath } from "node:url";
import { createInterface } from "node:readline";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

/** Subset of Pi's ctx.prefill (pi-coding-agent PrefillApi); absent on older Pi builds. */
interface PrefillHandle {
  append(text: string): boolean;
  end(): void;
  cancel(reason?: string): void;
  stats(): Record<string, unknown>;
}
interface PrefillApi { begin(toolCallId: string): PrefillHandle | undefined; }

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
  async ensureStarted() { await this.connection(); }
  private socket?: Socket;
  private connecting?: Promise<Socket>;
  private nextId = 1;
  private usedSessionIds = new Set<string>();
  private pending = new Map<number, {
    resolve: (value: WorkerResponse) => void;
    reject: (error: Error) => void;
    timer: NodeJS.Timeout;
    removeAbortListener: () => void;
    onFrame?: (frame: WorkerResponse) => void;
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
      if (response.type === "planner_request" || response.type === "progress") {
        if (request.onFrame) request.onFrame(response);
        return;
      }
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
    let launched: LaunchedWorker | undefined;
    try {
      const socket = await this.openSocket();
      this.attach(socket);
      return socket;
    } catch {
      try {
        unlinkSync(SOCKET);
      } catch {}
      const screen = process.env.PI_NODRIVER_SCREEN || (process.env.PI_NODRIVER_FRAME_WIDTH === "390" ? "500x1000x24" : "1366x768x24");
      // Own systemd unit when available: restarting this Pi process's service must not
      // take the shared Xvfb/Chrome down with it (worker-launch.ts).
      launched = await launchWorker({ root: ROOT, python: PYTHON, worker: WORKER, socket: SOCKET, screen });
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
    launched?.stop();
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

  private async sendRequest(command: string, sessionId: string, signal?: AbortSignal,
    research?: Record<string, unknown>, onFrame?: (frame: WorkerResponse) => void): Promise<WorkerResponse> {
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
        research ? 300_000 : 90_000,
      );
      const removeAbortListener = () => signal?.removeEventListener("abort", abort);
      this.pending.set(id, { resolve, reject, timer, removeAbortListener, onFrame });
      signal?.addEventListener("abort", abort, { once: true });

      if (signal?.aborted) {
        abort();
        return;
      }
      socket.write(`${JSON.stringify({ id, command, sessionId, ...(research ? { research } : {}) })}\n`, (error) => {
        if (error) finishWithError(error, false);
      });
    });
  }

  async request(command: string, sessionId: string, signal?: AbortSignal, retryCount = 1): Promise<WorkerResponse> {
    this.usedSessionIds.add(sessionId);
    try {
      return await this.sendRequest(command, sessionId, signal);
    } catch (error) {
      if (
        retryCount > 0 &&
        // Python accepts shlex-quoted/escaped actions. Only canonical plain
        // action tokens are eligible for replay; ambiguous spellings fail closed.
        /^[a-z][a-z0-9-]*(?:\s|$)/i.test(command.trim()) &&
        !/^(?:image-search(?:-batch|-results|-select)?|google-lens(?:-results|-select)?)(?:\s|$)/i.test(command.trim()) &&
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

  async addQuery(sessionId: string, payload: Record<string, unknown>) {
    await this.sendRequest("research-add-query", sessionId, undefined, payload);
  }

  async prefetch(query: string, index: number, sessionId: string) {
    this.usedSessionIds.add(sessionId);
    await this.sendRequest("research-prefetch", sessionId, undefined, { query, index });
  }

  async research(params: Record<string, unknown>, sessionId: string, planner: ResearchPlanner, signal?: AbortSignal,
    onEvidence?: (prefix: string) => void) {
    this.usedSessionIds.add(sessionId);
    const lifetime = new AbortController();
    const abort = () => lifetime.abort();
    signal?.addEventListener("abort", abort, { once: true });
    if (signal?.aborted) abort();
    try {
      // No replay: a lost response may already have consumed query budget.
      return await this.sendRequest("research", sessionId, lifetime.signal, params, (frame) => {
        if (frame.type === "progress" && frame.jobId === params.jobId &&
            frame.phase === "evidence" && typeof frame.prefix === "string") {
          onEvidence?.(frame.prefix);
          return;
        }
        if (frame.type !== "planner_request" || frame.jobId !== params.jobId ||
            typeof frame.requestId !== "string" || !Array.isArray(frame.providers)) return;
        const socket = this.socket;
        void (async () => {
          let reply: Record<string, unknown>;
          try {
            const proposal = await planner.plan(frame.view as Parameters<ResearchPlanner["plan"]>[0],
              frame.providers as string[], lifetime.signal);
            reply = { proposal };
          } catch { reply = { error: "planner_failed" }; }
          if (!lifetime.signal.aborted && socket && !socket.destroyed) {
            socket.write(`${JSON.stringify({type:"planner_reply",id:frame.id,jobId:frame.jobId,requestId:frame.requestId,...reply})}\n`);
          }
        })();
      });
    } finally {
      lifetime.abort(); signal?.removeEventListener("abort", abort);
    }
  }

  async cleanupSession(sessionId: string) {
    if (!this.usedSessionIds.has(sessionId)) return;
    await this.sendRequest("session-cleanup", sessionId);
    this.usedSessionIds.delete(sessionId);
  }

  disconnect() {
    const socket = this.socket;
    this.socket = undefined;
    for (const request of this.pending.values()) {
      clearTimeout(request.timer);
      request.removeAbortListener();
      request.reject(new Error("Browser daemon connection closed"));
    }
    this.pending.clear();
    socket?.destroy();
  }
}

// Models (especially with little thinking) often write last year into queries
// for "tomorrow"/"this week"; stale years return last year's pages. A year in the
// past five years is moved to the current year unless the user named that year or
// asked about the past.
const PAST_INTENT = /去年|前年|上一年|往年|歷年|過去|歷史|以前|當年|last year|previous year|history|historical/i;
function currentYearQuery(query: string, userText: string | undefined, year = new Date().getFullYear()): string {
  const asked = userText ?? "";
  if (PAST_INTENT.test(asked)) return query;
  return query.replace(/(?<!\d)(20\d{2})(?!\d)/g, (match) => {
    const value = Number(match);
    return value < year && value >= year - 5 && !asked.includes(match) ? String(year) : match;
  });
}

// Browser and search guidance belongs to the tools, not Pi's system prompt:
// promptSnippet/promptGuidelines are folded into the tool description (the
// tool schema) so no browser rule is rendered into the system prompt.
function guidanceInDescription<T extends {description: string; promptSnippet?: string; promptGuidelines?: string[]}>(tool: T) {
  const {promptSnippet: _snippet, promptGuidelines, ...rest} = tool;
  const rules = (promptGuidelines ?? []).map((rule) => rule.trim()).filter(Boolean);
  return {...rest, description: rules.length ? `${tool.description}\n\nUsage rules:\n${rules.map((rule) => `- ${rule}`).join("\n")}` : tool.description};
}

export default function (pi: ExtensionAPI) {
  const toolApi: ExtensionAPI = {...pi, registerTool: (tool) => pi.registerTool(guidanceInDescription(tool))};
  // crawlGuards: false exempts crawl from RESEARCH_FIRST and RESEARCH_DONE_GUARD.
  const { crawlGuards } = readBrowserConfig();
  const worker = new NodriverWorker();
  const ACTIVE_PREFILL = process.env.RESEARCH_ACTIVE_PREFILL !== "0";
  // Experiment: every web lookup starts with a breadth google_search (scattered directions),
  // then research picks its queries from what Google turned up. Off by default.
  const GOOGLE_FIRST = process.env.PI_RESEARCH_GOOGLE_FIRST === "1";
  registerBrowserIntent(toolApi, worker);
  let queue = Promise.resolve<unknown>(undefined);
  const sessionId = (ctx: { sessionManager: { getSessionId(): string } }) => ctx.sessionManager.getSessionId();

  // research returns the complete evidence for one user message. Agents still
  // wandered off to crawl / fetch more afterwards (minimal thinking ignores the
  // prompt rule), so follow-up lookups are blocked until the next user message
  // unless that message asks for them (a URL, Google, crawling, images, more search).
  const researchDone = new Map<string, boolean>();
  const researchBlocks = new Map<string, number>();
  const researchTried = new Map<string, boolean>();
  // GOOGLE_FIRST: google_search calls this message, and whether research was already sent back once.
  const googleCalls = new Map<string, number>();
  const googleFirstBlocked = new Map<string, boolean>();
  const lastInput = new Map<string, string>();
  // Research answers are breadth first (every option, one or two lines each) until the
  // user picks something to expand: then the same session switches to depth.
  const answerMode = new Map<string, "breadth" | "depth">();
  // research cannot read pages that render their data with JavaScript (live queue numbers,
  // stock, seat maps). One browser_intent call on a URL this message's research returned is
  // therefore not wandering; it is the only way to get that data.
  // research cannot read pages that render their data with JavaScript, and a user may
  // ask to act on a page research found. One browser_intent call per message is
  // therefore allowed after research; a second one is wandering.
  const researchIntentUsed = new Map<string, boolean>();
  const FOLLOW_UP_LOOKUPS = new Set(["crawl", "google_search", "fetch_image", "fetch_images", "research", "browser_intent"]);
  const USER_WANTS_MORE = /https?:\/\/|google|谷歌|crawl|爬|image|圖|照片|再查|再搜|多查|更多|再研究|深入|深度|deep|browser|瀏覽|打開|開啟|網站|網頁|點擊|點開|點進|點選/i;
  // The guard tells the agent to offer a deep crawl or more research; a bare yes to that
  // question must unlock follow-up lookups like an explicit request does.
  const AFFIRMATIVE = /^(好|好啊|好喔|好的|好呀|要|要啊|可以|可|嗯|對|是|行|麻煩|麻煩了|請|拜託|查|搜|ok|okay|yes|yep|sure|go|y)[\s!！。.~～，,]*$/i;
  const OFFERED_MORE_SEARCH = /查|搜|研究|深度|search|crawl|爬|look up|more|deep/i;
  // Asks for detail in so many words, on any turn.
  const WANTS_DEPTH = /詳細|詳情|細節|深入|深度|展開|仔細|完整介紹|多說|多講|說明一下|介紹一下|in depth|more detail|tell me more|elaborate/i;
  // Points back at something from the previous answer; only counts when there is one.
  const PICKS_ITEM = /<quoted_selection>|這個|這家|這間|這場|這款|這項|這什麼|那個|那家|那場|第\s*[一二三四五六七八九十\d]+\s*(?:個|項|點|家|間|場|款|天)|怎麼去|交通|票價|門票|價格|評價/i;
  // Image-generation skills legitimately need reference pages and images.
  const IMAGE_SKILL_PATH = /skills\/(create-image|qwen-image|photo-editing|create-gif|image-to-3d)\//;
  const IMAGE_TASK_LOOKUPS = new Set(["crawl", "fetch_image", "fetch_images", "browser_intent"]);
  const imageTask = new Map<string, boolean>();
  const userBody = (text: string) => text.replace(/^\[[^\]\n]*user:[^\]\n]*\]\s*/i, "").trim();
  const lastAssistantText = (ctx: {sessionManager?: {getBranch?: () => unknown[]}}): string => {
    const entries = ctx.sessionManager?.getBranch?.() ?? [];
    for (let i = entries.length - 1; i >= 0; i--) {
      const entry = entries[i] as {type?: string; message?: {role?: string; content?: unknown}};
      if (entry?.type !== "message") continue;
      if (entry.message?.role === "user") return "";
      if (entry.message?.role !== "assistant") continue;
      const content = entry.message.content;
      const text = typeof content === "string" ? content
        : Array.isArray(content) ? content.filter((c: {type?: string}) => c?.type === "text").map((c: {text?: string}) => c.text ?? "").join("\n") : "";
      if (text.trim()) return text.trim();
    }
    return "";
  };
  pi.on("input", (event, ctx) => {
    researchDone.set(sessionId(ctx), false);
    researchBlocks.set(sessionId(ctx), 0);
    researchTried.set(sessionId(ctx), false);
    googleCalls.set(sessionId(ctx), 0);
    googleFirstBlocked.set(sessionId(ctx), false);
    imageTask.set(sessionId(ctx), false);
    researchIntentUsed.set(sessionId(ctx), false);
    const text = typeof event.text === "string" ? event.text : "";
    let accepted = false;
    try {
      accepted = AFFIRMATIVE.test(userBody(text)) && OFFERED_MORE_SEARCH.test(lastAssistantText(ctx as any).slice(-600));
    } catch { /* branch unavailable: fall back to the literal text */ }
    lastInput.set(sessionId(ctx), accepted ? `${text}\n[accepted offer: 再查]` : text);
    let hadAnswer = false;
    try { hadAnswer = lastAssistantText(ctx as any).length > 0; } catch { /* no branch: treat as a new topic */ }
    const body = userBody(text);
    answerMode.set(sessionId(ctx), accepted || WANTS_DEPTH.test(body) || (hadAnswer && PICKS_ITEM.test(body)) ? "depth" : "breadth");
  });

  pi.on("tool_result", (event, ctx) => {
    if (event.toolName === "research") researchTried.set(sessionId(ctx), true);
    if (event.toolName === "research" && !event.isError) {
      researchDone.set(sessionId(ctx), true);
    }
  });

  // research is the default web lookup (AGENTS.md); with little thinking models
  // still reach for google_search/crawl first. Those are for explicit requests.
  // research runs its own Google query, so Google is never a research fallback:
  // google_search and browser google-search need the user to ask for Google.
  const USER_WANTS_GOOGLE = /google|谷歌|估狗/i;
  const USER_GAVE_URL_OR_CRAWL = /https?:\/\/|crawl|爬/i;
  pi.on("tool_call", (event, ctx) => {
    const asked = lastInput.get(sessionId(ctx)) ?? "";
    if (event.toolName === "read" && IMAGE_SKILL_PATH.test(String((event.input as {path?: unknown})?.path ?? "")))
      imageTask.set(sessionId(ctx), true);
    // After research ran (even if it failed) crawl is a legitimate fallback.
    if (!researchTried.get(sessionId(ctx)) &&
        event.toolName === "crawl" && crawlGuards && !USER_GAVE_URL_OR_CRAWL.test(asked)) {
      return {
        block: true,
        reason: "RESEARCH_FIRST: use the research tool for web lookups (four keyword queries q1-q4). crawl is only for URLs the user gave.",
      };
    }
    if (FOLLOW_UP_LOOKUPS.has(event.toolName) && (crawlGuards || event.toolName !== "crawl")
        && researchDone.get(sessionId(ctx))
        && !USER_WANTS_MORE.test(lastInput.get(sessionId(ctx)) ?? "")
        && !(imageTask.get(sessionId(ctx)) && IMAGE_TASK_LOOKUPS.has(event.toolName))) {
      if (event.toolName === "browser_intent" && !researchIntentUsed.get(sessionId(ctx))) {
        researchIntentUsed.set(sessionId(ctx), true);
        return;
      }
      const blocks = (researchBlocks.get(sessionId(ctx)) ?? 0) + 1;
      researchBlocks.set(sessionId(ctx), blocks);
      // Agents that keep retrying blocked lookups lose ~3.5 s per round; escalate.
      if (blocks > 1) return {
        block: true,
        reason: `RESEARCH_DONE_GUARD (blocked ${blocks} times): STOP calling tools. Your next message must be the final answer, written only from the research evidence you already have, ending with a question asking the user whether to deep-crawl specific sources or research more.`,
      };
      return {
        block: true,
        reason: `RESEARCH_DONE_GUARD: research already returned enough evidence for this message. Answer now from that evidence, cite its URLs and say plainly what it does not cover, then ask the user whether they want a deep crawl of specific sources or more research. ${crawlGuards ? "crawl, " : ""}google_search, fetch_image(s), browser/browser_intent and another research call are only allowed after the user asks for them.${crawlGuards ? "" : " research already crawled its result pages, so do not crawl on your own either."}`,
      };
    }
    const googleSearch = event.toolName === "google_search";
    // RESEARCH_DONE_GUARD above takes precedence once research has answered.
    if (GOOGLE_FIRST && googleSearch) {
      const calls = (googleCalls.get(sessionId(ctx)) ?? 0) + 1;
      googleCalls.set(sessionId(ctx), calls);
      if (calls > 1 && !USER_WANTS_GOOGLE.test(asked)) return {
        block: true,
        reason: "GOOGLE_DONE: google_search already ran for this message. Now call research with q1-q4 spread over the distinct items, names and sources its results turned up.",
      };
      return;
    }
    if (GOOGLE_FIRST && event.toolName === "research" && !googleCalls.get(sessionId(ctx)) && !googleFirstBlocked.get(sessionId(ctx))) {
      googleFirstBlocked.set(sessionId(ctx), true);
      return {
        block: true,
        reason: "GOOGLE_FIRST: start with google_search: 3-4 scattered directions (different sub-topics, synonyms, official / news / community / English sources) for a broad view. Then call research with queries picked from what Google found.",
      };
    }
    if (googleSearch && !USER_WANTS_GOOGLE.test(asked)) {
      return {
        block: true,
        reason: "RESEARCH_FIRST: google_search (and browser google-search) only runs when the user explicitly asks for a Google search. research already includes its own Google query, so use research for web lookups (four keyword queries q1-q4), or retry research if it failed.",
      };
    }
  });

  // The invoking agent writes the search queries itself (no planner model
  // call). Every extra token here is decoded on the critical path, so the
  // rule asks for keyword queries, not sentences.
  // q1 searches the subject exactly as the user wrote it: added keywords
  // (妖怪, 故事, English glosses) narrow or drift the results for rare names.
  const EXACT_Q1_RULE = "q1 is the exact search: the thing the user wants looked up, copied character for character (一字不差) from the user's message (for a follow-up on an earlier item: that item's name exactly as written), with nothing added, translated or reworded (user 「竹狐是什麼」 → q1 「竹狐」).";
  const RESEARCH_QUERY_RULE = GOOGLE_FIRST
    ? `research takes only q1, q2, q3, q4. ${EXACT_Q1_RULE} q2-q4: keyword queries, each 2-6 keywords (no sentences), relative dates replaced by concrete YYYY-MM-DD dates. Spread them wide: each targets a different item, name, venue or source that google_search turned up (or a sub-topic it missed), never rewordings of the user's question.`
    : `research takes only q1, q2, q3, q4. ${EXACT_Q1_RULE} q2-q4: distinct keyword queries, each 2-6 keywords (no sentences, no filler words), different angles (official source, exact figure, date, English), relative dates replaced by concrete YYYY-MM-DD dates.`;
  // Research queries are written by the invoking agent, which must resolve
  // 今天/明天/這週 to concrete dates (the planner used to get this line).
  const localDateLine = () => {
    const now = new Date();
    const day = new Intl.DateTimeFormat("en-CA", {year:"numeric",month:"2-digit",day:"2-digit"}).format(now);
    const weekday = "日一二三四五六"[now.getDay()];
    return `Today: ${day} (週${weekday}), timezone ${Intl.DateTimeFormat().resolvedOptions().timeZone}. Resolve relative dates (今天/明天/這週…) against this.`;
  };
  pi.on("before_agent_start", () => ({
    // Persist an appended context message. Never rewrite earlier date messages:
    // the system/tool prefix and existing conversation KV remain reusable.
    message: { customType: "browser-date-context", content: localDateLine(), display: false },
  }));

  const activeResearch = new Set<string>();
  const crawlConcurrency = Math.min(64, Math.max(1, Number(process.env.RESEARCH_CRAWL_CONCURRENCY) || 32));
  const crawlWordBudget = Math.min(100000, Math.max(1000, Number(process.env.RESEARCH_CRAWL_WORD_BUDGET) || 20000));
  const lastUserText = (ctx: {sessionManager?: {getBranch?: () => unknown[]}}): string | undefined => {
    const entries = ctx.sessionManager?.getBranch?.() ?? [];
    for (let i = entries.length - 1; i >= 0; i--) {
      const entry = entries[i] as {type?: string; message?: {role?: string; content?: unknown}};
      if (entry?.type !== "message" || entry.message?.role !== "user") continue;
      const content = entry.message.content;
      const text = typeof content === "string" ? content
        : Array.isArray(content) ? content.filter((c: {type?: string}) => c?.type === "text").map((c: {text?: string}) => c.text ?? "").join("\n") : "";
      if (text.trim()) return text.trim().slice(0, 4000);
    }
    return undefined;
  };
  // Prefill is owned by the Pi harness: its passive prefill warms the next
  // request from this tool's progress updates (the committed evidence prefix)
  // and pins the final request. This extension only reports committed text.
  // Speculative research: the job starts as soon as q1 of a research call is
  // written (search, crawl and evidence assembly need only the queries and
  // the user's question). q2..q4 join the running job as they are written.
  // execute() adopts the job; only the progress updates and the result need the
  // finished call. A call that never executes is cancelled.
  type Speculative = {jobId: string; sid: string; clock: ReturnType<typeof hostClock>; planner: ResearchPlanner;
    promise: Promise<Record<string, any>>; lastPrefix: string; listener?: (prefix: string) => void;
    abort: AbortController; adopted: boolean; timer: ReturnType<typeof setTimeout>};
  const speculative = new Map<string, Speculative>();
  const streamedArgs = new Map<string, {raw: string; sent: Set<number>}>();
  const dropSpeculative = (id: string, reason: string) => {
    const spec = speculative.get(id);
    if (!spec || spec.adopted) return;
    speculative.delete(id);
    clearTimeout(spec.timer);
    spec.abort.abort(new Error(reason));
    activeResearch.delete(spec.sid);
  };
  pi.on("message_update", (event: {assistantMessageEvent?: {type?: string; contentIndex?: number; delta?: string;
      partial?: {content?: Array<{type?: string; id?: string; name?: string}>}}}, ctx) => {
    const ev = event.assistantMessageEvent;
    if (!ev || (ev.type !== "toolcall_delta" && ev.type !== "toolcall_start" && ev.type !== "toolcall_end")) return;
    const block = ev.partial?.content?.[ev.contentIndex ?? -1];
    if (!block || block.type !== "toolCall" || block.name !== "research" || !block.id) return;
    const id = block.id;
    const sid = sessionId(ctx);
    const state = streamedArgs.get(id) ?? {raw: "", sent: new Set<number>()};
    streamedArgs.set(id, state);
    if (streamedArgs.size > 32) streamedArgs.delete(streamedArgs.keys().next().value as string);
    if (ev.type === "toolcall_delta" && typeof ev.delta === "string") state.raw += ev.delta;
    // Only complete string values (closing quote present) are used.
    for (const m of state.raw.matchAll(/"q([1-4])"\s*:\s*"((?:[^"\\]|\\.)*)"/g)) {
      const index = Number(m[1]) - 1;
      if (state.sent.has(index)) continue;
      let query: string;
      try { query = JSON.parse(`"${m[2]}"`); } catch { continue; }
      if (!query.trim() || query.length > 80) continue;
      query = currentYearQuery(query, lastUserText(ctx));
      state.sent.add(index);
      const spec = speculative.get(id);
      if (!spec) {
        if (activeResearch.has(sid)) continue;  // another research owns this session
        // RESEARCH_DONE_GUARD will block this call; do not search and crawl for it.
        if (researchDone.get(sid) && !USER_WANTS_MORE.test(lastInput.get(sid) ?? "")) continue;
        // GOOGLE_FIRST will block a research call written before google_search.
        if (GOOGLE_FIRST && !googleCalls.get(sid) && !googleFirstBlocked.get(sid)) continue;
        const jobId = randomUUID();
        const clock = hostClock();
        const abort = new AbortController();
        const created: Speculative = {jobId, sid, clock, planner: new ResearchPlanner(ctx, jobId, clock),
          promise: Promise.resolve({}), lastPrefix: "", abort, adopted: false,
          timer: setTimeout(() => dropSpeculative(id, "speculative_research_not_adopted"), 60_000)};
        created.promise = worker.research({jobId, question: lastUserText(ctx) ?? query, provider: "auto",
          searchBudget: 8, searchConcurrency: 4, layaConcurrency: 2, crawlWordBudget, rankBatchSize: 4,
          ranker: "none", crawlConcurrency, searchRounds: 1, queries: [query], streaming: true,
          evidence: "progressive", answer: answerMode.get(sid) ?? "breadth", clock}, sid, created.planner, abort.signal,
          (prefix) => { created.lastPrefix = prefix; created.listener?.(prefix); });
        created.promise.catch(() => {});
        speculative.set(id, created);
        activeResearch.add(sid);
        // A query written before q1 (unusual order) joins right away.
        for (const [other, q] of pendingEarly(state.raw, index)) {
          if (state.sent.has(other)) continue;
          state.sent.add(other);
          worker.addQuery(sid, {jobId, query: q, index: other}).catch(() => {});
        }
      } else {
        worker.addQuery(sid, {jobId: spec.jobId, query, index}).catch(() => {});
      }
    }
    const spec = speculative.get(id);
    if (ev.type === "toolcall_end" && spec) worker.addQuery(sid, {jobId: spec.jobId, done: true}).catch(() => {});
  });
  // Queries already complete when the job is created, other than the one that created it.
  const pendingEarly = (raw: string, creator: number): Array<[number, string]> => {
    const out: Array<[number, string]> = [];
    for (const m of raw.matchAll(/"q([1-4])"\s*:\s*"((?:[^"\\]|\\.)*)"/g)) {
      const index = Number(m[1]) - 1;
      if (index === creator) continue;
      try { out.push([index, JSON.parse(`"${m[2]}"`)]); } catch { /* skip */ }
    }
    return out;
  };
  // The call was not executed (other tool, no tool, error or abort): cancel.
  pi.on("message_end", (event: {message?: {role?: string; stopReason?: string; content?: Array<{type?: string; id?: string; name?: string}>}}) => {
    if (event.message?.role !== "assistant") return;
    const calls = new Set((event.message.content ?? []).filter((c) => c.type === "toolCall" && c.name === "research").map((c) => c.id));
    for (const id of [...speculative.keys()])
      if (!calls.has(id) || event.message.stopReason !== "toolUse") dropSpeculative(id, "speculative_research_not_called");
  });

  toolApi.registerTool({
    name: "research",
    label: "Budgeted research",
    description: GOOGLE_FIRST
      ? "Web research, step 2 of every web lookup (step 1 is google_search): runs your four keyword queries (3 fourget + 1 Google), resolves and deduplicates result URLs, crawls them in parallel in search order, and returns verbatim source-labelled evidence passages for your answer. You only choose what to search; everything else is fixed by the tool."
      : "Web research, the default and only web search tool unless the user explicitly asks for Google: runs your keyword queries (3 fourget + 1 Google, so Google results are already included), resolves and deduplicates result URLs, crawls them in parallel in search order, and returns verbatim source-labelled evidence passages for your answer. You only choose what to search; everything else is fixed by the tool.",
    promptSnippet: "Default web search: four keyword queries in, crawled source-labelled evidence out (searches, crawls and dates handled by the tool)",
    promptGuidelines: [
      ...(GOOGLE_FIRST ? [
        "Any question needing web or current information (events, news, prices, schedules, facts, docs): first ONE google_search for a broad view, then ONE research call whose queries are picked from the Google results. Never answer from the Google snippets alone.",
      ] : [
        "research is the default tool for any question needing web or current information (events, news, prices, schedules, facts, docs). Call it once, before any other search tool.",
        "research already runs its own Google query. Never add google_search (or browser google-search) to get Google results, to supplement research, or as a fallback when research fails or comes back thin; those are reserved for a user who explicitly says Google (e.g. 「用 Google 搜」).",
      ]),
      "For research queries, use the latest browser-date-context Today line for the local date; turn 今天/明天/這禮拜/週末 into concrete YYYY-MM-DD dates. Use gettime when exact current time is needed.",
      crawlGuards
        ? "Answer only from the evidence research returns and cite its source URLs; do not follow up with google_search or crawl unless the user asks."
        : "Answer from the evidence research returns and cite its source URLs. research already crawled its result pages, so do not crawl on your own afterwards, even when the evidence is thin: offer a deep crawl instead, and crawl only after the user asks for it or gives a URL. Do not follow up with google_search unless the user asks.",
      RESEARCH_QUERY_RULE,
      "How to write the answer comes with the evidence (its closing instructions); follow them. A new question is answered breadth first (every option, one or two lines each).",
      `When the user asks to go deeper on one item from an earlier answer (quotes it, says 這個／第N個／詳細), call ${GOOGLE_FIRST ? "google_search and research" : "research"} again with queries about that item (its official page, details, 交通、票價、評價) instead of answering from the earlier overview.`,
    ],
    // Four separate parameters, not one array: llama.cpp streams a tool call
    // one finished parameter at a time, so q1 reaches the extension (and its
    // search starts) while q2..q4 are still being written.
    parameters: Type.Object({
      q1: Type.String({ minLength: 1, maxLength: 80, description: "exact search: the thing asked about, copied verbatim (一字不差) from the user's message, no added words" }),
      q2: Type.String({ minLength: 1, maxLength: 80, description: "keyword query 2, a different angle" }),
      q3: Type.String({ minLength: 1, maxLength: 80, description: "keyword query 3, a different angle" }),
      q4: Type.String({ minLength: 1, maxLength: 80, description: "keyword query 4, a different angle" }),
    }),
    async execute(_toolCallId, params, signal, onUpdate, ctx) {
      const sid = sessionId(ctx);
      const spec = speculative.get(_toolCallId);
      if (spec) { spec.adopted = true; clearTimeout(spec.timer); }
      else if (activeResearch.has(sid)) throw new Error("research_session_busy");
      const jobId = spec?.jobId ?? randomUUID();
      const clock = spec?.clock ?? hostClock();
      const planner = spec?.planner ?? new ResearchPlanner(ctx, jobId, clock);
      // Committed evidence is appended once, in order; the final packet must
      // start with it or Pi drops the prefill (never affects correctness).
      const prefill = ACTIVE_PREFILL ? (ctx as {prefill?: PrefillApi}).prefill?.begin(_toolCallId) : undefined;
      // Evidence is ranked against what the user actually asked, taken from
      // the session instead of making the model copy it into the tool call.
      const asked = lastUserText(ctx);
      const queries = [params.q1, params.q2, params.q3, params.q4]
        .filter((q): q is string => typeof q === "string" && q.trim().length > 0)
        .map((q) => currentYearQuery(q, asked));
      const question = asked ?? queries.join(" ; ");
      let committed = "";
      let delivered = false;
      let sent = "";
      const onPrefix = (prefix: string) => {
        if (prefill) {
          if (!prefix.startsWith(committed)) { prefill.cancel("prefix_rewritten"); }
          else if (prefix.length > committed.length) {
            if (prefill.append(prefix.slice(committed.length))) committed = prefix;
          }
        }
        if (prefix.length <= sent.length) return;
        sent = prefix;
        onUpdate?.({content:[{type:"text",text:prefix}],details:{jobId,status:"evidence"}});
      };
      activeResearch.add(sid);
      try {
        // Status lives in details: update text must stay a prefix of the result.
        onUpdate?.({content:[],details:{jobId,status:"running"}});
        // The model only chooses queries; the tool owns every other knob.
        let response: Record<string, any>;
        if (spec) {
          // Adopt the job that started while the call was being written:
          // replay the evidence committed so far, then follow it live.
          spec.listener = onPrefix; if (spec.lastPrefix) onPrefix(spec.lastPrefix);
          signal?.addEventListener("abort", () => spec.abort.abort(), { once: true });
          response = await spec.promise;
        } else {
          response = await worker.research({jobId,question,provider:"auto",
            searchBudget:8,searchConcurrency:4,layaConcurrency:2,crawlWordBudget:crawlWordBudget,
            rankBatchSize:4,ranker:"none",crawlConcurrency,searchRounds:1,queries,
            evidence:"progressive",answer:answerMode.get(sid) ?? "breadth",clock},sid,planner,signal,
            onPrefix);
        }
        prefill?.end();
        if (response.action !== "research" || response.jobId !== jobId) throw new Error("research_invalid_terminal_frame");
        let text = response.text || "Research incomplete: no terminal evidence packet.";
        const usage = ctx.getContextUsage();
        // Coarse byte-based estimate, NOT tokenizer/model inference or a guarantee.
        // Keep half the remaining context plus 16k for sibling tools/prompt/output.
        const packetBytes = Buffer.byteLength(text);
        const estimatedInputTokens = Math.ceil(packetBytes / 2);
        const reserve = usage?.tokens == null || !Number.isFinite(usage.tokens) || !Number.isFinite(usage.contextWindow)
          ? 0 : Math.max(0,(usage.contextWindow-usage.tokens-16384)/2);
        if (response.fullEvidenceDelivered && (packetBytes>2*1024*1024 ||
            text.split("\n").length>20000 || estimatedInputTokens>reserve)) {
          response.status="incomplete"; response.reason="packet_too_large"; response.fullEvidenceDelivered=false;
          text="Research incomplete: packet_too_large (tool/context reserve). Full evidence was NOT delivered; immutable artifact preserved for recovery.";
        }
        // Restating the question after ~20k tokens of evidence keeps the answer on it.
        const asked = userBody(lastInput.get(sid) ?? "").replace(/\n\[accepted offer: 再查\]$/, "").slice(-600);
        if (response.fullEvidenceDelivered && asked) text += `\nThe user's question: «${asked}»\n` + (answerMode.get(sid) === "depth"
          ? "Answer it now, in depth: direct answer in the first sentence; then every concrete detail of the item asked about plus the 2-3 things the user needs next; "
          : "Answer it now, breadth first: direct answer in the first sentence; then every distinct on-topic item in one or two lines each (時間、地點、費用 or the key fact + 一句亮點), no deep dive into any single item; end by naming 1-3 items to expand and asking which one; ") +
          "a [來源](URL) link right after every key fact; nothing off-topic, nothing said twice. If the evidence is about a different product or company than the one asked, say so.\n";
        const {text: _packet,...details}=response;
        delivered = true;
        return {content:[{type:"text" as const,text}],details:{...details,estimatedInputTokens,
          plannerDiagnostics:planner.diagnostics,...(prefill ? {prefill:prefill.stats()} : {})},usage:planner.usage};
      } finally {
        activeResearch.delete(sid);
        speculative.delete(_toolCallId);
        if (!delivered) prefill?.cancel(signal?.aborted ? "aborted" : "research_failed");
      }
    },
  });

  toolApi.registerTool({
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

  toolApi.registerTool({
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

  toolApi.registerTool({
    name: "google_search",
    label: "Directional Google Search",
    description: GOOGLE_FIRST
      ? "Step 1 of every web lookup: a breadth-first Google search through Nodriver, one to four distinct query directions in parallel, returning a globally de-duplicated, diversity-balanced Top 10 (title/URL/snippet). Use what it finds to write the research queries (step 2)."
      : "EXPLICIT USER REQUEST ONLY: call this only when the user's own message explicitly asks for a Google search (e.g. 「用 Google 搜」, \"google it\"). For every other web lookup use research, which already includes a Google query. Searches Google directly through Nodriver with one to four distinct query directions in parallel and returns a globally de-duplicated, diversity-balanced Top 10 in the same title/URL/snippet format as web_search.",
    promptSnippet: "Explicit user Google requests only (otherwise use research): run 1–4 directional Google searches in parallel and return a de-duplicated Top 10",
    promptGuidelines: GOOGLE_FIRST ? [
      "google_search is the first step of every web lookup: call it once, then call research. Never answer from Google snippets alone and never call google_search twice for one message.",
      "Search breadth first with scattered keywords: three or four directions that each cover a different sub-topic, synonym or source type (official site, news, community such as PTT/Dcard/blogs, English or regional sources), 2-5 keywords each. Do not send near-duplicate queries.",
      "Use concrete dates or the current year (from the browser-date-context Today line) when time matters; never default to 2025 from model memory.",
      "After google_search, call research with q1-q4 spread over the distinct items, names, venues and sources the results turned up (plus any sub-topic Google missed).",
    ] : [
      "google_search is ONLY for a user who explicitly asks for a Google search in their message. Do not use it on your own initiative, to supplement research, or as a fallback when research fails or is thin: research already runs its own Google query, so call or retry research instead. Calls without an explicit Google request are blocked (RESEARCH_FIRST).",
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
        if (GOOGLE_FIRST && (suppliedTimestamp === undefined || Math.abs(Date.now() - suppliedTimestamp) > FRESH_TIME_MAX_AGE_MS)) {
          // The step-1 search runs on every lookup; the date context line already gives today.
          confirmedCurrentTime = new Date().toISOString();
        } else if (suppliedTimestamp === undefined || Math.abs(Date.now() - suppliedTimestamp) > FRESH_TIME_MAX_AGE_MS) {
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

  toolApi.registerTool({
    name: "crawl",
    label: "Parallel Browser Crawl",
    description: "Crawl one or more web pages or PDF files in parallel in the shared Chrome. HTML pages return their text plus ranked image candidates; PDFs return their text. Only crawl exact URLs the user gave or a search returned; never guess URLs.",
    promptSnippet: "Crawl web pages or PDFs in parallel, extracting text and embedded images",
    promptGuidelines: [
      "Use crawl when you need the full content of one or multiple web pages or direct PDF URLs.",
      "Never guess a crawl URL. Use research first (google_search only when the user explicitly asked for Google), then pass only exact URLs returned by search results or supplied by the user.",
      "Reach for it right after web_search whenever the snippets are too thin to answer from: crawl the promising result URLs rather than guessing at what the pages say.",
      "Not after research: research already crawled its result pages. Answer from its evidence and offer a deep crawl; crawl those sources only after the user asks for it.",
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

  // browser_intent's window (session `intent-<id>`) is deliberately left open: piweb and
  // piscord end the Pi process after each Life turn or 10 idle minutes, and the next turn
  // of the same conversation (a login code, "open the first result") must find its page.
  // The worker closes windows idle for 30 minutes.
  pi.on("session_shutdown", async (_event, ctx) => {
    const currentSessionId = ctx.sessionManager.getSessionId();
    try {
      await worker.cleanupSession(currentSessionId);
    } finally {
      worker.disconnect();
    }
  });
}
