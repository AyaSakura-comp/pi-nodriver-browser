import { randomUUID } from "node:crypto";
import { existsSync, readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { DEFAULT_MAX_BYTES, DEFAULT_MAX_LINES, formatSize, truncateHead, type ExtensionAPI, type ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import registerBrowserIntent from "./browser-intent-tool.ts";
import { routeResearch, ROUTER_THRESHOLD } from "./research-router.ts";
import { type ResearchParams, SOCKET, WorkerClient, startWorker } from "./worker-launch.ts";

// Research first (see before_agent_start): on unless RESEARCH_FIRST=0. Its latency budget: deliver
// the evidence this long after the job starts, at most this many rough tokens, no wait for images.
const RESEARCH_FIRST = process.env.RESEARCH_FIRST === "1";   // off: the user dropped the LFM router approach (2026-11-08)
const isLocalModel = (baseUrl: string | undefined) => {
  try {
    return ["localhost", "127.0.0.1", "[::1]", "::1"].includes(new URL(baseUrl ?? "").hostname);
  } catch {
    return false;
  }
};
const PREFILL_STOP_BEFORE_MS = 500;
const RESEARCH_FIRST_BUDGET = {
  deadlineMs: Number(process.env.RESEARCH_FIRST_DEADLINE_MS ?? 2700),
  evidenceBudget: Number(process.env.RESEARCH_FIRST_EVIDENCE ?? 1500),
  imageWaitMs: 0,
};

const MAX_BATCH_IMAGE_BYTES = 40 * 1024 * 1024;
const TIME_SENSITIVE_SEARCH_PATTERN = /(?:\b(?:19|20)\d{2}\b|\b(?:today|tomorrow|yesterday|now|current|currently|latest|recent|recently|upcoming|ago|date|time|timezone|schedule|deadline|release|price|stock|availability|exchange\s+rate|weather)\b|\bthis\s+(?:week|month|year)\b|\b(?:last|next)\s+(?:week|month|year)\b|今天|明天|昨天|現在|當下|目前|最新|最近|即將|本週|這週|上週|下週|本月|上月|下月|今年|去年|明年|日期|時間|時區|時程|排程|截止|發布|上市|價格|庫存|供貨|匯率|天氣|活動)/iu;
const FRESH_TIME_MAX_AGE_MS = 10 * 60 * 1000;

/** Milliseconds of a gettime(action: now) value such as "2026-08-30 15:52:06 +0800 CST". */
function parseGettimeValue(value: string): number | undefined {
  const match = value.trim().match(/^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\s+([+-]\d{2})(\d{2})(?:\s+\S+)?$/);
  if (!match) return undefined;
  const timestamp = Date.parse(`${match[1]}T${match[2]}${match[3]}:${match[4]}`);
  return Number.isFinite(timestamp) ? timestamp : undefined;
}

// Follow-up lookups after research, and when a user message unlocks them.
const FOLLOW_UP_LOOKUPS = new Set(["crawl", "google_search", "fetch_image", "fetch_images", "research", "browser_intent"]);
const USER_WANTS_MORE = /https?:\/\/|google|谷歌|crawl|爬|image|圖|照片|再查|再搜|多查|更多|再研究|深入|深度|deep|browser|瀏覽|打開|開啟|網站|網頁|點擊|點開|點進|點選/i;
const USER_WANTS_GOOGLE = /google|谷歌|估狗/i;
const USER_GAVE_URL_OR_CRAWL = /https?:\/\/|crawl|爬/i;
// The guard tells the agent to offer a deep crawl or more research; a bare yes to that
// question must unlock follow-up lookups like an explicit request does.
const AFFIRMATIVE = /^(好|好啊|好喔|好的|好呀|要|要啊|可以|可|嗯|對|是|行|麻煩|麻煩了|請|拜託|查|搜|ok|okay|yes|yep|sure|go|y)[\s!！。.~～，,]*$/i;
const OFFERED_MORE_SEARCH = /查|搜|研究|深度|search|crawl|爬|look up|more|deep/i;
// Image-generation skills legitimately need reference pages and images.
const IMAGE_SKILL_PATH = /skills\/(create-image|qwen-image|photo-editing|create-gif|image-to-3d)\//;
const IMAGE_TASK_LOOKUPS = new Set(["crawl", "fetch_image", "fetch_images", "browser_intent"]);
// Research answers are breadth first (every option, one or two lines each) until the user
// asks for detail in so many words, on any turn, or points back at an item of the previous answer.
const WANTS_DEPTH = /詳細|詳情|細節|深入|深度|展開|仔細|完整介紹|多說|多講|說明一下|介紹一下|in depth|more detail|tell me more|elaborate/i;
const PICKS_ITEM = /<quoted_selection>|這個|這家|這間|這場|這款|這項|這什麼|那個|那家|那場|第\s*[一二三四五六七八九十\d]+\s*(?:個|項|點|家|間|場|款|天)|怎麼去|交通|票價|門票|價格|評價/i;

// Models (especially with little thinking) often write last year into queries for
// "tomorrow"/"this week"; stale years return last year's pages. A year in the past five
// years is moved to the current year unless the user named that year or asked about the past.
const PAST_INTENT = /去年|前年|上一年|往年|歷年|過去|歷史|以前|當年|last year|previous year|history|historical/i;
function currentYearQuery(query: string, asked: string): string {
  if (PAST_INTENT.test(asked)) return query;
  const year = new Date().getFullYear();
  return query.replace(/(?<!\d)(20\d{2})(?!\d)/g, (match) => {
    const value = Number(match);
    return value < year && value >= year - 5 && !asked.includes(match) ? String(year) : match;
  });
}

const hostClock = () => ({ iso: new Date().toISOString(), timezone: Intl.DateTimeFormat().resolvedOptions().timeZone });

// The agent writes the research queries and must resolve 今天/明天/這週 to concrete dates.
function localDateLine(): string {
  const now = new Date();
  const day = new Intl.DateTimeFormat("en-CA", { year: "numeric", month: "2-digit", day: "2-digit" }).format(now);
  return `Today: ${day} (週${"日一二三四五六"[now.getDay()]}), timezone ${hostClock().timezone}. Resolve relative dates (今天/明天/這週…) against this.`;
}

/** The message without the transport's "[Discord user: name]" prefix. */
const userBody = (text: string) => text.replace(/^\[[^\]\n]*user:[^\]\n]*\]\s*/i, "").trim();

