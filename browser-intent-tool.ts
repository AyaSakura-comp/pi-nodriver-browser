import { readFileSync } from "node:fs";
import { Type } from "typebox";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { runBrowserTask, type LlmConfig, type TaskResult, type WorkerLike } from "./browser-agent.ts";

// Planner model when the conversation's own model cannot be called over /chat/completions,
// and the fallback when it cannot produce browser actions.
const DEFAULT_ENDPOINT = "http://127.0.0.1:8090/v1/chat/completions";
const DEFAULT_MODEL = "qwen3.8-flash";

/** OpenAI-style chat endpoint for a Pi model, when its API has one. */
function chatEndpoint(api: string | undefined, baseUrl: string | undefined): string | undefined {
  if (!baseUrl) return undefined;
  const base = baseUrl.replace(/\/+$/, "");
  if (api === "openai-completions") return `${base}/chat/completions`;
  if (api === "anthropic-messages") return base.endsWith("/v1") ? `${base}/chat/completions` : `${base}/v1/chat/completions`;
  return undefined;
}

function isLocal(url: string): boolean {
  try {
    return ["127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"].includes(new URL(url).hostname);
  } catch {
    return false;
  }
}

const DEFAULT_LLM: LlmConfig = { endpoint: DEFAULT_ENDPOINT, model: DEFAULT_MODEL, local: isLocal(DEFAULT_ENDPOINT) };

