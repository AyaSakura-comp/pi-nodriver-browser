/**
 * browser_intent's autopilot: one LLM picks one browser action per step from a
 * page snapshot ("[eN] role "name"" lines in page order) until the goal is met.
 *
 * Kept deliberately small: no planner/judge/embedding layers. Two mechanical
 * gates back the prompt rules: irreversible clicks (buy, pay, delete, log out)
 * need confirm=true, and secrets (passwords, OTP, card numbers) are typed only
 * when the goal itself contains them. "done" answers are checked against the
 * page text: quoted evidence that is not on the page makes the result UNVERIFIED.
 */

export interface WorkerLike {
  request(command: string, sessionId: string, signal?: AbortSignal): Promise<Record<string, any>>;
}

export interface LlmConfig {
  endpoint: string;            // .../chat/completions
  model: string;
  headers?: Record<string, string>;
  local?: boolean;             // local llama.cpp-style server: greedy, thinking off
}

export interface TaskOptions {
  goal: string;
  url?: string;
  confirm?: boolean;
  session: string;
  worker: WorkerLike;
  llm: LlmConfig;
  signal?: AbortSignal;
  onProgress?: (line: string) => void;
  maxSteps?: number;
  timeBudgetMs?: number;
}

export type TaskStatus = "TASK_DONE" | "TASK_UNVERIFIED" | "TASK_STOPPED" | "TASK_FAILED"
  | "TASK_NEEDS_CONFIRM" | "TASK_NEEDS_INPUT" | "TASK_BLOCKED";

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
  llm_ms: number;
  browser_ms: number;
}

const SYSTEM_PROMPT = `You operate a real Chrome browser to complete the user's goal. Each turn you get the goal, the steps taken so far and a snapshot of the current page. Interactive elements appear as [eN] role "name"; the other lines are the page's text in page order (a price usually follows its product).

Reply with exactly one JSON object and nothing else. Actions:
{"action":"click","ref":"e12"}
{"action":"type","ref":"e5","text":"words to type","submit":true}
{"action":"select","ref":"e7","option":"visible option text"}
{"action":"press","key":"Enter"}  (Enter, Escape, Tab, ArrowDown, PageDown)
{"action":"scroll","direction":"down"}  (down, up, top, bottom)
{"action":"back"}
{"action":"search","query":"keywords"}  (Google search: to find a site, or when this site is wrong or broken)
{"action":"open","url":"https://..."}  (only a URL that is written in the goal)
{"action":"done","answer":"the result the goal asks for, with exact names, prices, dates, numbers","evidence":["short quote copied exactly from the page"]}
{"action":"stop","status":"needs_input|needs_confirm|blocked|failed","reason":"why, and what is needed"}

Rules:
- Use only refs from the latest snapshot. If what you need is not listed, scroll or use the site's search box.
- To find an item on a site, type into the site's search box with "submit":true in one step.
- Among similar options (suggestions, stations, products), choose the one whose name equals what the goal names; an option with extra words ("-環島", "Pro", "2入組") is a different item.
- Reply done as soon as the page shows what the goal asks for; do not keep browsing. For a list, give the top items with their prices or key facts.
- evidence: 1-3 short quotes copied character for character from the current page.
- Never type passwords, OTP/verification codes, card numbers or personal data unless the goal gives them. At a login or verification wall the goal cannot pass, stop with needs_input and list the fields the page asks for.
- Never buy, pay, check out, place or submit an order, delete, unsubscribe or log out unless the goal asks for it and says it is confirmed; otherwise stop with needs_confirm. Add to cart only when the goal asks for it.
- CAPTCHA, "unusual traffic" or app-only pages: stop with blocked.
- If an action leads to a login, verification or error page, do not go back and retry it: stop (needs_input or blocked) and say what the page shows.
- If the page is an error page or the wrong site, use search.
- Do not repeat an action that did not change the page; try something else.
- Write answer and reason in the language of the goal.`;

const IRREVERSIBLE = /結帳|立即購買|直接購買|馬上購買|確認購買|立即訂購|付款|下單|送出訂單|確認訂單|提交訂單|完成訂單|刪除|登出|取消訂閱|退訂|check ?out|buy now|place order|pay now|purchase|delete|log ?out|sign ?out|unsubscribe/i;
const ADD_TO_CART = /加入購物車|放入購物車|加入購物籃|add to (cart|bag|basket)|\(icon: cart\)/i;
const SECRET_FIELD = /type=password|密碼|password|passcode|驗證碼|otp|one-time|一次性|卡號|card number|cvv|cvc|安全碼/i;
const MAX_SNAPSHOT_CHARS = 11000;

