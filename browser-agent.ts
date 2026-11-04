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
  /** Used for the rest of the task when `llm` twice fails to give a valid action (e.g. a tiny model). */
  fallbackLlm?: LlmConfig;
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
  planner_switched?: string;
}

const SYSTEM_PROMPT = `You operate a real Chrome browser to complete the user's goal. Each turn you get the goal, the steps taken so far and a snapshot of the current page. Interactive elements appear as [eN] role "name"; the other lines are the page's text in page order (a price usually follows its product).

Reply with exactly one JSON object and nothing else. Actions:
{"action":"click","ref":"e12"}
{"action":"type","ref":"e5","text":"words to type","submit":true}
{"action":"select","ref":"e7","option":"visible option text"}
{"action":"press","key":"Enter"}  (Enter, Escape, Tab, ArrowDown, PageDown)
{"action":"scroll","direction":"down"}  (down, up, top, bottom)
{"action":"back"}
{"action":"google","query":"keywords"}  (Google search: only to find a site, or when this site is wrong or broken)
{"action":"open","url":"https://..."}  (a URL from the goal, a site's home page, or a page on a site shown on the current page; never a guessed deep link)
{"action":"done","answer":"the result the goal asks for, with exact names, prices, dates, numbers","evidence":["short quote copied exactly from the page"]}
{"action":"stop","status":"needs_input|needs_confirm|blocked|failed","reason":"why, and what is needed"}

Rules:
- Use only refs from the latest snapshot. If what you need is not listed, scroll or use the site's search box.
- select is only for [eN] select dropdowns; use click for links, buttons, tabs and options.
- To find an item on a site, type into the site's search box with "submit":true in one step.
- Among similar options (suggestions, stations, products), choose the one whose name equals what the goal names; an option with extra words ("-環島", "Pro", "2入組") is a different item.
- Reply done as soon as the page shows what the goal asks for; do not keep browsing. For a list, give the top items with their prices or key facts.
- evidence: 1-3 short quotes copied character for character from the current page.
- The snapshot does not show link addresses: never write a URL you did not see (the URL line at the top is the current page's). Name the item instead, or open it and report its URL.
- Never type passwords, OTP/verification codes, card numbers or personal data unless the goal gives them. At a login or verification wall the goal cannot pass, stop with needs_input and list the fields the page asks for.
- Never buy, pay, check out, place or submit an order, delete, unsubscribe or log out unless the goal asks for it and says it is confirmed; otherwise stop with needs_confirm. Add to cart only when the goal asks for it.
- CAPTCHA, "unusual traffic" or app-only pages: stop with blocked.
- If an action leads to a login, verification or error page, do not go back and retry it: stop (needs_input or blocked) and say what the page shows.
- If the page is an error page or the wrong site, use google.
- Do not repeat an action that did not change the page; try something else.
- Write answer and reason in the language of the goal.`;

const IRREVERSIBLE = /結帳|立即購買|直接購買|馬上購買|確認購買|立即訂購|付款|下單|送出訂單|確認訂單|提交訂單|完成訂單|刪除|登出|取消訂閱|退訂|check ?out|buy now|place order|pay now|purchase|delete|log ?out|sign ?out|unsubscribe/i;
const ADD_TO_CART = /加入購物車|放入購物車|加入購物籃|add to (cart|bag|basket)|\(icon: cart\)/i;
const SECRET_FIELD = /type=password|密碼|password|passcode|驗證碼|otp|one-time|一次性|卡號|card number|cvv|cvc|安全碼/i;
const MAX_SNAPSHOT_CHARS = 11000;

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

