import { readFileSync } from "node:fs";
import { Type } from "typebox";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// Intent-level browser tool: the agent says WHAT it wants; the local intent service
// (laya-browser-intent, :8011) grounds it to one element via exact match / e5 + Laya / a
// shortlist judge, executes it on the shared nodriver browser, and returns the verified
// post-action page state. No @refs or raw DOM ever enter the agent context.
const SERVICE = process.env.LAYA_INTENT_URL || "http://127.0.0.1:8011";
// LAYA_INTENT_MODE=task: the agent can only hand over goals (task) and inspect (read/observe);
// every page operation is decided inside the service.
const TASK_ONLY = process.env.LAYA_INTENT_MODE === "task";
const ACTIONS = TASK_ONLY ? ["task", "read", "observe", "screenshot"] : ["task", "plan", "open", "click", "fill", "select", "scroll", "read", "observe", "back", "screenshot"];

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
    description: TASK_ONLY
      ? `Live browser, goal-level only. Call {action:"task", url, goal} with an exact user-supplied URL or exact search-result URL and every constraint in the goal. If the requested deep link is not indexed and not provided by the user, use the closest official parent URL from search and navigate through visible links; never pass guessed deep URLs. The browser service performs all clicks/typing itself (fast planner + grounding + safety gate + verification) and returns TASK_DONE (verified, with goal-relevant page text) / TASK_UNVERIFIED / TASK_FAILED / TASK_STOPPED / TASK_NEEDS_CONFIRM. Irreversible steps (buy, pay, delete, logout) stop with TASK_NEEDS_CONFIRM; repeat the task with confirm=true ONLY if the user explicitly asked. Use read {target} to look at the current page text. You cannot click yourself.`
      : DESCRIPTION,
    promptSnippet: "Operate web pages by describing what to click or fill; the tool grounds and verifies it",
    promptGuidelines: TASK_ONLY ? [
      "For web tasks, call task ONCE with an exact user-supplied URL or exact search-result URL plus the full goal. If the exact deep link is absent and not provided by the user, use the closest official parent URL returned by search and ask it to navigate through visible links; never reuse or construct the deep URL.",
      "If the result is TASK_STOPPED/TASK_FAILED/TASK_UNVERIFIED, restate the goal more precisely in one more task call or report what was found; do not loop.",
    ] : [
      "Prefer ONE task call {url, goal} using an exact user-supplied URL or exact search-result URL. If an exact deep link is absent and not provided by the user, use the closest official parent URL returned by search and navigate through visible links; never reuse or construct the deep URL.",
      "When the user asks for a screenshot or to see the current webpage (例如要求傳截圖、截圖給我看、看畫面、看網頁截圖或 send screenshot), use action: 'screenshot'. It captures the current browser view immediately without re-opening or navigation.",
      "Use browser_intent for interactive web tasks. Describe targets by visible wording and constraints; never invent refs, selectors or coordinates.",
      "After CLICKED, trust the returned url/title/screenshot as the new page state and decide the next intent from it.",
      "On NO_MATCH, do not repeat the same target: rephrase once with page wording, use read, or conclude the element is not available.",
      "Never set confirm=true unless the user explicitly asked for that irreversible action.",
    ],
    parameters: Type.Object({
      action: Type.Union(ACTIONS.map((a) => Type.Literal(a))),
      steps: Type.Optional(Type.Array(Type.Object({
        action: Type.Union([Type.Literal("open"), Type.Literal("click"), Type.Literal("fill"), Type.Literal("select"), Type.Literal("scroll"), Type.Literal("read"), Type.Literal("observe"), Type.Literal("back"), Type.Literal("screenshot")]),
        url: Type.Optional(Type.String()),
        target: Type.Optional(Type.String()),
        value: Type.Optional(Type.String()),
        submit: Type.Optional(Type.Boolean()),
      }), { maxItems: 8, description: "plan: ordered steps" })),
      url: Type.Optional(Type.String({ description: "open: exact URL" })),
      target: Type.Optional(Type.String({ description: "click/fill/scroll: description of one element; read: what to look for" })),
      value: Type.Optional(Type.String({ description: "fill: text to enter; scroll: direction or offset (e.g. top, bottom, down 600)" })),
      submit: Type.Optional(Type.Boolean({ description: "fill: press Enter after typing" })),
      goal: Type.Optional(Type.String({ description: "task: the full goal with all constraints; otherwise overall task for disambiguation" })),
      max_steps: Type.Optional(Type.Integer({ minimum: 1, maximum: 12, description: "task: step limit (default 8)" })),
      pick: Type.Optional(Type.Integer({ minimum: 1, maximum: 4, description: "Option number after AMBIGUOUS" })),
      confirm: Type.Optional(Type.Boolean({ description: "Only for user-requested irreversible actions" })),
    }),
    async execute(_id, params, signal, onUpdate, ctx) {
      await ensureBrowser?.();
      const session = `intent-${ctx.sessionManager.getSessionId()}`;
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
      if (model) {
        if (model.id) payload.model = model.id;
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
        if (baseUrl) payload.base_url = baseUrl;
        if (Object.keys(headers).length > 0) payload.headers = headers;
      }
      let data: Record<string, unknown> = {};
      try {
        const streamRes = await fetch(`${SERVICE}/act/stream`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(payload),
          signal,
        });
        if (streamRes.ok && streamRes.body) {
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
      } catch {
        const res = await fetch(`${SERVICE}/act`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(payload),
          signal,
        });
        data = (await res.json()) as Record<string, unknown>;
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
        const excerpt = data.page_excerpt || data.text || data.answer || "";
        humanSummary = `${title}${stepsBlock}\n\n- **當前網址**: ${data.url || ""}\n- **頁面標題**: ${data.title || ""}${excerpt ? `\n- **頁面資訊摘要**: ${excerpt.slice(0, 300)}` : ""}`;
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
