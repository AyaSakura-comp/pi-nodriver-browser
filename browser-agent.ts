/**
 * browser_intent's autopilot: one LLM drives the browser until the goal is met.
 *
 * Built for an inference engine with ONE slot whose state cannot be cut back (Strata):
 * - The task is one growing conversation. Each call sends everything sent before plus the
 *   result of the last action, so the engine reads only the new tokens. (The old prompt put
 *   "Step N / Steps so far" before the page: every step re-read the whole page, 52% of a
 *   task's time.)
 * - A page is listed whole, in parts ("more" shows the next part, no scrolling), and after an
 *   action on the same page only the new or changed lines are sent (element refs are stable).
 * - The conversation is the memory: earlier pages stay in it, and "done" evidence may come
 *   from any page seen in the task.
 * - The requests ask the engine to hold itself for this task between steps and to keep no
 *   session file for it (Strata's "hold"; other servers ignore the field).
 *
 * Kept deliberately small: no planner/judge/embedding layers. Mechanical gates back the
 * prompt rules: irreversible clicks (buy, pay, delete, log out) need confirm=true, secrets
 * (passwords, OTP, card numbers) are typed only when the goal itself contains them, and an
 * action already tried on the same page state is not run again.
 */

export interface WorkerLike {
  request(command: string, sessionId: string, signal?: AbortSignal): Promise<Record<string, any>>;
}

export interface LlmConfig {
  endpoint: string;            // .../chat/completions
  model: string;
  headers?: Record<string, string>;
  local?: boolean;             // local Strata/llama.cpp-style server: greedy, thinking off, engine hold
}

export interface TaskOptions {
  goal: string;
  url?: string;
  confirm?: boolean;
  session: string;
  worker: WorkerLike;
  llm: LlmConfig;
  /** Used for the rest of the task when `llm` twice fails to give a valid action (e.g. a tiny model). */
  fallbackLlm?: LlmConfig;
  signal?: AbortSignal;
  onProgress?: (line: string) => void;
  /** Milliseconds between looks at a page on which "more" found nothing new (tests pass 0). */
  lookAgainMs?: number;
}

export type TaskStatus = "TASK_DONE" | "TASK_UNVERIFIED" | "TASK_STOPPED" | "TASK_FAILED"
  | "TASK_NEEDS_CONFIRM" | "TASK_NEEDS_INPUT" | "TASK_BLOCKED";

/** One span of the task's time: a model call (with the server's own figures) or a browser command. */
export interface TraceSpan {
  at_ms: number;               // start, from the task's start
  ms: number;
  kind: "model" | "browser";
  label: string;
  wait_ms?: number;            // model: time not spent reading or writing (queued behind other requests)
  prompt_n?: number;           // model: tokens read this call
  cache_n?: number;            // model: tokens reused from the engine's state
  prompt_ms?: number;
  predicted_n?: number;
  predicted_ms?: number;
}

export interface TaskResult {
  status: TaskStatus;
  answer: string;
  evidence: string[];
  missing_evidence?: string[];
  reason?: string;
  url?: string;
  title?: string;
  steps: string[];
  screenshot?: string;
  page_excerpt?: string;
  elapsed_ms: number;
  llm_ms: number;              // model calls, wall time (includes wait_ms)
  browser_ms: number;
  wait_ms: number;             // of llm_ms: queued behind other requests on the engine
  model_calls: number;
  trace: TraceSpan[];
  planner_switched?: string;
}

const SYSTEM_PROMPT = `You operate a real Chrome browser to complete the user's goal. This conversation is the task so far: each of your replies is a browser action, and each user message says what it did and what the page shows now.

How pages are shown:
- A page is listed as text from its top to its end, not only the part on screen: you never scroll to read. Interactive elements appear as [eN] role "name"; the other lines are the page's text in page order (a price usually follows its product).
- A long page comes in parts: {"action":"more"} shows the next part.
- After an action that stays on the same page you get only the lines that are new or changed; what was listed before still holds unless the message says lines are gone.
- A site's menus (header and footer links, navigation, sidebars) come last, under "Site menu:", after the page's content.
- Pages you saw earlier stay above in this conversation: use them, do not go back to read them again. A ref works only on the page it was listed for.

Reply with JSON only, no other text: one action object, or an array of actions to run in order when you fill one form (select, type, type, click). An array stops at the first action that fails or opens another page.
{"action":"click","ref":"e12"}
{"action":"type","ref":"e5","text":"words to type","submit":true}
{"action":"select","ref":"e7","option":"visible option text"}
{"action":"press","key":"Enter"}  (Enter, Escape, Tab, ArrowDown, PageDown)
{"action":"more"}  (the next part of a long page)
{"action":"scroll","direction":"down"}  (only to make a page load more items; it never shows text that "more" does not)
{"action":"back"}
{"action":"google","query":"keywords"}  (Google search: only to find a site, or when this site is wrong or broken)
{"action":"open","url":"https://..."}  (a URL from the goal or a site's home page; for a page that a listed link leads to, click the link instead of writing its address)
{"action":"done","answer":"the result the goal asks for, with exact names, prices, dates, numbers","evidence":["short quote copied exactly from a page"]}
{"action":"stop","status":"needs_input|needs_confirm|blocked|failed","reason":"why, and what is needed"}

Rules:
- done and stop are always the only action of a reply.
- Use only refs listed for the current page. If what you need is not listed, use more, or the site's search box.
- select is only for [eN] select dropdowns; use click for links, buttons, tabs and options.
- To find an item on a site, type into the site's search box with "submit":true in one step.
- Among similar options (suggestions, stations, products), choose the one whose name equals what the goal names; an option with extra words ("-環島", "Pro", "2入組") is a different item.
- Reply done as soon as the pages you have seen show what the goal asks for; do not keep browsing. For a list, give the top items with their prices or key facts. When the goal covers several pages, read each once and answer from all of them.
- If a list does not show a detail the goal asks for (a wattage, a spec), open at most three of the most likely items to read it, then answer with what you found and say what the list does not show. A shorter form of a detail in the list (an address without its city, a price range) is that detail: answer with it, do not open the items for a longer form.
- evidence: 1-3 short quotes copied character for character from pages listed in this conversation.
- To give a link in the answer, write the ref of its link in square brackets, like [e12]: it is replaced with the real address. Never write a URL you did not see.
- Never type passwords, OTP/verification codes, card numbers or personal data unless the goal gives them. At a login or verification wall the goal cannot pass, stop with needs_input and list the fields the page asks for.
- Never buy, pay, check out, place or submit an order, delete, unsubscribe or log out unless the goal asks for it and says it is confirmed; otherwise stop with needs_confirm. Add to cart only when the goal asks for it.
- CAPTCHA, "unusual traffic" or app-only pages: stop with blocked.
- If a page is a login or verification wall, do not go back, reload or search the site again: the site shows the wall again. Stop (needs_input or blocked) and say what the page shows.
- If the page is an error page or the wrong site, use google.
- Do not repeat an action that did not change the page; try something else.
- Write answer and reason in the language of the goal.`;