const textOf = (content: unknown): string => (typeof content === "string" ? content
  : Array.isArray(content) ? content.filter((c) => c?.type === "text").map((c) => c.text ?? "").join("\n") : "").trim();

/** The latest assistant text since the last user message ("" when there is none). */
function lastAssistantText(ctx: ExtensionContext): string {
  const entries = ctx.sessionManager.getBranch();
  for (let i = entries.length - 1; i >= 0; i--) {
    const entry = entries[i];
    if (entry.type !== "message") continue;
    if (entry.message.role === "user") return "";
    const text = entry.message.role === "assistant" ? textOf(entry.message.content) : "";
    if (text) return text;
  }
  return "";
}

/** `"crawlGuards": false` in browser-config.json exempts crawl from RESEARCH_FIRST and RESEARCH_DONE_GUARD.
 * A missing or unreadable file keeps the guards on. */
function crawlGuardsSetting(): boolean {
  const path = join(process.env.PI_AGENT_DIR || join(homedir(), ".pi", "agent"), "browser-config.json");
  try {
    return !existsSync(path) || JSON.parse(readFileSync(path, "utf8")).crawlGuards !== false;
  } catch {
    return true;
  }
}

function truncated(text: string): { output: string; truncated: boolean } {
  const truncation = truncateHead(text, { maxLines: DEFAULT_MAX_LINES, maxBytes: DEFAULT_MAX_BYTES });
  return {
    output: truncation.truncated
      ? `${truncation.content}\n\n[Output truncated: ${truncation.outputLines}/${truncation.totalLines} lines, ${formatSize(truncation.outputBytes)}/${formatSize(truncation.totalBytes)}.]`
      : truncation.content,
    truncated: truncation.truncated,
  };
}

/** What one user message allows. Every new message (`input`) starts a new turn. */
interface Turn {
  asked: string;        // the message, without the transport prefix
  more: boolean;        // asks for more lookups, or a bare yes to the offer to search more
  depth: boolean;       // research answers this message in depth
  researched: boolean;  // research ran (even if it failed): crawl is a legitimate fallback
  answered: boolean;    // research returned evidence: follow-up lookups are guarded
  intentUsed: boolean;  // the one browser_intent call allowed after research
  imageTask: boolean;   // an image-generation skill is running
  blocks: number;       // RESEARCH_DONE_GUARD blocks so far
}

