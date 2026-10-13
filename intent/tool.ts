import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { ensureIntentService, failureAdvice, streamFailure } from "./resolver.ts";
import { Type } from "typebox";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// Intent-level browser tool: the agent says WHAT it wants; the local intent service
// (laya-browser-intent, :8011) grounds it to one element via exact match / e5 + Laya / a
// shortlist judge, executes it on the shared nodriver browser, and returns the verified
// post-action page state. No @refs or raw DOM ever enter the agent context.
const SERVICE = process.env.LAYA_INTENT_URL || "http://127.0.0.1:8011";
// Default: goal-only. The agent hands over {url?, goal}; the service's planner decides every
// open/click/fill/scroll itself and the tool returns the outcome plus the planner's event log.
// LAYA_INTENT_MODE=full restores the single-step actions (open/click/plan/...) for debugging.
export const FULL = process.env.LAYA_INTENT_MODE === "full";
const ACTIONS = ["task", "plan", "open", "click", "fill", "select", "scroll", "read", "observe", "back", "screenshot", "resolve"];
// Planner APIs the router can call with the session's own model over /chat/completions.
// Anthropic serves an OpenAI-compatible endpoint under /v1; Codex/Responses/Google do not fit.
export function plannerEndpoint(api: string | undefined, baseUrl: string | undefined): string | undefined {
  if (!baseUrl) return undefined;
  const base = baseUrl.replace(/\/+$/, "");
  if (api === "openai-completions") return `${base}/chat/completions`;
  if (api === "anthropic-messages") return base.endsWith("/v1") ? `${base}/chat/completions` : `${base}/v1/chat/completions`;
  return undefined;
}
const RESOLVE_HELP = 'Recovery: action "resolve" {target?: last error/status} checks the router, starts a missing local router through the restart-service hub, reconnects the worker and observes the page. Returns RESOLVED/RESOLVE_BLOCKED and common failure guidance. Never replays the original action, resets shared Chrome, or bypasses URL/auth/confirmation guards.';

const DESCRIPTION = `Intent-level live browser (shares the nodriver Chrome session). You never see DOM or @refs: describe the element in words and the tool finds, clicks/fills, and verifies it.
BEST DEFAULT: use action "task" {url, goal} with an exact user-supplied URL or an exact search-result URL. The browser service drives the site itself step by step (small fast planner + grounding + safety gate + verification) and returns TASK_DONE (verified, with the goal-relevant page text), TASK_UNVERIFIED (check it), TASK_FAILED, TASK_STOPPED or TASK_NEEDS_CONFIRM. Put every constraint in the goal (e.g. "single unit, not a bundle; do not add to cart"). If an unindexed deep link is not returned and not provided by the user, start from the closest official parent URL that search did return and navigate through visible links to the requested item. You then only check the result and answer.
ALTERNATIVE: use action "plan" with steps [...] to run a whole predictable sequence (open → fill → click → read) in ONE call. Targets are semantic, so you do not need to see a page before describing what to click on it. The plan stops early and reports the step that needs you (NO_MATCH / AMBIGUOUS / NEEDS_CONFIRM). You do NOT need to read search results before clicking: put the selection criteria in the click target and the tool picks the matching item (or stops with NO_MATCH).
Example (one call for a whole shopping lookup):
{"action":"plan","steps":[{"action":"open","url":"<exact url>"},{"action":"fill","target":"search box","value":"DGX Spark","submit":true},{"action":"click","target":"in-stock single-unit DGX Spark, not a 2-pack or bundle"},{"action":"read","target":"price and shipping"}]}
Actions:
- plan {steps:[{action,url?,target?,value?,submit?}, ...]}: run steps in order; ends with the last step's result (use a final read step to get the answer text).
- open {url}: navigate only to an exact user-supplied URL or an exact URL returned by successful google_search or web_search. Returns url/title and a screenshot.
- Every CLICKED/FILLED/SELECTED/OPENED result includes page_excerpt (first visible text of the new page state): use it to decide the next step instead of an extra read.
- click {target}: target = natural-language description of ONE element, including distinguishing constraints (e.g. "單台 DGX Spark 主機，不要兩入組", "加入購物車 button"). Returns CLICKED (with resulting url/title/headings/screenshot), NO_MATCH (nothing on the page does it: rephrase, read, or report unavailable), AMBIGUOUS (numbered options: call again with pick=N), or NEEDS_CONFIRM (purchase/checkout/delete/logout: only repeat with confirm=true if the user explicitly asked).
- fill {target, value, submit}: type into the described input; submit=true presses Enter.
- select {target, value}: choose option text value in the described dropdown.
- read {target?}: page text (optionally only the parts relevant to target) for answering questions such as price or stock.
- screenshot: capture and return an instant screenshot of the current page. Call this whenever the user asks for a screenshot or to see the current webpage (e.g. 截圖給我看, 截圖, 看當前畫面).
- observe: current url/title/headings.
- back: return to the previous page URL this tool visited (never construct URLs yourself).
CLICKED_UNCONFIRMED / FILLED_UNCONFIRMED mean the action ran but no page change was detectable; verify with read if it matters.`;