const IRREVERSIBLE = /結帳|立即購買|直接購買|馬上購買|確認購買|立即訂購|付款|下單|送出訂單|確認訂單|提交訂單|完成訂單|刪除|登出|取消訂閱|退訂|check ?out|buy now|place order|pay now|purchase|delete|log ?out|sign ?out|unsubscribe/i;
const ADD_TO_CART = /加入購物車|放入購物車|加入購物籃|add to (cart|bag|basket)|\(icon: cart\)/i;
const SECRET_FIELD = /type=password|密碼|password|passcode|驗證碼|otp|one-time|一次性|卡號|card number|cvv|cvc|安全碼/i;
const WALL = /請(先)?登入|登入以繼續|登入後才|sign in to continue|log ?in to continue|please (log|sign) ?in|驗證您是人類|確認您的連線|我不是機器人|unusual traffic|captcha|are you a robot|verify (that )?you are (a )?human/i;
const FIRST_PART_CHARS = 6000;   // the first part of a new page (the answers of 22 measured tasks stood in its first 3,100 characters)
const PART_CHARS = 10000;        // a later part, or an update of the same page
const MAX_STEPS = 20;            // model replies
const MAX_BATCH = 6;             // actions in one reply
const OWN_BUDGET_MS = 150_000;   // browser time + the model's reading and writing; time queued behind other requests is not counted
const WALL_LIMIT_MS = 300_000;   // however long the queue: the task ends here
const REBASE_CHARS = 160_000;    // conversation size at which earlier pages are dropped
const LOOK_AGAIN_MS = 500;       // between looks at a page "more" found nothing new on (7 looks)
const HOLD_SECONDS = 20;         // how long the engine waits for this task's next step (a slow page load)

/** Escape raw newlines/tabs inside JSON strings: models often write a multi-line answer literally. */
function escapeControlInStrings(json: string): string {
  let out = "";
  let inString = false;
  for (let i = 0; i < json.length; i++) {
    const ch = json[i];
    if (inString) {
      if (ch === "\\") { out += ch + (json[i + 1] ?? ""); i++; continue; }
      if (ch === '"') inString = false;
      else if (ch === "\n") { out += "\\n"; continue; }
      else if (ch === "\r") { continue; }
      else if (ch === "\t") { out += "\\t"; continue; }
    } else if (ch === '"') {
      inString = true;
    }
    out += ch;
  }
  return out;
}

function actionsOf(value: unknown): Record<string, any>[] {
  const isAction = (v: any) => v && typeof v === "object" && !Array.isArray(v) && typeof v.action === "string";
  if (Array.isArray(value)) return value.filter(isAction);
  if (value && typeof value === "object" && Array.isArray((value as any).actions)) return (value as any).actions.filter(isAction);
  return isAction(value) ? [value as Record<string, any>] : [];
}

