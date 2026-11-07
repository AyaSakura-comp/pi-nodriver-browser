/**
 * The research router: a small local model decides whether a user message needs web research
 * before the agent answers, the decision the agent otherwise takes in its own first turn.
 *
 * The decision is the model's probability for `"research": true`, read from the token
 * probabilities of its first answer token, so it is known after one token (~0.1 s on LFM3-1.7B
 * with its system prompt cached); the keyword queries for q2-q4 come from a second request. The
 * router reads the same research-when guidance as the agent (APPEND_SYSTEM.md).
 *
 * Measured 2026-11-08 on 23 messages (11 needing research): every message that needs none scored
 * <= 0.20, the ones that do 0.24-0.99; the bare true/false answer missed 5 of the 11.
 */
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const ROUTER_URL = process.env.RESEARCH_ROUTER_URL ?? "http://127.0.0.1:8016/v1/chat/completions";
const ROUTER_MODEL = process.env.RESEARCH_ROUTER_MODEL ?? "lfm3-1.7b";
export const ROUTER_THRESHOLD = Number(process.env.RESEARCH_ROUTER_THRESHOLD ?? 0.3);
const DECIDE_TIMEOUT_MS = 1500;   // no decision by then: the agent decides, as without a router
const QUERIES_TIMEOUT_MS = 1500;
const PREFIX = '{"research": ';
const FALLBACK_GUIDE = "回答前先判斷這則訊息是否需要上網查證：牽涉會變動的資訊、近期的事、具體事實數字、依現況的推薦或規劃，或使用者要求查證時需要；情緒、創作、閒聊、數學與程式、長期穩定的常識、主觀意見不需要。";

let guideText: string | undefined;
function guide(): string {
  if (guideText === undefined) {
    const path = join(homedir(), ".pi", "agent", "APPEND_SYSTEM.md");
    guideText = existsSync(path) ? readFileSync(path, "utf8") : FALLBACK_GUIDE;
  }
  return guideText;
}

interface TopLogprob { token: string; logprob: number }

/** P(true) from the first token whose alternatives include true/false, else undefined. */
function decision(top: TopLogprob[]): number | undefined {
  let pTrue = 0;
  let pFalse = 0;
  for (const { token, logprob } of top) {
    const t = token.trim().toLowerCase();
    if (t.startsWith("t")) pTrue += Math.exp(logprob);
    else if (t.startsWith("f")) pFalse += Math.exp(logprob);
  }
  return pTrue + pFalse > 0 ? pTrue / (pTrue + pFalse) : undefined;
}

async function post(body: Record<string, unknown>, timeoutMs: number): Promise<{ choices?: Array<{ message?: { content?: string }; logprobs?: { content?: Array<{ top_logprobs?: TopLogprob[] }> } }> }> {
  const response = await fetch(ROUTER_URL, {
    method: "POST",
    headers: { "content-type": "application/json" },
    signal: AbortSignal.timeout(timeoutMs),
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`router_http_${response.status}`);
  return response.json();
}

const QUERY_PREFIX = '{"research": true, "queries": ["';

/** The keyword queries of a forced answer that starts inside its query list. */
function forcedQueries(content: string): string[] {
  const text = content.includes(QUERY_PREFIX) ? content.slice(content.indexOf(QUERY_PREFIX) + QUERY_PREFIX.length) : content;
  const list = text.split("]")[0];
  try {
    return (JSON.parse(`["${list}]`) as unknown[]).filter((q): q is string => typeof q === "string");
  } catch {
    return [...`"${list}`.matchAll(/"((?:[^"\\]|\\.)*)"/g)].map((m) => m[1]);
  }
}

/**
 * Resolves once the router has decided: `p` is its probability that `asked` needs research. When `p` reaches
 * ROUTER_THRESHOLD, `queries` resolves with the router's keyword queries (three, from a second request whose
 * answer is forced to start inside the query list: asked to write the whole object, the router often stopped
 * after `"research": true`). Rejects when the router is unreachable or too slow to decide.
 */
export async function routeResearch(asked: string, dateLine: string): Promise<{ p: number; queries: Promise<string[]> }> {
  const system = `${guide()}\n\n你是路由器，只判斷使用者這則訊息是否需要先呼叫 research，不要回答問題本身。${dateLine}\n`
    + '只輸出一行 JSON：{"research": true 或 false, "queries": [3 個不同角度的關鍵字查詢，每個 2-6 個關鍵字，'
    + '例如官方來源、具體數字、日期，第 3 個用英文；相對日期換成具體日期]}。不需要 research 時 queries 為 []。';
  const messages = [{ role: "system", content: system }, { role: "user", content: asked.slice(0, 2000) }];
  const base = { model: ROUTER_MODEL, temperature: 0, continue_final_message: true, add_generation_prompt: false,
    chat_template_kwargs: { enable_thinking: false } };
  const decided = await post({ ...base, max_tokens: 1, n_probs: 10,
    messages: [...messages, { role: "assistant", content: PREFIX }] }, DECIDE_TIMEOUT_MS);
  let p: number | undefined;
  for (const entry of decided.choices?.[0]?.logprobs?.content ?? []) {
    p = decision(entry.top_logprobs ?? []);
    if (p !== undefined) break;
  }
  if (p === undefined) throw new Error("router_no_decision");
  const queries = p < ROUTER_THRESHOLD ? Promise.resolve([] as string[]) : post({ ...base, max_tokens: 100,
    messages: [...messages, { role: "assistant", content: QUERY_PREFIX }] }, QUERIES_TIMEOUT_MS)
    .then((r) => forcedQueries(r.choices?.[0]?.message?.content ?? ""));
  queries.catch(() => {});
  return { p, queries };
}