/** A research job; one per session. A job started while its call was still being written waits for execute(). */
interface Job {
  id: string;
  callId: string;
  done: Promise<Record<string, any>>;
  prefix: string;       // evidence committed so far
  listener?: (prefix: string) => void;
  abort: AbortController;
  adopted: boolean;
  timer?: ReturnType<typeof setTimeout>;
  /** A latency-budget job: when its evidence is delivered at the latest (epoch ms). */
  deadlineAt?: number;
}

// Browser and search guidance belongs to the tools, not Pi's system prompt: promptGuidelines
// are folded into the tool description (the tool schema), so no browser rule is rendered into
// the system prompt.
function guidanceInDescription<T extends { description: string; promptGuidelines?: string[] }>(tool: T) {
  const { promptGuidelines, ...rest } = tool;
  return { ...rest, description: `${tool.description}\n\nUsage rules:\n${(promptGuidelines ?? []).map((rule) => `- ${rule}`).join("\n")}` };
}

export default function (pi: ExtensionAPI, worker: WorkerClient = new WorkerClient(SOCKET, startWorker)) {
  const toolApi: ExtensionAPI = { ...pi, registerTool: (tool) => pi.registerTool(guidanceInDescription(tool)) };
  const crawlGuards = crawlGuardsSetting();
  // Kill switch for the research prompt prefill (on by default).
  const ACTIVE_PREFILL = process.env.RESEARCH_ACTIVE_PREFILL !== "0";
  registerBrowserIntent(toolApi, worker);
  const sessionId = (ctx: ExtensionContext) => ctx.sessionManager.getSessionId();

  // research returns the complete evidence for one user message. Agents still wandered off
  // to crawl / fetch more afterwards (minimal thinking ignores the prompt rule), so follow-up
  // lookups are blocked until the next user message unless that message asks for them.
  const turns = new Map<string, Turn>();
  const newTurn = (asked = "", more = false, depth = false): Turn =>
    ({ asked, more, depth, researched: false, answered: false, intentUsed: false, imageTask: false, blocks: 0 });
  const turnOf = (sid: string) => turns.get(sid) ?? turns.set(sid, newTurn()).get(sid)!;
  const guarded = (turn: Turn, tool: string) => FOLLOW_UP_LOOKUPS.has(tool) && (crawlGuards || tool !== "crawl")
    && turn.answered && !turn.more && !(turn.imageTask && IMAGE_TASK_LOOKUPS.has(tool));

  pi.on("input", (event, ctx) => {
    const asked = userBody(event.text);
    const previous = lastAssistantText(ctx);
    const accepted = AFFIRMATIVE.test(asked) && OFFERED_MORE_SEARCH.test(previous.slice(-600));
    turns.set(sessionId(ctx), newTurn(asked, accepted || USER_WANTS_MORE.test(asked),
      accepted || WANTS_DEPTH.test(asked) || (previous !== "" && PICKS_ITEM.test(asked))));
  });

  pi.on("tool_result", (event, ctx) => {
    if (event.toolName !== "research") return;
    const turn = turnOf(sessionId(ctx));
    turn.researched = true;
    if (!event.isError) turn.answered = true;
  });

  // research is the default web lookup; with little thinking models still reach for
  // google_search/crawl first. research runs its own Google query, so Google is never a
  // research fallback: google_search needs the user to ask for Google.
  pi.on("tool_call", (event, ctx) => {
    const turn = turnOf(sessionId(ctx));
    const tool = event.toolName;
    if (tool === "read" && IMAGE_SKILL_PATH.test(String((event.input as { path?: unknown }).path ?? ""))) turn.imageTask = true;
    if (tool === "crawl" && crawlGuards && !turn.researched && !USER_GAVE_URL_OR_CRAWL.test(turn.asked)) {
      return { block: true, reason: "RESEARCH_FIRST: use the research tool for web lookups (four keyword queries q1-q4). crawl is only for URLs the user gave." };
    }
    if (guarded(turn, tool)) {
      // research cannot read pages that render their data with JavaScript, and a user may ask to
      // act on a page research found: one browser_intent call per message is allowed after research.
      if (tool === "browser_intent" && !turn.intentUsed) {
        turn.intentUsed = true;
        return;
      }
      // Agents that keep retrying blocked lookups lose ~3.5 s per round; escalate.
      if (++turn.blocks > 1) return {
        block: true,
        reason: `RESEARCH_DONE_GUARD (blocked ${turn.blocks} times): STOP calling tools. Your next message must be the final answer, written only from the research evidence you already have, ending with a question asking the user whether to deep-crawl specific sources or research more.`,
      };
      return {
        block: true,
        reason: `RESEARCH_DONE_GUARD: research already returned enough evidence for this message. Answer now from that evidence, cite its URLs and say plainly what it does not cover, then ask the user whether they want a deep crawl of specific sources or more research. ${crawlGuards ? "crawl, " : ""}google_search, fetch_image(s), browser/browser_intent and another research call are only allowed after the user asks for them.${crawlGuards ? "" : " research already crawled its result pages, so do not crawl on your own either."}`,
      };
    }
    if (tool === "google_search" && !USER_WANTS_GOOGLE.test(turn.asked)) {
      return {
        block: true,
        reason: "RESEARCH_FIRST: google_search (and browser google-search) only runs when the user explicitly asks for a Google search. research already includes its own Google query, so use research for web lookups (four keyword queries q1-q4), or retry research if it failed.",
      };
    }
  });

  // Research starts the moment q1 of a research call is written: search, crawl and evidence
  // need only the queries and the user's question. q2..q4 join the running job as they are
  // written, and execute() adopts it. A job whose call is never executed is cancelled.
  const jobs = new Map<string, Job>();
  const startResearch = (sid: string, callId: string, queries: string[], streaming: boolean,
    budget: Pick<ResearchParams, "deadlineMs" | "evidenceBudget" | "imageWaitMs"> = {}): Job => {
    const turn = turnOf(sid);
    const job: Job = { id: randomUUID(), callId, done: Promise.resolve({}), prefix: "", abort: new AbortController(), adopted: false,
      ...(budget.deadlineMs && { deadlineAt: Date.now() + budget.deadlineMs }) };
    job.done = worker.research({
      jobId: job.id, question: turn.asked.slice(0, 4000) || queries.join(" ; "), queries, clock: hostClock(),
      answer: turn.depth ? "depth" : "breadth", crawlConcurrency: 32, crawlWordBudget: 20000, ...(streaming && { streaming }),
      ...budget,
    }, sid, job.abort.signal, (prefix) => {
      job.prefix = prefix;
      job.listener?.(prefix);
    });
    job.done.catch(() => {});
    jobs.set(sid, job);
    return job;
  };
  const dropJob = (sid: string) => {
    const job = jobs.get(sid);
    if (!job || job.adopted) return;
    jobs.delete(sid);
    clearTimeout(job.timer);
    job.abort.abort();
  };

  // Research first. Deciding to research is otherwise the agent's first turn: reading the new
  // message, thinking and writing the call took 3.4-5.1 s before the first search (2026-11-08,
  // Strata), and the evidence could only be prefilled after it. A small local model (the router)
  // takes that decision in ~0.5 s: when it gives research a probability of at least
  // RESEARCH_ROUTER_THRESHOLD, the job starts at once with the user's words as q1 and the turn
  // starts with that research call (before_agent_start toolCall), so the agent's first model call
  // is the answer, with the evidence prefilled while it arrived. A latency budget keeps the
  // evidence small enough to be read before the answer. Anything else (router down, below the
  // threshold, an image, a running job) keeps the agent's own decision.
  pi.on("before_agent_start", async (event, ctx) => {
    // Persist an appended context message. Never rewrite earlier date messages:
    // the system/tool prefix and existing conversation KV remain reusable.
    const date = { customType: "browser-date-context", content: localDateLine(), display: false };
    // Only for a model on this machine: its prompt reading is what the latency budget is for, and a cloud
    // model is better served by the full evidence of the agent's own research call.
    if (!RESEARCH_FIRST || !ctx || event.images?.length || !isLocalModel(ctx.model?.baseUrl)) return { message: date };
    const sid = sessionId(ctx);
    const turn = turnOf(sid);
    if (jobs.has(sid) || !turn.asked || guarded(turn, "research")) return { message: date };
    const routed = await routeResearch(turn.asked, localDateLine()).catch(() => undefined);
    if (!routed || routed.p < ROUTER_THRESHOLD) return { message: date };
    // Strata saves the conversation it holds before another one replaces it (~0.25 s); asked now, it does so
    // while the queries are written instead of in front of the first prefill. Other servers refuse it harmlessly.
    void fetch(`${ctx.model!.baseUrl.replace(/\/+$/, "").replace(/\/v1$/, "")}/slots/0?action=flush`,
      { method: "POST", headers: { "content-type": "application/json" }, body: "{}", signal: AbortSignal.timeout(5000) })
      .catch(() => {});
    const callId = `call_${randomUUID().replaceAll("-", "")}`;
    const q1 = turn.asked.replace(/\s+/g, " ").trim().slice(0, 80);
    // q1 (the user's words) searches at once; q2-q4, the router's keyword queries, join as soon as it has
    // written them (~0.3 s later). A research call is four queries: without three more, the agent writes its own.
    const job = startResearch(sid, callId, [currentYearQuery(q1, turn.asked)], true, RESEARCH_FIRST_BUDGET);
    job.timer = setTimeout(() => dropJob(sid), 60_000);
    const extra = [...new Set((await routed.queries.catch(() => [] as string[]))
      .map((q) => q.replace(/\s+/g, " ").trim()).filter((q) => q && q.length <= 80 && q !== q1))].slice(0, 3);
    if (extra.length < 3) {
      dropJob(sid);
      return { message: date };
    }
    extra.forEach((query, i) => worker.addQuery(sid, { jobId: job.id, query: currentYearQuery(query, turn.asked), index: i + 1 }).catch(() => {}));
    worker.addQuery(sid, { jobId: job.id, done: true }).catch(() => {});
    const [q2, q3, q4] = extra;
    return { message: date, toolCall: { id: callId, name: "research", arguments: { q1, q2, q3, q4 } } };
  });
  const streamed = new Map<string, { raw: string; sent: Set<number> }>();
  pi.on("message_update", (event, ctx) => {
    const ev = event.assistantMessageEvent;
    if (ev.type !== "toolcall_delta" && ev.type !== "toolcall_end") return;
    const block = ev.partial.content[ev.contentIndex];
    if (block?.type !== "toolCall" || block.name !== "research") return;
    const sid = sessionId(ctx);
    const turn = turnOf(sid);
    let call = streamed.get(block.id);
    if (!call) {
      streamed.set(block.id, call = { raw: "", sent: new Set() });
      if (streamed.size > 32) streamed.delete(streamed.keys().next().value!);
    }
    if (ev.type === "toolcall_delta") call.raw += ev.delta;
    // Only complete string values (closing quote present) are used.
    for (const [, n, literal] of call.raw.matchAll(/"q([1-4])"\s*:\s*"((?:[^"\\]|\\.)*)"/g)) {
      const index = Number(n) - 1;
      if (call.sent.has(index)) continue;
      let query: string;
      try {
        query = JSON.parse(`"${literal}"`);
      } catch {
        continue;
      }
      if (!query.trim() || query.length > 80) continue;
      query = currentYearQuery(query, turn.asked);
      const job = jobs.get(sid);
      if (job?.callId === block.id) {
        worker.addQuery(sid, { jobId: job.id, query, index }).catch(() => {});
      } else {
        // Another research owns the session, or RESEARCH_DONE_GUARD will block this call.
        if (job || guarded(turn, "research")) continue;
        startResearch(sid, block.id, [query], true).timer = setTimeout(() => dropJob(sid), 60_000);
      }
      call.sent.add(index);
    }
    const job = jobs.get(sid);
    if (ev.type === "toolcall_end" && job?.callId === block.id) worker.addQuery(sid, { jobId: job.id, done: true }).catch(() => {});
  });
  // The call was not executed (other tool, no tool, error or abort): cancel its job.
  pi.on("message_end", (event, ctx) => {
    const message = event.message;
    if (message.role !== "assistant") return;
    const job = jobs.get(sessionId(ctx));
    if (job && (message.stopReason !== "toolUse" || !message.content.some((c) => c.type === "toolCall" && c.id === job.callId))) {
      dropJob(sessionId(ctx));
    }
  });

  // The invoking agent writes the search queries itself. Every extra token here is decoded on
  // the critical path, so the rule asks for keyword queries, not sentences. q1 searches the
  // subject exactly as the user wrote it: added keywords (妖怪, 故事, English glosses) narrow or
  // drift the results for rare names.
  const EXACT_Q1_RULE = "q1 is the exact search: the thing the user wants looked up, copied character for character (一字不差) from the user's message (for a follow-up on an earlier item: that item's name exactly as written), with nothing added, translated or reworded (user 「竹狐是什麼」 → q1 「竹狐」).";
  toolApi.registerTool({
    name: "research",
    label: "Budgeted research",
    description: "Web research, the default and only web search tool unless the user explicitly asks for Google: runs your keyword queries (3 fourget + 1 Google, so Google results are already included), resolves and deduplicates result URLs, crawls them in parallel in search order, and returns verbatim source-labelled evidence passages for your answer. You only choose what to search; everything else is fixed by the tool.",
    promptGuidelines: [
      "research is the default tool for any question needing web or current information (events, news, prices, schedules, facts, docs). Call it once, before any other search tool.",
      "research already runs its own Google query. Never add google_search (or browser google-search) to get Google results, to supplement research, or as a fallback when research fails or comes back thin; those are reserved for a user who explicitly says Google (e.g. 「用 Google 搜」).",
      "For research queries, use the latest browser-date-context Today line for the local date; turn 今天/明天/這禮拜/週末 into concrete YYYY-MM-DD dates. Use gettime when exact current time is needed.",
      crawlGuards
        ? "Answer only from the evidence research returns and cite its source URLs; do not follow up with google_search or crawl unless the user asks."
        : "Answer from the evidence research returns and cite its source URLs. research already crawled its result pages, so do not crawl on your own afterwards, even when the evidence is thin: offer a deep crawl instead, and crawl only after the user asks for it or gives a URL. Do not follow up with google_search unless the user asks.",
      `research takes only q1, q2, q3, q4. ${EXACT_Q1_RULE} q2-q4: distinct keyword queries, each 2-6 keywords (no sentences, no filler words), different angles (official source, exact figure, date, English), relative dates replaced by concrete YYYY-MM-DD dates.`,
      "How to write the answer comes with the evidence (its closing instructions); follow them. A new question is answered breadth first (every option, one or two lines each).",
      "When the user asks to go deeper on one item from an earlier answer (quotes it, says 這個／第N個／詳細), call research again with queries about that item (its official page, details, 交通、票價、評價) instead of answering from the earlier overview.",
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
    async execute(callId, params, signal, onUpdate, ctx) {
      const sid = sessionId(ctx);
      let job = jobs.get(sid);
      if (job && job.callId !== callId) throw new Error("research_session_busy");
      // Committed evidence is appended once, in order; the final packet must start with it
      // or Pi drops the prefill (never affects correctness).
      const prefill = ACTIVE_PREFILL ? ctx.prefill?.begin(callId) : undefined;
      // Near a latency budget's deadline no new prefill starts: one started then would still be reading when the
      // evidence is delivered, and the answer waits for it and pays a second read. What arrives after this is read
      // by the answer itself, in the same pass as the rest. (The evidence delivered is the same either way.)
      const stopPrefill = job?.deadlineAt && prefill
        ? setTimeout(() => prefill.end(), Math.max(0, job.deadlineAt - PREFILL_STOP_BEFORE_MS - Date.now())) : undefined;
      let committed = "";
      let shown = "";
      let delivered = false;
      const onPrefix = (prefix: string) => {
        if (prefill) {
          if (!prefix.startsWith(committed)) prefill.cancel("prefix_rewritten");
          else if (prefix.length > committed.length && prefill.append(prefix.slice(committed.length))) committed = prefix;
        }
        if (prefix.length <= shown.length) return;
        shown = prefix;
        onUpdate?.({ content: [{ type: "text", text: prefix }], details: { jobId: job!.id, status: "evidence" } });
      };
      try {
        const asked = turnOf(sid).asked;
        job ??= startResearch(sid, callId, [params.q1, params.q2, params.q3, params.q4]
          .filter((q) => q.trim()).map((q) => currentYearQuery(q, asked)), false);
        job.adopted = true;
        clearTimeout(job.timer);
        // Status lives in details: update text must stay a prefix of the result.
        onUpdate?.({ content: [], details: { jobId: job.id, status: "running" } });
        // A job started while the call was written: replay its evidence so far, then follow it live.
        job.listener = onPrefix;
        if (job.prefix) onPrefix(job.prefix);
        const cancel = job.abort;
        if (signal?.aborted) cancel.abort();
        signal?.addEventListener("abort", () => cancel.abort(), { once: true });
        const response = await job.done;
        prefill?.end();
        let text: string = response.text || "Research incomplete: no terminal evidence packet.";
        // Coarse byte-based estimate, NOT tokenizer/model inference or a guarantee.
        // Keep half the remaining context plus 16k for sibling tools/prompt/output.
        const usage = ctx.getContextUsage();
        const packetBytes = Buffer.byteLength(text);
        const estimatedInputTokens = Math.ceil(packetBytes / 2);
        const room = usage?.tokens != null ? (usage.contextWindow - usage.tokens - 16384) / 2 : NaN;
        if (response.fullEvidenceDelivered && (packetBytes > 2 * 1024 * 1024 || text.split("\n").length > 20000 ||
            estimatedInputTokens > (Number.isFinite(room) ? Math.max(0, room) : 0))) {
          response.status = "incomplete";
          response.reason = "packet_too_large";
          response.fullEvidenceDelivered = false;
          text = "Research incomplete: packet_too_large (tool/context reserve). Full evidence was NOT delivered; immutable artifact preserved for recovery.";
        }
        // Restating the question after ~20k tokens of evidence keeps the answer on it.
        const turn = turnOf(sid);
        // A research-first packet opens with the question and its answer rules (prefilled early).
        if (response.fullEvidenceDelivered && turn.asked && !response.rulesFirst) text += `\nThe user's question: «${turn.asked.slice(-600)}»\n` + (turn.depth
          ? "Answer it now, in depth: direct answer in the first sentence; then every concrete detail of the item asked about plus the 2-3 things the user needs next; "
          : "Answer it now, breadth first: direct answer in the first sentence; then every distinct on-topic item in one or two lines each (時間、地點、費用 or the key fact + 一句亮點), no deep dive into any single item; end by naming 1-3 items to expand and asking which one; ") +
          "a [來源](URL) link right after every key fact; nothing off-topic, nothing said twice. If the evidence is about a different product or company than the one asked, say so.\n";
        const { text: _packet, ...details } = response;
        delivered = true;
        return { content: [{ type: "text" as const, text }], details: { ...details, estimatedInputTokens, ...(prefill && { prefill: prefill.stats() }) } };
      } finally {
        if (jobs.get(sid) === job) jobs.delete(sid);
        clearTimeout(stopPrefill);
        if (!delivered) prefill?.cancel(signal?.aborted ? "aborted" : "research_failed");
      }
    },
  });

  toolApi.registerTool({
    name: "fetch_image",
    label: "Fetch Image",
    description: "Fetch and validate one direct HTTP/HTTPS image URL discovered by web_search, crawl, or browser. Returns the image inline plus a local path that can be sent to PiWeb or Discord.",
    promptGuidelines: [
      "Use fetch_image after web_search, crawl, or browser when the user asks to see or receive a discovered image.",
      "Pass fetch_image a direct HTTP/HTTPS image URL, not an article, gallery, search-results, or HTML page URL.",
      "After fetch_image succeeds, include the exact '[[image: <path>]]' marker it returns in the final reply so PiWeb or Discord actually receives the image.",
    ],
    parameters: Type.Object({
      url: Type.String({ description: "Direct HTTP/HTTPS image URL discovered by web_search, crawl, or browser" }),
    }),
    async execute(_callId, params, signal, _onUpdate, ctx) {
      const response = await worker.request(`fetch-image ${JSON.stringify(params.url)}`, sessionId(ctx), signal);
      const marker = `[[image: ${response.imagePath}]]`;
      const fetched = response.text || "Image fetched";
      return {
        content: [
          { type: "text" as const, text: fetched.includes(marker) ? fetched : `${fetched}\nSend it to the user with exactly: ${marker}` },
          { type: "image" as const, data: readFileSync(response.imagePath).toString("base64"), mimeType: response.mimeType || "image/png" },
        ],
        details: response,
      };
    },
  });

  toolApi.registerTool({
    name: "fetch_images",
    label: "Fetch Images in Parallel",
    description: "Securely fetch and validate up to four direct HTTP/HTTPS image URLs in parallel. Returns exact PiWeb/Discord delivery markers without injecting image bytes into the next model turn.",
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
    async execute(_callId, params, signal, _onUpdate, ctx) {
      const urls = [...new Set(params.urls.map((url) => url.trim()).filter(Boolean))].slice(0, 4);
      if (urls.length === 0) throw new Error("fetch_images requires at least one direct image URL");
      const settled = await Promise.allSettled(urls.map((url) => worker.request(`fetch-image ${JSON.stringify(url)}`, sessionId(ctx), signal)));
      if (signal?.aborted) throw new Error("fetch_images cancelled");
      const successes = settled.flatMap((result) => result.status === "fulfilled" ? [result.value] : []);
      const failedCount = settled.length - successes.length;
      if (successes.length === 0) throw new Error(`All ${urls.length} parallel image fetches failed`);
      let attachmentBytes = 0;
      const deliverable = successes.filter((response) => {
        const size = statSync(response.imagePath).size;
        if (attachmentBytes + size > MAX_BATCH_IMAGE_BYTES) return false;
        attachmentBytes += size;
        return true;
      });
      if (deliverable.length === 0) throw new Error("Parallel image results exceed the aggregate attachment byte limit");
      const omittedForBudget = successes.length - deliverable.length;
      const text = [
        `Fetched ${deliverable.length}/${urls.length} images in parallel${failedCount ? `; ${failedCount} failed validation or download` : ""}${omittedForBudget ? `; ${omittedForBudget} omitted by the aggregate attachment limit` : ""}.`,
        "Include each successful image in the final reply with exactly these markers:",
        ...deliverable.map((response) => `[[image: ${response.imagePath}]]`),
      ].join("\n");
      return {
        content: [{ type: "text" as const, text }],
        details: { successCount: deliverable.length, failedCount, omittedForBudget, totalCount: urls.length, attachmentBytes, results: deliverable },
      };
    },
  });

  toolApi.registerTool({
    name: "google_search",
    label: "Directional Google Search",
    description: "EXPLICIT USER REQUEST ONLY: call this only when the user's own message explicitly asks for a Google search (e.g. 「用 Google 搜」, \"google it\"). For every other web lookup use research, which already includes a Google query. Searches Google directly through Nodriver with one to four distinct query directions in parallel and returns a globally de-duplicated, diversity-balanced Top 10 in the same title/URL/snippet format as web_search.",
    promptGuidelines: [
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
    async execute(_callId, params, signal, _onUpdate, ctx) {
      const seen = new Set<string>();
      const searches = params.searches
        .map((search) => ({ direction: search.direction.trim(), query: search.query.trim() }))
        .filter((search) => {
          const key = search.query.toLocaleLowerCase();
          if (!search.direction || !search.query || seen.has(key)) return false;
          seen.add(key);
          return true;
        })
        .slice(0, 4);
      if (searches.length === 0) throw new Error("google_search requires at least one non-empty directional query");
      let confirmedCurrentTime: string | undefined;
      if (searches.some((search) => TIME_SENSITIVE_SEARCH_PATTERN.test(`${search.direction} ${search.query}`))) {
        confirmedCurrentTime = (params.currentTime ?? "").trim();
        const supplied = parseGettimeValue(confirmedCurrentTime);
        if (supplied === undefined || Math.abs(Date.now() - supplied) > FRESH_TIME_MAX_AGE_MS) {
          throw new Error('Time-sensitive google_search requires a fresh gettime(action: "now") result in currentTime. Call gettime first, copy its complete output, and retry.');
        }
      }
      const response = await worker.request(`google-search ${JSON.stringify(searches)}`, sessionId(ctx), signal);
      const { output, truncated: wasTruncated } = truncated(response.text || "(no Google results)");
      return {
        content: [{ type: "text" as const, text: confirmedCurrentTime ? `[Current time confirmed: ${confirmedCurrentTime}]\n\n${output}` : output }],
        details: { ...response, confirmedCurrentTime, truncated: wasTruncated },
      };
    },
  });

  toolApi.registerTool({
    name: "crawl",
    label: "Parallel Browser Crawl",
    description: "Crawl one or more web pages or PDF files in parallel in the shared Chrome. HTML pages return their text plus ranked image candidates; PDFs return their text. Only crawl exact URLs the user gave or a search returned; never guess URLs.",
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
    async execute(_callId, params, signal, _onUpdate, ctx) {
      // An item may hold several URLs or trailing punctuation.
      const urls = [...new Set((params.urls.join(" ").match(/https?:\/\/[^\s"'\]\[\<\>]+/g) ?? []).map((url) => url.replace(/[.,;)]+$/, "")))];
      if (urls.length === 0) {
        return { content: [{ type: "text" as const, text: "Error: No valid URLs provided to crawl." }], details: { error: "No valid URLs provided to crawl" } };
      }
      const response = await worker.request(`crawl ${JSON.stringify(urls)}`, sessionId(ctx), signal);
      const { output, truncated: wasTruncated } = truncated(response.text || "(no output)");
      return { content: [{ type: "text" as const, text: output }], details: { ...response, truncated: wasTruncated } };
    },
  });

  // browser_intent's window (session `intent-<id>`) is deliberately left open: piweb and
  // piscord end the Pi process after each Life turn or 10 idle minutes, and the next turn
  // of the same conversation (a login code, "open the first result") must find its page.
  // The worker closes windows idle for 30 minutes.
  pi.on("session_shutdown", async (_event, ctx) => {
    try {
      await worker.cleanupSession(sessionId(ctx));
    } finally {
      worker.disconnect();
    }
  });
}