const GOAL_DESCRIPTION = `Live browser autopilot (shares the logged-in nodriver Chrome). Give it a start URL and a goal; a planner inside the browser service opens the page, clicks, types, selects and scrolls by itself, verifies the result against the page text, and returns:
- TASK_DONE: verified; text = the goal-relevant page text, evidence = why it counts as done.
- TASK_UNVERIFIED: the planner thinks it is done but the page text does not show it; check text before answering.
- TASK_STOPPED / TASK_FAILED: not reached; steps and events say how far it got and why.
- TASK_NEEDS_CONFIRM: an irreversible step (buy, pay, submit order, delete, log out) was reached and NOT taken.
Every result has events (what the planner did, step by step, with timings) and a screenshot of where it ended.
url must be an exact URL from the user or from search results; a Google search URL (https://www.google.com/search?q=...) also works as a start. Omit url to continue from the page this conversation's last task ended on (e.g. a follow-up question about the same page, or "take a screenshot").`;
const GOAL_GUIDELINES = [
  "browser_intent takes only {url, goal}: describe the destination and every constraint, never the clicks. One call per user goal; the planner does the navigation.",
  "If the exact deep link was not given by the user or returned by search, start from the closest official page or a Google search URL and say in the goal what to navigate to. Never guess deep URLs.",
  "TASK_DONE means the text was already checked against the page by an independent verifier and the screenshot is attached: answer from text right away. Do not call again just to look at or re-confirm the same answer.",
  "For a follow-up about the page already open (another detail, a screenshot), call again WITHOUT url and with the new goal.",
  "On TASK_STOPPED/TASK_FAILED/TASK_UNVERIFIED, read events and text, then either restate the goal more precisely once or report what was found. Do not loop.",
  "Set confirm=true only when the user explicitly asked for the irreversible step that TASK_NEEDS_CONFIRM stopped at.",
];