/** The actions of a reply: one object, an array of them, or {"actions": [...]}; prose around it is ignored. */
export function parseActions(text: string): Record<string, any>[] {
  const cleaned = text.replace(/<think>[\s\S]*?<\/think>/g, "").replace(/```(?:json)?/g, "");
  // The JSON may follow some prose; try every value start until one parses to actions.
  let start = cleaned.search(/[{[]/);
  while (start >= 0) {
    let depth = 0;
    let inString = false;
    for (let i = start; i < cleaned.length; i++) {
      const ch = cleaned[i];
      if (inString) {
        if (ch === "\\") i++;
        else if (ch === '"') inString = false;
        continue;
      }
      if (ch === '"') inString = true;
      else if (ch === "{" || ch === "[") depth++;
      else if ((ch === "}" || ch === "]") && --depth === 0) {
        const candidate = cleaned.slice(start, i + 1);
        for (const attempt of [candidate, escapeControlInStrings(candidate)]) {
          try {
            const actions = actionsOf(JSON.parse(attempt));
            if (actions.length) return actions;
          } catch {}
        }
        break;
      }
    }
    const next = cleaned.slice(start + 1).search(/[{[]/);
    start = next < 0 ? -1 : start + 1 + next;
  }
  return [];
}

export function parseAction(text: string): Record<string, any> | undefined {
  return parseActions(text)[0];
}

function normalize(text: string): string {
  return text.normalize("NFKC").replace(/[\s​]+/g, "").toLowerCase();
}

function describeElement(line: string | undefined, ref: string): string {
  if (!line) return ref;
  const body = line.replace(/^\[e\d+\]\s*/, "");
  return body.length > 70 ? body.slice(0, 69) + "…" : body;
}

/** open takes a URL from the goal or the start URL, a Google search, any site's home page,
 * or a page on a site the goal, start URL or a page seen in the task names (a search result
 * shows its host); and any URL as the task's first page, when no start URL was given (the
 * planner has to begin somewhere, and a wrong address shows as an error page). Refused: a
 * deep link the planner guessed mid-task on a site nothing mentions. */
function openAllowed(url: string, goal: string, start: string | undefined, seen: string, anyPageOpen: boolean): boolean {
  let u: URL;
  try {
    u = new URL(url);
  } catch {
    return false;
  }
  if (u.protocol !== "https:" && u.protocol !== "http:") return false;
  if (!anyPageOpen && !start) return true;
  if (goal.includes(url) || url === start) return true;
  if (/^www\.google\.[a-z.]+$/.test(u.hostname) && u.pathname === "/search") return true;
  if (u.pathname === "/" && !u.search) return true;
  const host = u.hostname.toLowerCase().replace(/^www\./, "");
  return [goal, start ?? "", seen].some((text) => text.toLowerCase().includes(host));
}

/** Page lines as the text a reader sees: no "[e12] link" before an element's name, no quotes around it. */
function plainText(text: string): string {
  return text.replace(/^\[e\d+\] [a-z]+ "(.*?)"(?: [a-z]+(?:=\S+|="[^"]*")?)*$/gm, "$1")
    .replace(/^\[e\d+\] [a-z]+ /gm, "").replace(/\[img ([^\]]*)\]/g, "$1");
}

/** Is the quote on a page? Whole, or - a quote that joins lines, or leaves out an icon or
 * a separator between them - as its pieces in order with next to nothing between them
 * (40 characters: more would let a quote pair one item's name with the next item's price). */
function quoteFound(quote: string, pages: string[]): boolean {
  const bare = quote.replace(/^["'「『]|["'」』]$/g, "");
  const whole = normalize(plainText(bare));
  if (!whole) return false;
  const pieces = plainText(bare).split(/\n|\s[·•|｜/]\s|\s{2,}/).map(normalize).filter((piece) => piece.length >= 2);
  for (const page of pages) {
    if (page.includes(whole)) return true;
    if (pieces.length < 2) continue;
    for (let at = page.indexOf(pieces[0]); at >= 0; at = page.indexOf(pieces[0], at + 1)) {
      let cursor = at + pieces[0].length;
      const inOrder = pieces.slice(1).every((piece) => {
        const next = page.indexOf(piece, cursor);
        if (next < 0 || next - cursor > 40) return false;
        cursor = next + piece.length;
        return true;
      });
      if (inOrder) return true;
    }
  }
  return false;
}

/** host + path of an address, for "the same page but for its query" (a guess next to a real link). */
function pathKey(url: string): string {
  try {
    const u = new URL(url);
    return (u.hostname.replace(/^www\./, "") + u.pathname.replace(/\/$/, "")).toLowerCase();
  } catch {
    return "";
  }
}

function fingerprint(text: string): string {
  let hash = 0;
  for (let i = 0; i < text.length; i++) hash = (Math.imul(hash, 31) + text.charCodeAt(i)) | 0;
  return (hash >>> 0).toString(36);
}

function shortUrl(url: string | undefined): string {
  if (!url) return "";
  try {
    const u = new URL(url);
    const path = decodeURIComponent(u.pathname + u.search);
    return u.host + (path.length > 60 ? path.slice(0, 59) + "…" : path);
  } catch {
    return url.slice(0, 80);
  }
}

interface Part { text: string; shown: number; left: number; gone: number; first: boolean }

/** The current page as the model knows it: the latest snapshot's lines and which of them it has been shown. */
class PageView {
  doc = "";
  url = "";
  title = "";
  status = 0;
  truncated = false;
  notes: string[] = [];
  lines: string[] = [];
  refLine = new Map<string, string>();
  private seen = new Map<string, number>();

  get open(): boolean { return this.doc !== ""; }

  /** Take a command's result. newDoc: another document than before (its refs and lines are all new). */
  adopt(result: Record<string, any>): { newDoc: boolean; changed: boolean } {
    // Results without the parts (an older worker) carry the page as "header\n---\nlines".
    const body = typeof result.body === "string" ? result.body
      : typeof result.snapshot === "string" ? result.snapshot.split("\n---\n").slice(1).join("\n---\n") : "";
    const doc = String(result.doc ?? result.url ?? "page");
    const newDoc = doc !== this.doc;
    const changed = newDoc || body !== this.lines.join("\n");
    if (newDoc) this.seen = new Map();
    this.doc = doc;
    this.url = String(result.url ?? "");
    this.title = String(result.title ?? "");
    this.status = Number(result.httpStatus ?? 0);
    this.truncated = result.truncated === true;
    if (Array.isArray(result.notes)) this.notes.push(...result.notes.map(String));
    this.lines = body && body !== "(no visible text)" ? body.split("\n") : [];
    this.refLine = new Map();
    for (const line of this.lines) {
      const ref = line.match(/^\[(e\d+)\] /)?.[1];
      if (ref) this.refLine.set(ref, line);
    }
    return { newDoc, changed };
  }

  /** What the page shows, for "same action on the same page": clocks and request ids left out
   * (a login wall that prints the time and a request id is the same wall every time). */
  state(): string {
    return this.doc.split(".")[0] + "|" + this.url.split("#")[0] + "|" + fingerprint(this.lines.join("\n")
      .replace(/\d{4}-\d{2}-\d{2}T[\d:.]+Z?|\b\d{1,2}:\d{2}(:\d{2})?\b|\b[0-9a-f]{6,}(-[0-9a-f]{3,})+\b|\b[0-9a-f]{16,}\b/gi, "#"));
  }

  /** The lines the model has not been shown yet, in page order, up to `limit` characters;
   * they count as shown from now on. Lines are matched as a multiset (twelve "登記" lines are
   * twelve lines), and a line that left the page is forgotten: if it comes back it is new. */
  take(limit: number): Part {
    const avail = new Map(this.seen);
    const next = new Map<string, number>();
    const add = (line: string) => next.set(line, (next.get(line) ?? 0) + 1);
    const unseen: number[] = [];
    let known = 0;
    for (const count of this.seen.values()) known += count;
    let kept = 0;
    this.lines.forEach((line, i) => {
      const n = avail.get(line) ?? 0;
      if (n > 0) {
        avail.set(line, n - 1);
        add(line);
        kept++;
      } else {
        unseen.push(i);
      }
    });
    const out: string[] = [];
    let chars = 0;
    let shown = 0;
    let previous = -2;
    for (const i of unseen) {
      const line = this.lines[i];
      if (shown && chars + line.length + 1 > limit) break;
      // In an update, say where a run of new lines stands on the page.
      if (known && i !== previous + 1 && i > 0) out.push(`  (after: ${this.lines[i - 1].slice(0, 80)})`);
      out.push(line);
      chars += line.length + 1;
      shown++;
      previous = i;
      add(line);
    }
    this.seen = next;
    return { text: out.join("\n"), shown, left: unseen.length - shown, gone: known - kept, first: known === 0 };
  }

  /** How many of the page's lines the model has not been shown. */
  pending(): number {
    const avail = new Map(this.seen);
    let unseen = 0;
    for (const line of this.lines) {
      const n = avail.get(line) ?? 0;
      if (n > 0) avail.set(line, n - 1);
      else unseen++;
    }
    return unseen;
  }

  /** Forget what was shown: the next take() lists the page from its top again. */
  reset(): void { this.seen = new Map(); }
}

interface ChatReply { content: string; ms: number; serverMs?: number; timings?: Record<string, any> }

async function chat(llm: LlmConfig, messages: { role: string; content: string }[], holdKey: string,
  signal: AbortSignal | undefined, timeoutMs: number): Promise<ChatReply> {
  const body: Record<string, unknown> = { model: llm.model, messages, max_tokens: 2000 };
  if (llm.local) {
    body.temperature = 0;
    body.chat_template_kwargs = { enable_thinking: false };
    // Strata: keep the engine for this task's next step, and write no session file for it.
    body.hold = { key: holdKey, seconds: HOLD_SECONDS, keep: false };
  }
  const started = Date.now();
  const timeout = AbortSignal.timeout(timeoutMs);
  const response = await fetch(llm.endpoint, {
    method: "POST",
    headers: { "content-type": "application/json", ...(llm.headers ?? {}) },
    body: JSON.stringify(body),
    signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
  });
  if (!response.ok) {
    const detail = (await response.text()).slice(0, 300);
    throw new Error(`model HTTP ${response.status}: ${detail}`);
  }
  const data: any = await response.json();
  const timings = data?.timings && typeof data.timings === "object" ? data.timings : undefined;
  const serverMs = timings && typeof timings.prompt_ms === "number" && typeof timings.predicted_ms === "number"
    ? timings.prompt_ms + timings.predicted_ms : undefined;
  return { content: String(data?.choices?.[0]?.message?.content ?? ""), ms: Date.now() - started, serverMs, timings };
}

/** The task is over: let the engine go (Strata's POST /v1/hold; any other server answers 404). */
async function releaseHold(llm: LlmConfig, holdKey: string): Promise<void> {
  if (!llm.local || !/\/chat\/completions$/.test(llm.endpoint)) return;
  try {
    await fetch(llm.endpoint.replace(/\/chat\/completions$/, "/hold"), {
      method: "POST",
      headers: { "content-type": "application/json", ...(llm.headers ?? {}) },
      body: JSON.stringify({ key: holdKey }),
      signal: AbortSignal.timeout(3000),
    });
  } catch {}
}

export async function runBrowserTask(options: TaskOptions): Promise<TaskResult> {
  const { goal, worker, session, signal } = options;
  let llm = options.llm;
  let switched: string | undefined;
  const started = Date.now();
  const progress = options.onProgress ?? (() => undefined);
  const holdKey = `browser:${session}:${started.toString(36)}`;
  const held = new Set<LlmConfig>();
  const steps: string[] = [];
  const trace: TraceSpan[] = [];
  let llmMs = 0;
  let browserMs = 0;
  let waitMs = 0;
  let ownMs = 0;
  let modelCalls = 0;
  const view = new PageView();
  const corpus: string[] = [];              // every page text seen in the task: done evidence is checked against it
  const links = new Map<string, string>();  // ref -> address, over the whole task (refs are never reused)
  let pageLinks: string[] = [];             // the addresses of the current page's links
  const noGuess = new Set<string>();        // hosts on which a guessed address turned out wrong
  let wallSeen = false;                     // a login or verification wall was shown to the model
  let offWall = false;                      // ...and the task has moved off it since
  const tried = new Map<string, { count: number; step: number; outcome: string }>();
  let finalUrl: string | undefined;

  const browser = async (command: string, label: string) => {
    const t = Date.now();
    try {
      return await worker.request(command, session, signal);
    } finally {
      const ms = Date.now() - t;
      browserMs += ms;
      ownMs += ms;
      trace.push({ at_ms: t - started, ms, kind: "browser", label });
    }
  };
  const quote = (text: string) => JSON.stringify(text);
  const adopt = (result: Record<string, any>) => {
    const change = view.adopt(result);
    if (change.changed && view.lines.length) corpus.push(view.lines.join("\n"));
    if (result.links && typeof result.links === "object") {
      for (const [ref, href] of Object.entries(result.links)) links.set(ref, String(href));
      pageLinks = Object.values(result.links).map(String);
    }
    finalUrl = view.url || finalUrl;
    return change;
  };

  /** The page as a block of a user message. kind: a page not shown before, an update of the same page, or its next part. */
  const block = (kind: "page" | "update" | "more"): string => {
    const part = view.take(kind === "page" ? FIRST_PART_CHARS : PART_CHARS);
    const notes = view.notes.splice(0);
    const end = !part.left && view.truncated ? "\n(The page goes on past what could be listed.)" : "";
    const wall = onWall()
      ? `\nThis is a login or verification wall${sentFrom()}. Going back, opening the address again or searching the site again leads back to it. Unless the goal gives what it asks for, stop now: needs_input for a login, blocked for a CAPTCHA or bot check.` : "";
    if (kind === "page" || part.first) {
      const head = [`Page: ${view.title}`, `URL: ${view.url.length > 200 ? view.url.slice(0, 199) + "…" : view.url}`];
      if (view.status >= 400) head.push(`HTTP status: ${view.status} (error page)`);
      head.push(...notes);
      head.push(part.left ? `The page's text from its top, first part (${part.left} more lines: {"action":"more"} shows the next part):`
        : "The page's whole text:");
      return `${head.join("\n")}\n---\n${part.text || "(the page shows no text)"}${end}${wall}`;
    }
    const head = notes.length ? notes.join("\n") + "\n" : "";
    if (kind === "more") {
      return head + (part.shown
        ? `Next part of the page (${part.left ? `${part.left} more lines after it` : "this is its end"}):\n${part.text}${end}`
        : `There is no more text on this page.${end}`);
    }
    const gone = part.gone ? `${part.gone} earlier lines are gone` : "";
    if (!part.shown) return `${head}Same page: nothing new appeared${gone ? ` (${gone})` : ""}.${wall}`;
    const more = part.left ? `; ${part.left} more new lines: {"action":"more"} shows them` : "";
    return `${head}Same page, ${part.shown} new or changed lines${gone ? `, ${gone}` : ""}${more}:\n${part.text}${end}${wall}`;
  };

  /** The page is a login or verification wall: little on it, and it says so. */
  function onWall(): boolean {
    return view.open && view.refLine.size <= 30 && WALL.test(view.lines.join("\n").slice(0, 3000));
  }
  let asked = options.url ?? "";            // the address the last open asked for
  function sentFrom(): string {
    return asked && pathKey(asked) !== pathKey(view.url) ? ` (the site sent ${shortUrl(asked)} here)` : "";
  }

  const finish = async (status: TaskStatus, extra: Partial<TaskResult>): Promise<TaskResult> => {
    let screenshot: string | undefined;
    if (view.open) {
      try {
        const t = Date.now();
        screenshot = (await worker.request("screenshot", session, AbortSignal.timeout(10_000))).path;
        trace.push({ at_ms: t - started, ms: Date.now() - t, kind: "browser", label: "screenshot" });
        browserMs += Date.now() - t;
      } catch {}
    }
    return {
      status, answer: "", evidence: [], steps, url: finalUrl, title: view.title || undefined, screenshot,
      ...(switched ? { planner_switched: switched } : {}),
      page_excerpt: view.lines.join("\n").slice(0, 1500), elapsed_ms: Date.now() - started,
      llm_ms: llmMs, browser_ms: browserMs, wait_ms: waitMs, model_calls: modelCalls, trace,
      ...extra,
    };
  };

  const head = `Goal: ${goal}\nIrreversible steps confirmed by the user: ${options.confirm ? "yes" : "no"}\n\n`;
  const messages: { role: string; content: string }[] = [{ role: "system", content: SYSTEM_PROMPT }];

  /** One model reply. While it waits (another client's request is running on the one-slot engine), say so. */
  const ask = async (label: string): Promise<ChatReply> => {
    const t = Date.now();
    let told = 0;
    const timer = setInterval(() => {
      const waited = Math.round((Date.now() - t) / 1000);
      if (waited >= (told ? told + 20 : 10)) {
        told = waited;
        progress(`⏳ model: ${waited}s (reading a long page, or busy with another request)`);
      }
    }, 1000);
    try {
      held.add(llm);
      const wallLeft = WALL_LIMIT_MS - (Date.now() - started);
      const reply = await chat(llm, messages, holdKey, signal, Math.max(5_000, Math.min(120_000, wallLeft + 5_000)));
      const own = Math.min(reply.ms, reply.serverMs ?? reply.ms);
      llmMs += reply.ms;
      ownMs += own;
      waitMs += reply.ms - own;
      modelCalls++;
      const tm = reply.timings ?? {};
      trace.push({ at_ms: t - started, ms: reply.ms, kind: "model", label, wait_ms: reply.ms - own,
        prompt_n: tm.prompt_n, cache_n: tm.cache_n, prompt_ms: tm.prompt_ms, predicted_n: tm.predicted_n, predicted_ms: tm.predicted_ms });
      return reply;
    } catch (error) {
      llmMs += Date.now() - t;
      throw error;
    } finally {
      clearInterval(timer);
    }
  };

  try {
    let first = "(No page is open yet: open a URL, or use google.)";
    if (options.url) {
      progress(`🌐 open ${shortUrl(options.url)}`);
      try {
        adopt(await browser(`open ${quote(options.url)}`, "open"));
        steps.push(`open ${options.url} → ${view.title || shortUrl(view.url)}`);
        first = block("page");
      } catch (error) {
        const message = `open ${options.url} failed: ${(error as Error).message}`;
        steps.push(message);
        first = `${message}\n${first}`;
      }
    } else {
      try {
        adopt(await browser("snapshot", "snapshot"));
        if (view.open) first = block("page");
      } catch {}
    }
    messages.push({ role: "user", content: `${head}${first}\n\nStep 1 of at most ${MAX_STEPS}. Reply with JSON.` });
    wallSeen = onWall();

    for (let step = 1; step <= MAX_STEPS; step++) {
      signal?.throwIfAborted();
      if (ownMs > OWN_BUDGET_MS || Date.now() - started > WALL_LIMIT_MS) break;
      // Too long a conversation: go on with the goal, the steps so far and the current page.
      if (messages.reduce((n, m) => n + m.content.length, 0) > REBASE_CHARS) {
        view.reset();
        const page = view.open ? block("page") : "(No page is open.)";
        messages.splice(1, messages.length - 1, { role: "user",
          content: `${head}Steps so far (their pages are no longer shown):\n${steps.map((s, i) => `${i + 1}. ${s}`).join("\n")}\n\n${page}\n\nStep ${step} of at most ${MAX_STEPS}. Reply with JSON.` });
      }

      let actions: Record<string, any>[] = [];
      let lastReply = "";
      let callError = "";
      for (let attempt = 0; attempt < 2 && !actions.length; attempt++) {
        // The correction stays in the conversation: what was sent is never changed afterwards.
        if (attempt) messages.push({ role: "user", content: `Your reply was not valid JSON actions (it began: ${JSON.stringify(lastReply.slice(0, 160))}). Reply with ONLY one JSON action object, or an array of them; write line breaks inside strings as \\n.` });
        try {
          lastReply = (await ask(`step ${step}${attempt ? " (again)" : ""}`)).content;
        } catch (error) {
          signal?.throwIfAborted();
          callError = `browser planner model failed: ${(error as Error).message}`;
          break;
        }
        actions = parseActions(lastReply);
      }
      if (!actions.length && options.fallbackLlm && options.fallbackLlm.model !== llm.model && !switched) {
        // The conversation's model cannot drive the browser (seen: a 1.7B model answering with
        // <tool_call> markup): continue the task on the known-good planner.
        switched = `${llm.model} → ${options.fallbackLlm.model}`;
        llm = options.fallbackLlm;
        progress(`🔁 planner ${switched}`);
        step--;
        continue;
      }
      if (!actions.length) {
        return await finish("TASK_FAILED", { reason: callError || "the planner model did not return a valid action twice in a row" });
      }
      // The reply is kept as the bare actions: prose around the JSON would be copied in every later reply.
      actions = actions.slice(0, MAX_BATCH);
      messages.push({ role: "assistant", content: JSON.stringify(actions.length === 1 ? actions[0] : actions) });

      const results: string[] = [];        // what this reply's actions did, for the next user message
      let shownAs: "page" | "update" | "more" = "update";
      let leftDocument = false;
      let pageTouched = false;

      for (let index = 0; index < actions.length; index++) {
        const action = actions[index];
        let kind = String(action.action).toLowerCase();
        const ref = typeof action.ref === "string" ? action.ref.replace(/^@/, "").toLowerCase() : "";
        const line = ref ? view.refLine.get(ref) : undefined;
        // Models say "select" for "choose this result": on anything but a dropdown that is a click.
        if (kind === "select" && line && !/^\[e\d+\] select /.test(line)) kind = "click";
        if (kind === "search") kind = "google";
        if (kind === "fill") kind = "type";

        if (kind === "done" || kind === "stop") {
          if (index > 0) {
            results.push(`${kind} was not taken: it must be the only action of a reply, after you have seen what the others did.`);
            break;
          }
          if (kind === "stop") {
            const status = String(action.status ?? "failed").toLowerCase();
            const reason = String(action.reason ?? "").trim();
            const mapped: TaskStatus = status.includes("input") ? "TASK_NEEDS_INPUT"
              : status.includes("confirm") ? "TASK_NEEDS_CONFIRM"
              : status.includes("block") ? "TASK_BLOCKED" : "TASK_FAILED";
            progress(`🛑 ${mapped}: ${reason}`);
            steps.push(`stop ${status}: ${reason}`);
            return await finish(mapped, { reason });
          }
          // [e12] in the answer is that link's address (put in after the check below: those
          // addresses are real; the check is for the ones the planner wrote itself).
          const written = String(action.answer ?? "").trim();
          const answer = written
            .replace(/\s*\[(e\d+)\]/gi, (_all, r: string) => links.has(r.toLowerCase()) ? ` ${links.get(r.toLowerCase())} ` : "")
            .replace(/ +\n/g, "\n").replace(/ {2,}/g, " ").trim();
          const evidence = (Array.isArray(action.evidence) ? action.evidence : [action.evidence])
            .map((e: unknown) => String(e ?? "").trim()).filter(Boolean).slice(0, 5);
          let pageText = corpus.join("\n");
          if (view.open) {
            try {
              pageText += "\n" + String((await browser("get text", "read page text")).text ?? "");
            } catch {}
          }
          const pages = [...corpus, pageText].flatMap((text) => [normalize(text), normalize(plainText(text))]);
          const missing = evidence.filter((e: string) => !quoteFound(e, pages));
          // A URL in the answer must be a link seen in the task (or a page of it, or one the goal
          // gave): asked for 104 job links, the planner made up addresses. Query strings and
          // fragments may be dropped (a link's tracking parameters).
          const urls = [...new Set(written.match(/https?:\/\/[^\s"'<>()（）\]｜|，、；：。！？「」『』]+/g) ?? [])].map((u) => u.replace(/[.,;:!?]+$/, ""));
          if (urls.length) {
            let pageLinks = "";
            if (view.open) {
              try {
                pageLinks = String((await browser("get links", "read page links")).text ?? "");
              } catch {}
            }
            const bare = (u: string) => u.split(/[?#]/)[0].replace(/\/$/, "");
            const known = new Set([...pageLinks.split("\n"), ...links.values(), view.url, options.url ?? ""]
              .flatMap((u) => [u.replace(/\/$/, ""), bare(u)]));
            for (const url of urls) {
              if (!known.has(url.replace(/\/$/, "")) && !known.has(bare(url)) && !goal.includes(url) && !pageText.includes(url)) {
                missing.push(`link not on the page: ${url}`);
              }
            }
          }
          progress(`✅ done`);
          steps.push(`done`);
          const verified = evidence.length > 0 && missing.length === 0;
          return await finish(verified ? "TASK_DONE" : "TASK_UNVERIFIED", {
            answer, evidence, ...(missing.length ? { missing_evidence: missing } : {}),
            ...(verified ? {} : { reason: !evidence.length ? "no evidence quoted"
              : missing.some((m: string) => m.startsWith("link not on the page")) ? "the answer has links that are not on the page"
              : "some evidence quotes are not on the pages seen" }),
          });
        }

        const fail = (message: string, stepLine?: string) => {
          results.push(message);
          if (stepLine) steps.push(stepLine);
        };
        let command = "";
        let label = "";
        let guessed = false;
        let openNote = "";
        const name = describeElement(line, ref);
        if (kind === "click" || kind === "type" || kind === "select") {
          if (!line) {
            fail(`${ref || "(no ref)"} is not on the current page (a ref works only on the page it was listed for).`, `${kind} ${ref} → not on the page`);
            break;
          }
        }
        if (kind === "click") {
          if (IRREVERSIBLE.test(line!) && !options.confirm) {
            steps.push(`click ${name} → not taken (irreversible)`);
            return await finish("TASK_NEEDS_CONFIRM", { reason: `the next step is an irreversible action: ${name}. Ask the user; call again with confirm=true only if they agree.` });
          }
          if (ADD_TO_CART.test(line!) && !/購物車|購物籃|cart|basket|bag|加入|下單|購買|buy/i.test(goal)) {
            steps.push(`click ${name} → not taken (adds to cart)`);
            return await finish("TASK_NEEDS_CONFIRM", { reason: `the next step adds an item to the cart (${name}), which the goal did not ask for.` });
          }
          command = `click ${ref}`;
          label = `🖱️ click ${name}`;
        } else if (kind === "type") {
          const text = String(action.text ?? action.value ?? "");
          if (SECRET_FIELD.test(line!) && text && !goal.includes(text)) {
            steps.push(`type into ${name} → not taken (secret not given in the goal)`);
            return await finish("TASK_NEEDS_INPUT", { reason: `the page asks for ${name}; the goal does not give that value. Ask the user for it.` });
          }
          command = `type ${ref} ${quote(text)}${action.submit ? " --submit" : ""}`;
          label = `⌨️ type ${quote(text)} into ${name}${action.submit ? " + Enter" : ""}`;
        } else if (kind === "select") {
          command = `select ${ref} ${quote(String(action.option ?? action.value ?? ""))}`;
          label = `🔽 select ${quote(String(action.option ?? action.value ?? ""))} in ${name}`;
        } else if (kind === "press") {
          command = `press ${quote(String(action.key ?? "Enter"))}`;
          label = `⌨️ press ${action.key ?? "Enter"}`;
        } else if (kind === "scroll") {
          const direction = ["down", "up", "top", "bottom"].includes(String(action.direction)) ? String(action.direction) : "down";
          command = `scroll ${direction}`;
          label = `📜 scroll ${direction}`;
        } else if (kind === "more") {
          label = "📄 more";
        } else if (kind === "back") {
          command = "back";
          label = "⬅️ back";
        } else if (kind === "google") {
          const query = String(action.query ?? "").trim();
          if (!query) { fail("google needs a query."); break; }
          asked = "";
          command = `open ${quote(`https://www.google.com/search?hl=zh-TW&gl=tw&q=${encodeURIComponent(query)}`)}`;
          label = `🔎 google ${quote(query)}`;
        } else if (kind === "open") {
          const url = String(action.url ?? "").trim();
          if (!openAllowed(url, goal, options.url, corpus.join("\n"), view.open)) {
            fail(`open does not take a guessed link on a site that is not in the goal or on a page you saw (${url || "none given"}); open the site's home page, search, or click links instead.`,
              `open ${url} → refused (guessed link)`);
            break;
          }
          // A deep link the planner wrote itself (not one a page listed, not from the goal):
          // next to a real link to the same page it is that link with a wrong query
          // ("?Lang=zh_TW" for "?Lang=zh-TW" opened a maintenance page), so the real one is opened;
          // and after one guess on a site turned out wrong, no more guesses there.
          let target = url;
          const listed = (address: string) => [...links.values()].some((known) => known.replace(/\/$/, "") === address.replace(/\/$/, ""));
          guessed = view.open && !goal.includes(url) && url !== options.url && !listed(url)
            && !/^https?:\/\/[^/]+\/?$/.test(url) && !/^https?:\/\/www\.google\.[a-z.]+\/search\?/.test(url);
          if (guessed) {
            const real = [...new Set(pageLinks.filter((address) => pathKey(address) === pathKey(url)))];
            const host = pathKey(url).split("/")[0];
            if (real.length) {
              target = real[0];
              guessed = false;
              openNote = `(Opened the page's own link ${target}: the address you wrote is not on the page.)`;
            } else if (noGuess.has(host)) {
              fail(`open was not done: an address you guessed on ${host} was wrong before. Click a link listed on a page, or use the site's search box.`,
                `open ${url} → refused (guessed link)`);
              break;
            }
          }
          asked = target;
          command = `open ${quote(target)}`;
          label = `🌐 open ${shortUrl(target)}`;
        } else {
          fail(`unknown action ${kind}.`);
          break;
        }
        const plain = label.replace(/^\S+\s/, "");

        // Loop guard: an action that was already done on this same page state is not done again
        // (scrolling up and down a page, a login wall and its "back to home" button); the
        // third time the planner picks it, the task stops.
        // ("more" moves through a page that stays the same: its state is also how much is left to read.)
        const key = `${kind}|${name}|${action.text ?? action.query ?? action.url ?? action.direction ?? action.key ?? action.option ?? ""}|${view.state()}${kind === "more" ? `|${view.pending()}` : ""}`;
        const before = tried.get(key);
        if (before) {
          before.count++;
          if (before.count >= 3) {
            steps.push(`${plain} → chosen again without progress`);
            return await finish("TASK_STOPPED", { reason: "the same action kept being chosen on a page it did not change" });
          }
          fail(`Not done again: "${plain}" was already done on this same page (step ${before.step}: ${before.outcome}). Choose something else, or finish with done or stop.`,
            `${plain} → not repeated`);
          break;
        }

        progress(label);
        let outcome = "";
        if (kind === "more") {
          // Nothing left to show: look at the page again first. What a click started may have
          // arrived since (a timetable's results came in after the page had gone quiet).
          // The planner expects more than the page shows: give a late result up to 3 s (the THSR
          // timetable came over 2 s after its page; with nothing new the planner searched again).
          let fresh = false;
          if (view.open && !view.pending()) {
            for (let look = 0; look < 7 && !fresh; look++) {
              if (look) await new Promise((resolve) => setTimeout(resolve, options.lookAgainMs ?? LOOK_AGAIN_MS));
              try {
                fresh = adopt(await browser("snapshot", "look again")).changed;
              } catch {
                break;
              }
            }
          }
          const text = block(fresh ? "update" : "more");
          shownAs = "more";
          outcome = /^There is no more text/m.test(text) ? "no more text" : fresh ? "the page had new lines" : "next part shown";
          results.push(text);
          steps.push(`more → ${outcome}`);
          tried.set(key, { count: 1, step, outcome });
          pageTouched = true;
          continue;
        }
        try {
          const url = view.url;
          const change = adopt(await browser(command, plain.slice(0, 60)));
          pageTouched = true;
          shownAs = "update";
          if (change.newDoc) {
            outcome = view.title || shortUrl(view.url);
            leftDocument = true;
          } else if (view.url !== url) {
            outcome = `${view.title || shortUrl(view.url)} (${shortUrl(view.url)})`;
          } else {
            outcome = change.changed ? "page updated" : kind === "scroll" ? "nothing new loaded" : "no visible change";
          }
          steps.push(`${plain} → ${outcome}`);
          results.push(`${plain} → ${outcome}`);
          if (openNote) results.push(openNote);
          if (guessed && (view.status >= 400 || (pathKey(view.url) !== pathKey(asked) && !pathKey(view.url).includes("/")))) {
            // The guess was wrong: an error page, or the site sent it to its home page.
            noGuess.add(pathKey(asked).split("/")[0]);
            results.push(`That address was a guess and the site does not have it${view.status >= 400 ? ` (HTTP ${view.status})` : " (it was sent to the home page)"}. Do not write addresses on this site yourself again: click links listed on pages, or use its search box.`);
          }
          tried.set(key, { count: 1, step, outcome });
          if (leftDocument) {
            const rest = actions.length - index - 1;
            if (rest > 0) results.push(`The other ${rest} action${rest > 1 ? "s were" : " was"} not run: the page changed.`);
            break;
          }
          // More actions follow in this reply: say now what this one showed. (A reply of
          // click, back, click, back on Google Maps never saw the panels it opened.)
          if (index < actions.length - 1 && change.changed && view.pending() > 0) {
            results.push(block("update"));
            pageTouched = false;
          }
        } catch (error) {
          const message = String((error as Error).message || error).replace(/^\w+Error: /, "");
          signal?.throwIfAborted();
          steps.push(`${plain} → failed: ${message.slice(0, 160)}`);
          results.push(`${plain} → failed: ${message.slice(0, 300)}`);
          tried.set(key, { count: 1, step, outcome: "failed" });
          if (/STALE_REF|not on the page/.test(message)) {
            try {
              leftDocument = adopt(await browser("snapshot", "snapshot")).newDoc;
              pageTouched = true;
            } catch {}
          }
          break;
        }
      }

      // A wall the model has been told about is there again after the task had moved off it
      // (home page, search again, the address again: Shopee took 4 model calls to give up):
      // stop here, as the model was told to.
      const wallNow = onWall();
      if (wallNow && wallSeen && (offWall || /"action":"(open|back|google)"/.test(messages[messages.length - 1].content))) {
        const says = view.lines.filter((l) => !/^\[e\d+\]/.test(l)).join(" ").slice(0, 200);
        const check = /驗證您是人類|確認您的連線|我不是機器人|unusual traffic|captcha|are you a robot|verify (that )?you are (a )?human/i.test(view.lines.join("\n"));
        steps.push(`the ${check ? "verification" : "login"} wall is back → stopped`);
        progress(`🛑 ${check ? "verification" : "login"} wall again`);
        return await finish(check ? "TASK_BLOCKED" : "TASK_NEEDS_INPUT", {
          reason: `the site shows its ${check ? "verification" : "login"} wall again after another way in was tried${sentFrom()}. The page says: ${says}`,
        });
      }
      if (wallNow) wallSeen = true;
      else if (wallSeen && view.open) offWall = true;
      // What the page shows now: a new page whole, the same page as its new lines only.
      if (shownAs !== "more" && pageTouched && view.open) results.push(block(leftDocument ? "page" : "update"));
      const left = MAX_STEPS - step;
      const pace = left === 0 ? "" : left === 1
        ? `Step ${step + 1} of ${MAX_STEPS}, the last one: reply done with what the pages so far answer, or stop.`
        : `Step ${step + 1} of at most ${MAX_STEPS}. Reply with JSON.`;
      messages.push({ role: "user", content: `${results.join("\n")}\n\n${pace}` });
    }
    const outOfTime = ownMs > OWN_BUDGET_MS || Date.now() - started > WALL_LIMIT_MS;
    return await finish("TASK_STOPPED", {
      reason: outOfTime ? "time budget used up before the goal was reached" : `stopped after ${MAX_STEPS} steps without reaching the goal`,
    });
  } catch (error) {
    // The tool's hard limit (a TimeoutError on the signal) ends the task like the budget
    // does: report the steps so far, not an error with nothing in it. A cancel still throws.
    if (signal?.aborted && (signal.reason as Error | undefined)?.name === "TimeoutError") {
      return await finish("TASK_STOPPED", { reason: "time limit reached before the goal; the steps show how far it got" });
    }
    throw error;
  } finally {
    await Promise.all([...held].map((config) => releaseHold(config, holdKey)));
  }
}