async function plannerFor(ctx: ExtensionContext): Promise<{ llm: LlmConfig; note: string }> {
  const model = ctx.model;
  if (model && chatEndpoint(model.api, model.baseUrl)) {
    let baseUrl = model.baseUrl;
    let headers: Record<string, string> = { ...(model.headers || {}) };
    try {
      const auth = await ctx.modelRegistry.getApiKeyAndHeaders(model);
      if (auth.ok) {
        if (auth.baseUrl) baseUrl = auth.baseUrl;
        if (auth.headers) headers = { ...headers, ...auth.headers } as Record<string, string>;
        if (auth.apiKey && auth.apiKey !== "not-needed" && !headers.Authorization && !headers.authorization) {
          headers.Authorization = `Bearer ${auth.apiKey}`;
        }
      }
    } catch {}
    const endpoint = chatEndpoint(model.api, baseUrl);
    if (endpoint) {
      return { llm: { endpoint, model: model.id, headers, local: isLocal(endpoint) },
        note: `${model.provider ?? ""}/${model.id}`.replace(/^\//, "") };
    }
  }
  return { llm: DEFAULT_LLM, note: `${DEFAULT_MODEL} (local default)` };
}

const DESCRIPTION = `Live browser autopilot on the shared, logged-in Chrome. Give a goal (and a start URL when you have one); it opens the page, clicks, types, selects and scrolls by itself, then returns:
- TASK_DONE: answer, with evidence quotes that were found on the final page.
- TASK_UNVERIFIED: an answer whose quotes were not all found on the page; check it before relying on it.
- TASK_NEEDS_INPUT: a login or verification step needs values the goal does not give; reason lists the fields. Ask the user; never invent credentials.
- TASK_NEEDS_CONFIRM: the next step is irreversible (buy, pay, submit an order, delete, log out, or add to cart when not asked). Ask the user; call again with confirm=true only if they agree.
- TASK_BLOCKED: CAPTCHA, anti-bot or app-only wall. Tell the user; do not retry it or work around it (no verification clicks, curl, site APIs or proxies).
- TASK_STOPPED / TASK_FAILED: not reached; steps show how far it got.
The result names a screenshot of where it ended; the image itself is attached when the task did not finish.`;

const GUIDELINES = [
  "When the user asks to use the browser or to act on a website (用瀏覽器、打開網站、幫我點/登入/填表/操作), call browser_intent, not research.",
  "browser_intent takes {url?, goal}: put what to find or do and every constraint in the goal (e.g. \"price of the single unit, not a bundle; do not add to cart\"). The autopilot does the clicking; one call per user goal.",
  "url can be any start page: the exact page when you have it, otherwise the site's home page or a Google search URL. Omit url to continue on the page this conversation's last browser task ended on (follow-ups, screenshots).",
  "When the user gives login details or a one-time code for a page the previous call stopped at, call again without url and put them in the goal; they are typed only into the fields that ask for them.",
  "TASK_DONE answers are already checked against the page: answer from them right away instead of calling again to re-check.",
  "To send the final screenshot to the user, include the returned [[image: <path>]] marker in your reply.",
];

// What the conversation's model should do with each stop, said where it reads the result
// (seen: after TASK_BLOCKED on Dcard, Qwen asked the autopilot to click the Cloudflare box,
// then tried curl with a browser User-Agent and a reader proxy).
const NEXT: Record<string, string> = {
  TASK_BLOCKED: "Next: tell the user this site blocks automated browsing. Do not ask to click its verification, call browser_intent on it again, or fetch it another way (curl, its API, proxies or reader services).",
  TASK_NEEDS_INPUT: "Next: ask the user for the fields above. Never guess or invent credentials or codes.",
  TASK_NEEDS_CONFIRM: "Next: ask the user. Call again with confirm=true only after they agree to this exact step.",
};

const HEAD: Record<string, string> = {
  TASK_DONE: "✅ 任務完成（證據已在頁面上找到）",
  TASK_UNVERIFIED: "🔍 已有答案，但部分證據在頁面上找不到",
  TASK_NEEDS_INPUT: "🔑 需要使用者提供登入或驗證資料",
  TASK_NEEDS_CONFIRM: "⚠️ 下一步是不可逆操作，需要使用者確認",
  TASK_BLOCKED: "🛑 網站擋下自動瀏覽",
  TASK_STOPPED: "⏸️ 未完成就停止",
  TASK_FAILED: "❌ 任務失敗",
};

function summary(result: TaskResult, goal: string, planner: string): string {
  const lines = [`${HEAD[result.status] ?? result.status}：${goal}`];
  if (result.answer) lines.push("", result.answer);
  if (result.reason) lines.push("", `原因：${result.reason}`);
  if (NEXT[result.status]) lines.push("", NEXT[result.status]);
  if (result.steps.length) lines.push("", "步驟：", ...result.steps.map((s, i) => `${i + 1}. ${s}`));
  if (result.url) lines.push("", `目前頁面：${result.title || ""} ${result.url}`);
  if (result.screenshot) lines.push(`截圖：[[image: ${result.screenshot}]]`);
  const waited = result.wait_ms > 2000 ? `，其中 ${(result.wait_ms / 1000).toFixed(0)}s 在等模型空檔` : "";
  lines.push(`（${(result.elapsed_ms / 1000).toFixed(1)}s${waited}；規劃模型 ${planner}）`);
  return lines.join("\n");
}

export default function registerBrowserIntent(pi: ExtensionAPI, worker: WorkerLike) {
  // One window per conversation: parallel calls would drive the same page at once, so they
  // queue. The window outlives the Pi process (see session_shutdown in index.ts).
  const queues = new Map<string, Promise<unknown>>();

  pi.registerTool({
    name: "browser_intent",
    label: "Browser autopilot",
    description: DESCRIPTION,
    promptGuidelines: GUIDELINES,
    parameters: Type.Object({
      url: Type.Optional(Type.String({ description: "Start page (exact page, site home page, or a Google search URL). Omit to continue on this conversation's current page." })),
      goal: Type.String({ description: "What to find or do, with every constraint (e.g. \"price and stock of the single-unit DGX Spark, not a bundle; do not add to cart\")." }),
      confirm: Type.Optional(Type.Boolean({ description: "Only when the user explicitly approved the irreversible step a previous TASK_NEEDS_CONFIRM stopped at." })),
    }),
    async execute(_id, params, signal, onUpdate, ctx) {
      const goal = params.goal.trim();
      if (!goal) throw new Error("browser_intent needs a goal");
      const url = params.url?.trim() || undefined;
      const { llm, note } = await plannerFor(ctx);
      const started = Date.now();
      const lines = [`🎯 瀏覽任務：${goal}`];
      // piweb shows one progress row per update from details.message (transport/web.ts).
      const show = (message: string) => onUpdate?.({
        content: [{ type: "text", text: lines.length > 14 ? [lines[0], "…", ...lines.slice(-12)].join("\n") : lines.join("\n") }],
        details: { message },
      });
      show(lines[0]);
      const session = `intent-${ctx.sessionManager.getSessionId()}`;
      const task = (queues.get(session) ?? Promise.resolve()).then(() => runBrowserTask({
        goal, url, confirm: params.confirm === true, session, worker, llm, fallbackLlm: DEFAULT_LLM,
        // The hard limit counts from the start of this task, not from the wait in the queue. The
        // task ends itself first: after 150 s of its own work, or 300 s on the clock when the
        // engine was busy with other requests.
        signal: AbortSignal.any([...(signal ? [signal] : []), AbortSignal.timeout(320_000)]),
        onProgress: (line) => {
          const stamped = `${((Date.now() - started) / 1000).toFixed(1)}s ${line}`;
          lines.push(stamped);
          show(stamped);
        },
      }));
      const settled = task.catch(() => undefined);
      queues.set(session, settled);
      const result = await task.finally(() => {
        if (queues.get(session) === settled) queues.delete(session);
      });
      const details = { ...result, planner: result.planner_switched ? `${note} → ${DEFAULT_MODEL} (fallback)` : note };
      // The summary says everything the model needs (the same fields as JSON after it doubled the
      // result's size); details carry the fields for the UI. A finished task's answer was checked
      // against the pages, so its screenshot is named, not attached: an image costs the
      // conversation's model an encode and its tokens.
      const content: any[] = [{ type: "text" as const, text: summary(result, goal, note) }];
      if (result.screenshot && result.status !== "TASK_DONE") {
        try {
          content.push({ type: "image" as const, data: readFileSync(result.screenshot).toString("base64"), mimeType: "image/jpeg" });
        } catch {}
      }
      return { content, details };
    },
  });
}