export default function (pi: ExtensionAPI, ensureBrowser?: () => Promise<void>) {
  // Close this session's browser tab(s) when the pi session ends; otherwise every run leaks a tab
  // toward the worker's 20-tab cap.
  pi.on("session_shutdown", async (_event, ctx) => {
    try {
      await fetch(`${SERVICE}/act`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ session: `intent-${ctx.sessionManager.getSessionId()}`, action: "cleanup" }),
        signal: AbortSignal.timeout(5000),
      });
    } catch {}
  });

  pi.registerTool({
    name: "browser_intent",
    label: "Browser (intent → Laya → click)",
    description: FULL ? DESCRIPTION + "\n" + RESOLVE_HELP : GOAL_DESCRIPTION,
    promptSnippet: FULL
      ? "Operate web pages by intent; resolve diagnoses and safely repairs browser failures"
      : "Hand a live-browser goal (and start URL) to an autopilot that does the clicking and reports back",
    promptGuidelines: FULL ? [
      "Prefer ONE task call {url, goal} using an exact user-supplied URL or exact search-result URL. If an exact deep link is absent and not provided by the user, use the closest official parent URL returned by search and navigate through visible links; never reuse or construct the deep URL.",
      "When a page is opened or read, examine the '頁面候選跳轉連結' list in the output. To follow a link from the current page, use action: 'open' with pick: <number> (e.g. pick: 1) or copy the exact candidate URL. Never guess, infer, or construct deep URLs yourself.",
      "To search within the browser, open a Google search URL directly: action: 'open', url: 'https://www.google.com/search?q=<query>'.",
      "When the user asks for a screenshot or to see the current webpage (例如要求傳截圖、截圖給我看、看畫面、看網頁截圖或 send screenshot), use action: 'screenshot'. It captures the current browser view immediately without re-opening or navigation.",
      "Use browser_intent for interactive web tasks. Describe targets by visible wording and constraints; never invent refs, selectors or coordinates.",
      "After CLICKED, trust the returned url/title/screenshot as the new page state and decide the next intent from it.",
      "On NO_MATCH, do not repeat the same target: rephrase once with page wording, use read, or conclude the element is not available.",
      "Never set confirm=true unless the user explicitly asked for that irreversible action.",
      'For browser_intent transport/errors, call action "resolve" with target=last error; inspect current state before retrying. Never guess URLs or blindly replay actions.',
    ] : GOAL_GUIDELINES,
    parameters: FULL ? Type.Object({
      action: Type.Union(ACTIONS.map((a) => Type.Literal(a))),
      steps: Type.Optional(Type.Array(Type.Object({
        action: Type.Union([Type.Literal("open"), Type.Literal("click"), Type.Literal("fill"), Type.Literal("select"), Type.Literal("scroll"), Type.Literal("read"), Type.Literal("observe"), Type.Literal("back"), Type.Literal("screenshot")]),
        url: Type.Optional(Type.String()),
        target: Type.Optional(Type.String()),
        value: Type.Optional(Type.String()),
        submit: Type.Optional(Type.Boolean()),
      }), { maxItems: 8, description: "plan: ordered steps" })),
      url: Type.Optional(Type.String({ description: "open: exact URL (from user, search, page candidates, or a search engine query)" })),
      target: Type.Optional(Type.String({ description: "click/fill/scroll: description of one element; read: what to look for; resolve: last failure message/status" })),
      value: Type.Optional(Type.String({ description: "fill: text to enter; scroll: direction or offset (e.g. top, bottom, down 600)" })),
      submit: Type.Optional(Type.Boolean({ description: "fill: press Enter after typing" })),
      goal: Type.Optional(Type.String({ description: "task: the full goal with all constraints; otherwise overall task for disambiguation" })),
      max_steps: Type.Optional(Type.Integer({ minimum: 1, maximum: 12, description: "task: step limit (default 8)" })),
      pick: Type.Optional(Type.Integer({ minimum: 1, maximum: 20, description: "Option number after AMBIGUOUS, or candidate link index to navigate to with action: 'open'" })),
      confirm: Type.Optional(Type.Boolean({ description: "Only for user-requested irreversible actions" })),
    }) : Type.Object({
      url: Type.Optional(Type.String({ description: "Start page: an exact URL from the user or from search results (a Google search URL is fine). Omit to continue on this conversation's current page." })),
      goal: Type.String({ description: "The destination: what to find or do, with every constraint (e.g. \"price and stock of the single-unit DGX Spark, not a bundle; do not add to cart\")." }),
      confirm: Type.Optional(Type.Boolean({ description: "Only when the user explicitly asked for the irreversible step a previous TASK_NEEDS_CONFIRM stopped at." })),
    }),
    async execute(_id, rawParams, signal, onUpdate, ctx) {
      const params: any = FULL ? rawParams : { action: "task", url: (rawParams as any).url, goal: (rawParams as any).goal, confirm: (rawParams as any).confirm };
      const session = `intent-${ctx.sessionManager.getSessionId()}`;
      const boundedSignal = AbortSignal.any([...(signal ? [signal] : []), AbortSignal.timeout(120000)]);
      const result = (data: Record<string, unknown>) => ({
        content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }], details: data,
      });
      try {
        await ensureIntentService(SERVICE, async () => {
          try {
            const res = await fetch(`${SERVICE}/health`, { signal: AbortSignal.any([boundedSignal, AbortSignal.timeout(3000)]) });
            return res.ok && (await res.json()).ok === true;
          } catch { boundedSignal.throwIfAborted(); return false; }
        }, async () => {
          onUpdate?.({ content: [{ type: "text", text: "🔧 Browser resolver: starting missing intent router through restart-service hub" }] });
          const repair = await pi.exec("bash", [join(homedir(), ".hermes/skills/restart-service/scripts/resolve-browser.sh")], { signal: boundedSignal, timeout: 60000 });
          if (repair.code !== 0) throw new Error(`Browser hub repair failed: ${repair.stderr || repair.stdout}`);
        });
        await ensureBrowser?.();
        boundedSignal.throwIfAborted();
        if (params.action === "resolve") {
          // Observation only: no navigation, reload, original click/task, or guard reset.
          const res = await fetch(`${SERVICE}/act`, {
            method: "POST", headers: { "content-type": "application/json" },
            body: JSON.stringify({ session, action: "observe" }), signal: boundedSignal,
          });
          if (!res.ok) throw new Error(`Intent observation HTTP ${res.status}`);
          const state = await res.json();
          return result({ status: state.status === "OBSERVED" ? "RESOLVED" : "RESOLVE_BLOCKED",
            infrastructure: "healthy", state, retry_original: false,
            advice: failureAdvice(params.target || state.message || ""),
            note: "Infrastructure checked; original operation and website success are not implied. No action was replayed." });
        }
      } catch (error) {
        if (boundedSignal.aborted) throw error;
        const message = error instanceof Error ? error.message : String(error);
        return result({ status: "RESOLVE_BLOCKED", phase: "preflight", message, advice: failureAdvice(message), retry_original: false });
      }
      const isPlan = params.action === "plan" && Array.isArray(params.steps) && params.steps.length > 0;
      const isTask = params.action === "task";

      const formatStepDescription = (s: { action: string; target?: string; url?: string; value?: string }): string => {
        const icon = s.action === "open" ? "🌐"
          : s.action === "click" ? "🖱️"
          : s.action === "fill" ? "⌨️"
          : s.action === "select" ? "🔽"
          : s.action === "scroll" ? "📜"
          : s.action === "screenshot" ? "📸"
          : s.action === "read" ? "📖"
          : s.action === "observe" ? "👁️"
          : s.action === "back" ? "⬅️"
          : "⚡";
        let desc = `${icon} ${s.action}`;
        if (s.url) desc += ` ${s.url}`;
        if (s.target) desc += ` "${s.target}"`;
        if (s.value) desc += ` -> "${s.value}"`;
        return desc;
      };

      // 1. Structured plan tracker for step-by-step UI updates
      const planSteps = isPlan
        ? (params.steps || []).map((st: any, i: number) => ({
            index: i + 1,
            action: st.action,
            target: st.target,
            url: st.url,
            value: st.value,
            desc: formatStepDescription(st),
            status: (i === 0 ? "running" : "pending") as "pending" | "running" | "done" | "failed",
            detail: "",
          }))
        : [];

      const renderPlanView = (activeDetail?: string) => {
        const total = planSteps.length;
        const doneCount = planSteps.filter((s) => s.status === "done").length;
        const header = `📋 **[browser_intent 執行計劃]** (進度 ${doneCount}/${total}):`;
        const lines = [header];
        for (const s of planSteps) {
          let icon = "⬜";
          let extra = "";
          if (s.status === "done") {
            icon = "✅";
            if (s.detail) extra = ` (${s.detail})`;
          } else if (s.status === "running") {
            icon = "▶️";
            extra = activeDetail ? ` — ${activeDetail}` : " (執行中...)";
          } else if (s.status === "failed") {
            icon = "❌";
            extra = s.detail ? ` (${s.detail})` : " (中斷)";
          }
          lines.push(`  ${icon} [步驟 ${s.index}/${total}] ${s.desc}${extra}`);
        }
        return lines.join("\n");
      };

      // 2. Initial onUpdate dispatch so the user sees the plan or action immediately
      const singleActionLines: string[] = [];
      if (isPlan) {
        onUpdate?.({
          content: [{ type: "text", text: renderPlanView() }],
          details: { status: "EXECUTING", action: "plan", totalSteps: planSteps.length },
        });
      } else if (isTask) {
        singleActionLines.push(`🎯 **[browser_intent 自主任務]** 目標: 「${params.goal || ""}」`);
        onUpdate?.({
          content: [{ type: "text", text: singleActionLines.join("\n") }],
          details: { status: "EXECUTING", action: "task", goal: params.goal },
        });
      } else {
        const actionDesc = formatStepDescription({
          action: params.action,
          target: params.target,
          url: params.url,
          value: params.value,
        });
        singleActionLines.push(`⏳ [browser_intent] 啟動操作: ${actionDesc}`);
        onUpdate?.({
          content: [{ type: "text", text: singleActionLines.join("\n") }],
          details: { status: "EXECUTING", action: params.action, target: params.target },
        });
      }

      const payload: Record<string, unknown> = { session, ...params };
      const model = (ctx as any).model;
      // Plan with the conversation's own model when its API fits /chat/completions; otherwise
      // (Codex, Responses, Google) leave the router on its local default planner.
      let plannerNote = "local default planner";
      if (model && plannerEndpoint(model.api, model.baseUrl) !== undefined) {
        let baseUrl = model.baseUrl;
        let headers: Record<string, string> = { ...(model.headers || {}) };
        try {
          const auth = await (ctx as any).modelRegistry?.getApiKeyAndHeaders?.(model);
          if (auth?.ok) {
            if (auth.baseUrl) baseUrl = auth.baseUrl;
            if (auth.headers) headers = { ...headers, ...auth.headers };
            if (auth.apiKey && auth.apiKey !== "not-needed" && !headers["Authorization"] && !headers["authorization"]) {
              headers["Authorization"] = `Bearer ${auth.apiKey}`;
            }
          }
        } catch {}
        const endpoint = plannerEndpoint(model.api, baseUrl);
        if (endpoint) {
          payload.model = model.id;
          payload.base_url = endpoint;
          if (Object.keys(headers).length > 0) payload.headers = headers;
          plannerNote = `${model.provider ?? ""}/${model.id}`.replace(/^\//, "");
        }
      } else if (model) {
        plannerNote = `local default planner (${model.api} models cannot plan over /chat/completions)`;
      }
      const events: string[] = [];
      const started = Date.now();
      let data: Record<string, unknown> = {};
      try {
        const streamRes = await fetch(`${SERVICE}/act/stream`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(payload),
          signal: boundedSignal,
        });
        // A missing streaming route cannot have dispatched the page action.
        if (streamRes.status === 404 || streamRes.status === 405) {
          const res = await fetch(`${SERVICE}/act`, {
            method: "POST", headers: { "content-type": "application/json" },
            body: JSON.stringify(payload), signal: boundedSignal,
          });
          if (!res.ok) throw new Error(`Intent HTTP ${res.status}`);
          data = await res.json();
        } else if (streamRes.ok && streamRes.body) {
          const reader = streamRes.body.getReader();
          const decoder = new TextDecoder();
          let buf = "";
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buf += decoder.decode(value, { stream: true });
            const lines = buf.split("\n\n");
            buf = lines.pop() ?? "";
            for (const block of lines) {
              for (const line of block.split("\n")) {
                if (line.startsWith("data: ")) {
                  try {
                    const evt = JSON.parse(line.slice(6));
                    if (evt.type === "progress" && evt.message) {
                      events.push(`${((Date.now() - started) / 1000).toFixed(1)}s ${evt.message}`);
                      if (isPlan) {
                        if (evt.stage === "plan_step") {
                          const stepIdx = (evt.current_step ? evt.current_step - 1 : 0);
                          planSteps.forEach((s, idx) => {
                            if (idx < stepIdx) s.status = "done";
                            else if (idx === stepIdx) s.status = "running";
                            else s.status = "pending";
                          });
                          onUpdate?.({
                            content: [{ type: "text", text: renderPlanView(evt.step_action ? `${evt.step_action} 執行中` : undefined) }],
                            details: evt,
                          });
                        } else if (evt.stage === "plan_step_done") {
                          const cur = planSteps.find((s) => s.status === "running");
                          if (cur) cur.status = "done";
                          onUpdate?.({
                            content: [{ type: "text", text: renderPlanView() }],
                            details: evt,
                          });
                        } else if (evt.stage === "plan_step_failed") {
                          const cur = planSteps.find((s) => s.status === "running");
                          if (cur) {
                            cur.status = "failed";
                            cur.detail = evt.message || "失敗";
                          }
                          onUpdate?.({
                            content: [{ type: "text", text: renderPlanView() }],
                            details: evt,
                          });
                        } else {
                          onUpdate?.({
                            content: [{ type: "text", text: renderPlanView(evt.message) }],
                            details: evt,
                          });
                        }
                      } else {
                        singleActionLines.push(evt.message);
                        const displayText = singleActionLines.length > 12
                          ? [singleActionLines[0], "...", ...singleActionLines.slice(-10)].join("\n")
                          : singleActionLines.join("\n");
                        onUpdate?.({
                          content: [{ type: "text", text: displayText }],
                          details: evt,
                        });
                      }
                    } else if (evt.type === "result") {
                      data = (evt.data || {}) as Record<string, unknown>;
                    }
                  } catch {}
                }
              }
            }
          }
        }
        if (!data || Object.keys(data).length === 0) {
          throw new Error("No data received from stream");
        }
      } catch (error) {
        if (signal?.aborted) throw error;
        data = streamFailure(error);
      }
      if (["ERROR", "NO_MATCH", "AMBIGUOUS", "PICK_EXPIRED", "TASK_FAILED", "TASK_STOPPED", "NEEDS_CONFIRM", "TASK_NEEDS_CONFIRM"].includes(String(data.status))) {
        const advice = failureAdvice(`${data.status} ${data.message || data.hint || ""}`);
        data.resolver = FULL ? { action: "resolve", advice } : { advice };
      }
      if (isTask) {
        data.planner = plannerNote;
        // The agent only sees the final result, so hand it the planner's own event log.
        if (events.length) data.events = events.length > 40 ? [...events.slice(0, 5), `… ${events.length - 35} events omitted …`, ...events.slice(-30)] : events;
      }
      const shot = typeof data.screenshot === "string" ? data.screenshot : undefined;
      const { screenshot: _s, ...rest } = data;

      let humanSummary = "";
      if (data.status === "SCREENSHOT_TAKEN") {
        humanSummary = `📸 **[頁面截圖已擷取]**\n- **當前頁面**: ${data.title ? `「${data.title}」` : ""}\n- **網址**: ${data.url || ""}\n- **截圖檔案**: ${shot || ""}\n- **耗時**: ${data.total_ms || 0}ms`;
      } else if (data.status === "SCROLLED") {
        humanSummary = `📜 **[捲動操作完成]**\n- **目標描述**: ${params.target || "頁面"}\n- **定位元素**: ${data.element || "頁面"}\n- **執行細節**: ${data.detail || "已捲動至目標位置"}\n- **狀態變化**: DOM 變更: ${data.dom_changed ? "是" : "否"} | 頁面跳轉: ${data.navigated ? "是" : "否"}\n- **當前頁面**: ${data.title ? `「${data.title}」` : ""}\n- **網址**: ${data.url || ""}`;
      } else if (data.status === "CLICKED" || data.status === "CLICKED_UNCONFIRMED") {
        const unconfirmed = data.status === "CLICKED_UNCONFIRMED" ? " ⚠️ (未偵測到明顯頁面變化)" : " ✓";
        humanSummary = `🖱️ **[點擊操作完成]**${unconfirmed}\n- **目標描述**: ${params.target || ""}\n- **鎖定元素**: ${data.element || params.target}\n- **決策層級**: ${data.decided_by || "DOM exact"}\n- **狀態變化**: DOM 變更: ${data.dom_changed ? "是" : "否"} | 頁面跳轉: ${data.navigated ? "是" : "否"}\n- **當前頁面**: ${data.title ? `「${data.title}」` : ""}\n- **網址**: ${data.url || ""}`;
      } else if (data.status === "FILLED" || data.status === "FILLED_UNCONFIRMED") {
        const unconfirmed = data.status === "FILLED_UNCONFIRMED" ? " ⚠️ (未偵測到明顯頁面變化)" : " ✓";
        humanSummary = `⌨️ **[填寫操作完成]**${unconfirmed}\n- **目標描述**: ${params.target || ""}\n- **鎖定元素**: ${data.element || params.target}\n- **輸入內容**: "${params.value || ""}" ${params.submit ? "(已按 Enter 提交)" : ""}\n- **當前頁面**: ${data.title ? `「${data.title}」` : ""}\n- **網址**: ${data.url || ""}`;
      } else if (data.status === "OPENED") {
        humanSummary = `🌐 **[已開啟網頁]**\n- **網址**: ${data.url}\n- **標題**: ${data.title || ""}`;
      } else if (data.status === "TASK_DONE" || data.status === "TASK_UNVERIFIED" || data.status === "TASK_STOPPED" || data.status === "TASK_FAILED") {
        const icon = data.status === "TASK_DONE" ? "✅" : (data.status === "TASK_UNVERIFIED" ? "🔍" : "🛑");
        const statusMap: Record<string, string> = {
          TASK_DONE: "任務已完成 (經獨立驗證)",
          TASK_UNVERIFIED: "任務已執行 (待人工確認)",
          TASK_STOPPED: "任務已停止 (重試或步數上限)",
          TASK_FAILED: "任務失敗",
        };
        const title = `${icon} **[${statusMap[data.status as string] || data.status}]** ${params.goal || ""}`;
        let stepsBlock = "";
        if (Array.isArray(data.steps) && data.steps.length > 0) {
          const stepLines = data.steps.map((s: any, idx: number) => {
            const p = s.plan || {};
            const act = p.action || s.action || "step";
            const tgt = p.target || s.target || "";
            const val = p.value ? ` -> "${p.value}"` : "";
            const st = s.status ? ` [${s.status}]` : "";
            const el = s.element ? ` (${s.element})` : "";
            return `${idx + 1}. **${act}** ${tgt}${val}${el}${st}`;
          }).join("\n");
          stepsBlock = `\n\n**執行動作細節序列 (${data.steps.length} 步)**:\n${stepLines}`;
        }
        // The page text itself is in the JSON below (text/page_excerpt); repeating it here only cost tokens.
        const evidence = typeof data.evidence === "string" && data.evidence ? `\n- **驗證依據**: ${data.evidence}` : "";
        humanSummary = `${title}${stepsBlock}\n\n- **當前網址**: ${data.url || ""}\n- **頁面標題**: ${data.title || ""}${evidence}`;
      } else if (data.status === "PLAN_DONE" || data.status === "PLAN_STOPPED") {
        const title = data.status === "PLAN_DONE" ? "📋 **[計劃步驟全部完成]**" : "⚠️ **[計劃步驟提前中斷]**";
        let stepsBlock = "";
        if (Array.isArray(data.steps) && data.steps.length > 0) {
          const stepLines = data.steps.map((s: any, idx: number) => {
            const act = s.action || "step";
            const icon = act === "open" ? "🌐"
              : act === "click" ? "🖱️"
              : act === "fill" ? "⌨️"
              : act === "select" ? "🔽"
              : act === "scroll" ? "📜"
              : act === "screenshot" ? "📸"
              : act === "read" ? "📖"
              : act === "observe" ? "👁️"
              : "⚡";
            const el = s.element ? ` "${s.element}"` : (s.target ? ` "${s.target}"` : (s.url ? ` ${s.url}` : ""));
            const val = s.value ? ` -> "${s.value}"` : "";
            const st = s.status ? ` [${s.status}]` : "";
            return `${idx + 1}. ${icon} **${act}**${el}${val}${st}`;
          }).join("\n");
          stepsBlock = `\n\n**步驟執行清單**:\n${stepLines}`;
        }
        humanSummary = `${title}${stepsBlock}`;
        if (data.url || data.title) {
          humanSummary += `\n\n- **當前網址**: ${data.url || ""}\n- **頁面標題**: ${data.title ? `「${data.title}」` : ""}`;
        }
      } else if (data.status === "NO_MATCH") {
        humanSummary = `❌ **[未找到目標元素]** "${params.target}"\n- **提示**: ${data.hint || "無相符元素"}\n- **搜尋範圍**: ${data.searched || "當前視窗"}`;
      } else if (data.status === "AMBIGUOUS") {
        const opts = Array.isArray(data.options) ? data.options.map((o: any, idx: number) => `  ${idx + 1}. ${o}`).join("\n") : "";
        humanSummary = `⚠️ **[目標有多個可能選項]** "${params.target}"\n${opts}\n- 請指定 pick=<編號> 或更具體的特徵描述。`;
      } else if (data.status === "READ") {
        humanSummary = `📖 **[已讀取頁面內容]**\n${data.text || ""}`;
      }

      if (FULL && Array.isArray(data.links) && data.links.length > 0) {
        const linkLines = data.links.slice(0, 10).map((l: any, idx: number) => {
          const t = l.title ? `「${l.title}」 ` : "";
          return `  [${idx + 1}] ${t}${l.url}`;
        });
        const linksBlock = `\n\n🔗 **[頁面候選跳轉連結 (可直接 open 搭配 pick:編號 或複製 exact url)]**:\n${linkLines.join("\n")}`;
        humanSummary += linksBlock;
      }

      const text = humanSummary ? `${humanSummary}\n\n\`\`\`json\n${JSON.stringify(rest, null, 2)}\n\`\`\`` : JSON.stringify(rest, null, 1);
      const content: any[] = [{ type: "text" as const, text }];
      if (shot && process.env.LAYA_INTENT_IMAGES !== "0") {
        try {
          content.push({ type: "image" as const, data: readFileSync(shot).toString("base64"), mimeType: "image/jpeg" });
        } catch {}
      }
      return { content, details: data };
    },
  });
}