function parseAction(text: string): Record<string, any> | undefined {
  const cleaned = text.replace(/<think>[\s\S]*?<\/think>/g, "").replace(/```(?:json)?/g, "");
  const start = cleaned.indexOf("{");
  if (start < 0) return undefined;
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
    else if (ch === "{") depth++;
    else if (ch === "}" && --depth === 0) {
      try {
        const value = JSON.parse(cleaned.slice(start, i + 1));
        return value && typeof value === "object" && typeof value.action === "string" ? value : undefined;
      } catch {
        return undefined;
      }
    }
  }
  return undefined;
}

export const _test = { parseAction, IRREVERSIBLE, ADD_TO_CART, SECRET_FIELD, normalize: (s: string) => normalize(s) };

function normalize(text: string): string {
  return text.normalize("NFKC").replace(/[\s​]+/g, "").toLowerCase();
}

function elementLine(snapshot: string, ref: string): string | undefined {
  const match = snapshot.match(new RegExp(`^\\[${ref}\\] .*$`, "m"));
  return match?.[0];
}

function describeElement(line: string | undefined, ref: string): string {
  if (!line) return ref;
  const body = line.replace(/^\[e\d+\]\s*/, "");
  return body.length > 70 ? body.slice(0, 69) + "…" : body;
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

async function chat(llm: LlmConfig, messages: { role: string; content: string }[], signal?: AbortSignal): Promise<string> {
  const body: Record<string, unknown> = { model: llm.model, messages, max_tokens: 1200 };
  if (llm.local) {
    body.temperature = 0;
    body.chat_template_kwargs = { enable_thinking: false };
  }
  const timeout = AbortSignal.timeout(60_000);
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
  return String(data?.choices?.[0]?.message?.content ?? "");
}

export async function runBrowserTask(options: TaskOptions): Promise<TaskResult> {
  const { goal, worker, session, llm, signal } = options;
  const maxSteps = options.maxSteps ?? 20;
  const deadline = Date.now() + (options.timeBudgetMs ?? 150_000);
  const started = Date.now();
  const progress = options.onProgress ?? (() => undefined);
  const steps: string[] = [];
  let llmMs = 0;
  let browserMs = 0;
  let page: Record<string, any> = {};
  let snapshot = "(no page is open yet: use search, or open a URL written in the goal)";
  let lastError = "";
  const repeats = new Map<string, number>();

  const browser = async (command: string) => {
    const t = Date.now();
    try {
      return await worker.request(command, session, signal);
    } finally {
      browserMs += Date.now() - t;
    }
  };
  const quote = (text: string) => JSON.stringify(text);
  const adopt = (result: Record<string, any>) => {
    page = result;
    if (typeof result.snapshot === "string") snapshot = result.snapshot;
  };
  const finish = async (status: TaskStatus, extra: Partial<TaskResult>): Promise<TaskResult> => {
    let screenshot: string | undefined;
    if (page.url) {
      try {
        screenshot = (await browser("screenshot")).path;
      } catch {}
    }
    const body = snapshot.split("\n---\n").slice(1).join("\n---\n");
    return {
      status, answer: "", evidence: [], steps, url: page.url, title: page.title, screenshot,
      page_excerpt: body.slice(0, 1500), elapsed_ms: Date.now() - started, llm_ms: llmMs, browser_ms: browserMs,
      ...extra,
    };
  };

  if (options.url) {
    progress(`🌐 open ${shortUrl(options.url)}`);
    try {
      adopt(await browser(`open ${quote(options.url)}`));
      steps.push(`open ${options.url} → ${page.title || shortUrl(page.url)}`);
    } catch (error) {
      lastError = `open ${options.url} failed: ${(error as Error).message}`;
      steps.push(lastError);
    }
  } else {
    try {
      adopt(await browser("snapshot"));
    } catch {}
  }

  for (let step = 1; step <= maxSteps; step++) {
    signal?.throwIfAborted();
    if (Date.now() > deadline) break;
    const history = steps.length ? steps.map((s, i) => `${i + 1}. ${s}`).join("\n") : "(none yet)";
    const view = snapshot.length > MAX_SNAPSHOT_CHARS
      ? snapshot.slice(0, MAX_SNAPSHOT_CHARS) + "\n(… snapshot cut; scroll down for more)" : snapshot;
    const user = `Goal: ${goal}\nConfirmed by the user for irreversible steps: ${options.confirm ? "yes" : "no"}\n` +
      `Step ${step} of at most ${maxSteps}. Steps so far:\n${history}\n` +
      (lastError ? `Last action failed: ${lastError}\n` : "") +
      `\nCurrent page:\n${view}\n\nReply with one JSON object.`;
    let action: Record<string, any> | undefined;
    for (let attempt = 0; attempt < 2 && !action; attempt++) {
      const t = Date.now();
      const messages = [{ role: "system", content: SYSTEM_PROMPT }, { role: "user", content: user }];
      if (attempt) messages.push({ role: "user", content: "Your reply was not one valid JSON action. Reply with ONLY one JSON object from the action list." });
      let reply = "";
      try {
        reply = await chat(llm, messages, signal);
      } catch (error) {
        signal?.throwIfAborted();
        llmMs += Date.now() - t;
        return finish("TASK_FAILED", { reason: `browser planner model failed: ${(error as Error).message}` });
      }
      llmMs += Date.now() - t;
      action = parseAction(reply);
    }
    if (!action) {
      return finish("TASK_FAILED", { reason: "the planner model did not return a valid action twice in a row" });
    }
    lastError = "";
    const kind = String(action.action).toLowerCase();
    const ref = typeof action.ref === "string" ? action.ref.replace(/^@/, "").toLowerCase() : "";
    const line = ref ? elementLine(snapshot, ref) : undefined;

    if (kind === "done") {
      const answer = String(action.answer ?? "").trim();
      const evidence = (Array.isArray(action.evidence) ? action.evidence : [action.evidence])
        .map((e: unknown) => String(e ?? "").trim()).filter(Boolean).slice(0, 5);
      let pageText = snapshot;
      try {
        pageText += "\n" + String((await browser("get text")).text ?? "");
      } catch {}
      const haystack = normalize(pageText);
      const missing = evidence.filter((e: string) => !haystack.includes(normalize(e.replace(/^["'「『]|["'」』]$/g, ""))));
      progress(`✅ done`);
      steps.push(`done`);
      const verified = evidence.length > 0 && missing.length === 0;
      return finish(verified ? "TASK_DONE" : "TASK_UNVERIFIED", {
        answer, evidence, ...(missing.length ? { missing_evidence: missing } : {}),
        ...(verified ? {} : { reason: evidence.length ? "some evidence quotes are not on the page" : "no evidence quoted" }),
      });
    }
    if (kind === "stop") {
      const status = String(action.status ?? "failed").toLowerCase();
      const reason = String(action.reason ?? "").trim();
      const mapped: TaskStatus = status.includes("input") ? "TASK_NEEDS_INPUT"
        : status.includes("confirm") ? "TASK_NEEDS_CONFIRM"
        : status.includes("block") ? "TASK_BLOCKED" : "TASK_FAILED";
      progress(`🛑 ${mapped}: ${reason}`);
      steps.push(`stop ${status}: ${reason}`);
      return finish(mapped, { reason });
    }

    // Loop guard: the same action on the same page three times means it is not working.
    const key = `${kind}|${line ?? ref}|${action.text ?? action.query ?? action.url ?? action.direction ?? action.key ?? ""}|${page.url ?? ""}`;
    const count = (repeats.get(key) ?? 0) + 1;
    repeats.set(key, count);
    if (count >= 3) {
      steps.push(`${kind} ${describeElement(line, ref)} repeated without progress`);
      return finish("TASK_STOPPED", { reason: "the same action kept repeating without changing the page" });
    }
    const repeatNote = count === 2 ? "This exact action was already tried on this page and did not reach the goal; if it fails again the task stops. " : "";

    let command = "";
    let label = "";
    if (kind === "click") {
      if (!line) { lastError = `${ref || "(no ref)"} is not in the current snapshot`; steps.push(`click ${ref} → not on the page`); continue; }
      const name = describeElement(line, ref);
      if (IRREVERSIBLE.test(line) && !options.confirm) {
        steps.push(`click ${name} → not taken (irreversible)`);
        return finish("TASK_NEEDS_CONFIRM", { reason: `the next step is an irreversible action: ${name}. Ask the user; call again with confirm=true only if they agree.` });
      }
      if (ADD_TO_CART.test(line) && !/購物車|購物籃|cart|basket|bag|加入|下單|購買|buy/i.test(goal)) {
        steps.push(`click ${name} → not taken (adds to cart)`);
        return finish("TASK_NEEDS_CONFIRM", { reason: `the next step adds an item to the cart (${name}), which the goal did not ask for.` });
      }
      command = `click ${ref}`;
      label = `🖱️ click ${name}`;
    } else if (kind === "type" || kind === "fill") {
      if (!line) { lastError = `${ref || "(no ref)"} is not in the current snapshot`; steps.push(`type into ${ref} → not on the page`); continue; }
      const text = String(action.text ?? action.value ?? "");
      if (SECRET_FIELD.test(line) && text && !goal.includes(text)) {
        steps.push(`type into ${describeElement(line, ref)} → not taken (secret not given in the goal)`);
        return finish("TASK_NEEDS_INPUT", { reason: `the page asks for ${describeElement(line, ref)}; the goal does not give that value. Ask the user for it.` });
      }
      command = `type ${ref} ${quote(text)}${action.submit ? " --submit" : ""}`;
      label = `⌨️ type ${quote(text)} into ${describeElement(line, ref)}${action.submit ? " + Enter" : ""}`;
    } else if (kind === "select") {
      if (!line) { lastError = `${ref || "(no ref)"} is not in the current snapshot`; continue; }
      command = `select ${ref} ${quote(String(action.option ?? action.value ?? ""))}`;
      label = `🔽 select ${quote(String(action.option ?? action.value ?? ""))} in ${describeElement(line, ref)}`;
    } else if (kind === "press") {
      command = `press ${quote(String(action.key ?? "Enter"))}`;
      label = `⌨️ press ${action.key ?? "Enter"}`;
    } else if (kind === "scroll") {
      const direction = ["down", "up", "top", "bottom"].includes(String(action.direction)) ? String(action.direction) : "down";
      command = `scroll ${direction}`;
      label = `📜 scroll ${direction}`;
    } else if (kind === "back") {
      command = "back";
      label = "⬅️ back";
    } else if (kind === "search") {
      const query = String(action.query ?? "").trim();
      if (!query) { lastError = "search needs a query"; continue; }
      command = `open ${quote(`https://www.google.com/search?hl=zh-TW&gl=tw&q=${encodeURIComponent(query)}`)}`;
      label = `🔎 search ${quote(query)}`;
    } else if (kind === "open") {
      const url = String(action.url ?? "").trim();
      const allowed = url && (goal.includes(url) || url === options.url || /^https:\/\/www\.google\.[a-z.]+\/search\?/.test(url));
      if (!allowed) {
        lastError = `open only accepts a URL written in the goal (${url || "none given"}); use search or click links instead`;
        steps.push(`open ${url} → refused (not in the goal)`);
        continue;
      }
      command = `open ${quote(url)}`;
      label = `🌐 open ${shortUrl(url)}`;
    } else {
      lastError = `unknown action ${kind}`;
      continue;
    }

    progress(label);
    try {
      const before = page.url;
      adopt(await browser(command));
      const moved = page.url && page.url !== before ? ` → ${page.title || shortUrl(page.url)}` : " → same page";
      steps.push(`${label.replace(/^\S+\s/, "")}${moved}`);
      if (repeatNote) lastError = repeatNote + "Try something different or stop.";
    } catch (error) {
      lastError = String((error as Error).message || error).replace(/^\w+Error: /, "");
      steps.push(`${label.replace(/^\S+\s/, "")} → failed: ${lastError.slice(0, 160)}`);
      if (/STALE_REF|not on the page/.test(lastError)) {
        try { adopt(await browser("snapshot")); } catch {}
      }
    }
  }
  return finish("TASK_STOPPED", {
    reason: Date.now() > deadline ? "time budget used up before the goal was reached" : `stopped after ${maxSteps} steps without reaching the goal`,
  });
}