function parseAction(text: string): Record<string, any> | undefined {
  const cleaned = text.replace(/<think>[\s\S]*?<\/think>/g, "").replace(/```(?:json)?/g, "");
  // The action may follow some prose; try every object start until one parses.
  for (let start = cleaned.indexOf("{"); start >= 0; start = cleaned.indexOf("{", start + 1)) {
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
        const candidate = cleaned.slice(start, i + 1);
        for (const attempt of [candidate, escapeControlInStrings(candidate)]) {
          try {
            const value = JSON.parse(attempt);
            if (value && typeof value === "object" && typeof value.action === "string") return value;
          } catch {}
        }
        break;
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

/** open takes a URL from the goal or the start URL, a Google search, any site's home page,
 * or a page on a site the goal, start URL or current page names (a search result shows its
 * host). Refused: a deep link the planner guessed on a site nothing mentions. */
function openAllowed(url: string, goal: string, start: string | undefined, page: string): boolean {
  let u: URL;
  try {
    u = new URL(url);
  } catch {
    return false;
  }
  if (u.protocol !== "https:" && u.protocol !== "http:") return false;
  if (goal.includes(url) || url === start) return true;
  if (/^www\.google\.[a-z.]+$/.test(u.hostname) && u.pathname === "/search") return true;
  if (u.pathname === "/" && !u.search) return true;
  const host = u.hostname.toLowerCase().replace(/^www\./, "");
  return [goal, start ?? "", page].some((text) => text.toLowerCase().includes(host));
}

function fingerprint(text: string): string {
  let hash = 0;
  for (let i = 0; i < text.length; i++) hash = (Math.imul(hash, 31) + text.charCodeAt(i)) | 0;
  return (hash >>> 0).toString(36);
}

/** "506/5584px" from the snapshot's "Scroll: 506/5584px (viewport 633px)" line. */
function scrollPosition(snapshot: string): string {
  return snapshot.match(/^Scroll: (\d+\/\d+px)/m)?.[1] ?? "";
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

async function chat(llm: LlmConfig, messages: { role: string; content: string }[], signal?: AbortSignal,
  timeoutMs = 60_000): Promise<string> {
  const body: Record<string, unknown> = { model: llm.model, messages, max_tokens: 2000 };
  if (llm.local) {
    body.temperature = 0;
    body.chat_template_kwargs = { enable_thinking: false };
  }
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
  return String(data?.choices?.[0]?.message?.content ?? "");
}

export async function runBrowserTask(options: TaskOptions): Promise<TaskResult> {
  const { goal, worker, session, signal } = options;
  let llm = options.llm;
  let switched: string | undefined;
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
        screenshot = (await worker.request("screenshot", session, AbortSignal.timeout(10_000))).path;
      } catch {}
    }
    const body = snapshot.split("\n---\n").slice(1).join("\n---\n");
    return {
      status, answer: "", evidence: [], steps, url: page.url, title: page.title, screenshot,
      ...(switched ? { planner_switched: switched } : {}),
      page_excerpt: body.slice(0, 1500), elapsed_ms: Date.now() - started, llm_ms: llmMs, browser_ms: browserMs,
      ...extra,
    };
  };

  try {
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
      let lastReply = "";
      let callError = "";
      for (let attempt = 0; attempt < 2 && !action; attempt++) {
        const t = Date.now();
        const messages = [{ role: "system", content: SYSTEM_PROMPT }, { role: "user", content: user }];
        if (attempt) messages.push({ role: "user", content: `Your reply was not one valid JSON action (it began: ${JSON.stringify(lastReply.slice(0, 160))}). Reply with ONLY one JSON object from the action list; write line breaks inside strings as \\n.` });
        let reply = "";
        try {
          reply = await chat(llm, messages, signal, Math.max(5_000, Math.min(60_000, deadline - Date.now() + 15_000)));
        } catch (error) {
          signal?.throwIfAborted();
          llmMs += Date.now() - t;
          callError = `browser planner model failed: ${(error as Error).message}`;
          break;
        }
        llmMs += Date.now() - t;
        lastReply = reply;
        action = parseAction(reply);
        if (process.env.PI_BROWSER_AGENT_TRACE) progress(`· model: ${reply.replace(/\s+/g, " ").slice(0, 300)}`);
      }
      if (!action && options.fallbackLlm && options.fallbackLlm.model !== llm.model && !switched) {
        // The conversation's model cannot drive the browser (seen: a 1.7B model answering with
        // <tool_call> markup): continue the task on the known-good planner.
        switched = `${llm.model} → ${options.fallbackLlm.model}`;
        llm = options.fallbackLlm;
        progress(`🔁 planner ${switched}`);
        step--;
        continue;
      }
      if (!action) {
        return finish("TASK_FAILED", { reason: callError || "the planner model did not return a valid action twice in a row" });
      }
      lastError = "";
      let kind = String(action.action).toLowerCase();
      const ref = typeof action.ref === "string" ? action.ref.replace(/^@/, "").toLowerCase() : "";
      const line = ref ? elementLine(snapshot, ref) : undefined;
      // Models say "select" for "choose this result": on anything but a dropdown that is a click.
      if (kind === "select" && line && !/^\[e\d+\] select /.test(line)) kind = "click";
      if (kind === "search") kind = "google";

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
        // A URL in the answer must be one the page links to (or the page itself, or one the goal
        // gave): the snapshot shows no addresses, and the planner made up 104 job links.
        const urls = [...new Set(answer.match(/https?:\/\/[^\s"'<>()（）\]｜|，、]+/g) ?? [])].map((u) => u.replace(/[.,;:!?。]+$/, ""));
        if (urls.length) {
          let links = "";
          try {
            links = String((await browser("get links")).text ?? "");
          } catch {}
          const known = new Set([...links.split("\n"), page.url ?? "", options.url ?? ""].map((u) => u.replace(/\/$/, "")));
          for (const url of urls) {
            if (!known.has(url.replace(/\/$/, "")) && !goal.includes(url) && !pageText.includes(url)) missing.push(`link not on the page: ${url}`);
          }
        }
        progress(`✅ done`);
        steps.push(`done`);
        const verified = evidence.length > 0 && missing.length === 0;
        return finish(verified ? "TASK_DONE" : "TASK_UNVERIFIED", {
          answer, evidence, ...(missing.length ? { missing_evidence: missing } : {}),
          ...(verified ? {} : { reason: !evidence.length ? "no evidence quoted"
            : missing.some((m: string) => m.startsWith("link not on the page")) ? "the answer has links that are not on the page"
            : "some evidence quotes are not on the page" }),
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
      // Scrolling and keys move through one URL, so for them the page is what the snapshot
      // shows: reading a long list scroll by scroll is progress, the end of the page is not.
      const where = kind === "scroll" || kind === "press" ? `|${fingerprint(snapshot)}` : "";
      const key = `${kind}|${line ?? ref}|${action.text ?? action.query ?? action.url ?? action.direction ?? action.key ?? ""}|${page.url ?? ""}${where}`;
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
      } else if (kind === "google") {
        const query = String(action.query ?? "").trim();
        if (!query) { lastError = "google needs a query"; continue; }
        command = `open ${quote(`https://www.google.com/search?hl=zh-TW&gl=tw&q=${encodeURIComponent(query)}`)}`;
        label = `🔎 google ${quote(query)}`;
      } else if (kind === "open") {
        const url = String(action.url ?? "").trim();
        if (!openAllowed(url, goal, options.url, snapshot)) {
          lastError = `open does not take a guessed link on a site that is not in the goal or on this page (${url || "none given"}); open the site's home page, search, or click links instead`;
          steps.push(`open ${url} → refused (guessed link)`);
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
        const shown = fingerprint(snapshot);
        adopt(await browser(command));
        // Say what changed: "same page" after a working scroll read as "scroll does nothing".
        const moved = page.url && page.url !== before ? ` → ${page.title || shortUrl(page.url)}`
          : fingerprint(snapshot) === shown ? " → no visible change"
          : (kind === "scroll" || kind === "press") && scrollPosition(snapshot) ? ` → now at ${scrollPosition(snapshot)}`
          : " → page updated";
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
  } catch (error) {
    // The tool's hard limit (a TimeoutError on the signal) ends the task like the budget
    // does: report the steps so far, not an error with nothing in it. A cancel still throws.
    if (signal?.aborted && (signal.reason as Error | undefined)?.name === "TimeoutError") {
      return finish("TASK_STOPPED", { reason: "time limit reached before the goal; the steps show how far it got" });
    }
    throw error;
  }
}
