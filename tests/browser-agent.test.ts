import test from "node:test";
import assert from "node:assert/strict";
import { runBrowserTask, _test } from "../browser-agent.ts";

const PAGE = (body: string) => `URL: https://shop.example/item\nTitle: Item\n---\n${body}`;

function harness(replies: string[], pages: Record<string, string>, initial = PAGE("[e1] link \"home\"")) {
  const sent: string[] = [];
  let current = initial;
  const worker = {
    async request(command: string) {
      sent.push(command);
      if (command === "screenshot") return { path: "/tmp/none.jpg" };
      if (command === "get text") return { text: current };
      const next = pages[command];
      if (next === undefined) throw new Error(`unexpected command ${command}`);
      current = next;
      return { url: "https://shop.example/item", title: "Item", snapshot: next };
    },
  };
  const queue = [...replies];
  const realFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response(JSON.stringify({ choices: [{ message: { content: queue.shift() ?? "{}" } }] }))) as typeof fetch;
  const run = (goal: string, extra: Record<string, unknown> = {}) =>
    runBrowserTask({ goal, session: "t", worker, llm: { endpoint: "http://llm/chat", model: "m", local: true },
      url: "https://shop.example/item", ...extra }).finally(() => { globalThis.fetch = realFetch; });
  return { sent, run };
}

test("parseAction tolerates fences, think blocks and braces inside strings", () => {
  assert.deepEqual(_test.parseAction("<think>x</think>```json\n{\"action\":\"click\",\"ref\":\"e2\"}\n```"), { action: "click", ref: "e2" });
  assert.equal(_test.parseAction("{\"action\":\"done\",\"answer\":\"a {b} c\"} trailing").answer, "a {b} c");
  assert.equal(_test.parseAction("no json here"), undefined);
});

test("an irreversible click is not sent without confirm", async () => {
  const page = PAGE("[e3] button \"立即購買\"\n$1,990");
  const h = harness(['{"action":"click","ref":"e3"}'], { 'open "https://shop.example/item"': page });
  const result = await h.run("查價格");
  assert.equal(result.status, "TASK_NEEDS_CONFIRM");
  assert.ok(!h.sent.includes("click e3"));
});

test("confirm=true lets the irreversible click through", async () => {
  const page = PAGE("[e3] button \"立即購買\"");
  const done = PAGE("訂單已成立");
  const h = harness(['{"action":"click","ref":"e3"}', '{"action":"done","answer":"ok","evidence":["訂單已成立"]}'],
    { 'open "https://shop.example/item"': page, "click e3": done });
  const result = await h.run("幫我下單", { confirm: true });
  assert.ok(h.sent.includes("click e3"));
  assert.equal(result.status, "TASK_DONE");
});

test("a secret is typed only when the goal gives it", async () => {
  const page = PAGE('[e2] textbox "密碼" type=password');
  const h = harness(['{"action":"type","ref":"e2","text":"guess123"}'], { 'open "https://shop.example/item"': page });
  const result = await h.run("登入看訂單");
  assert.equal(result.status, "TASK_NEEDS_INPUT");
  assert.ok(!h.sent.some((c) => c.startsWith("type")));
});

test("done is verified against the page text", async () => {
  const page = PAGE("[e1] link \"商品\"\n售價 $349");
  const ok = harness(['{"action":"done","answer":"$349","evidence":["售價 $349"]}'], { 'open "https://shop.example/item"': page });
  assert.equal((await ok.run("價格")).status, "TASK_DONE");
  const bad = harness(['{"action":"done","answer":"$299","evidence":["售價 $299"]}'], { 'open "https://shop.example/item"': page });
  const result = await bad.run("價格");
  assert.equal(result.status, "TASK_UNVERIFIED");
  assert.deepEqual(result.missing_evidence, ["售價 $299"]);
});

test("the same action three times on an unchanged page stops the task", async () => {
  const page = PAGE("[e1] button \"更多\"");
  const reply = '{"action":"click","ref":"e1"}';
  const h = harness([reply, reply, reply], { 'open "https://shop.example/item"': page, "click e1": page });
  const result = await h.run("找規格");
  assert.equal(result.status, "TASK_STOPPED");
  assert.equal(h.sent.filter((c) => c === "click e1").length, 2);
});

test("open only accepts URLs written in the goal", async () => {
  const page = PAGE("[e1] link \"home\"");
  const h = harness(['{"action":"open","url":"https://guessed.example/deep/link"}', '{"action":"stop","status":"failed","reason":"x"}'],
    { 'open "https://shop.example/item"': page });
  await h.run("看看");
  assert.ok(!h.sent.some((c) => c.includes("guessed.example")));
});

test("select on a link is treated as a click", async () => {
  const page = PAGE('[e4] link "行動電源"\n[e5] select "排序" selected="推薦" options: 推薦 | 價格');
  const next = PAGE("搜尋結果 行動電源 $599");
  const h = harness(['{"action":"select","ref":"e4"}', '{"action":"done","answer":"$599","evidence":["行動電源 $599"]}'],
    { 'open "https://shop.example/item"': page, "click e4": next });
  const result = await h.run("find power banks");
  assert.ok(h.sent.includes("click e4"));
  assert.equal(result.status, "TASK_DONE");
});

test("parseAction repairs raw line breaks inside strings and skips prose objects", () => {
  const raw = '{"action":"done","answer":"1. A $1\n2. B $2","evidence":["A $1"]}';
  assert.equal(_test.parseAction(raw).answer, "1. A $1\n2. B $2");
  assert.equal(_test.parseAction('Note {not json} then {"action":"scroll","direction":"down"}').action, "scroll");
});

test("a planner that cannot produce actions hands the task to the fallback planner", async () => {
  const page = PAGE('[e1] link "行動電源"\n$599');
  const sent: string[] = [];
  const worker = {
    async request(command: string) {
      sent.push(command);
      if (command === "screenshot") return { path: "/tmp/none.jpg" };
      if (command === "get text") return { text: page };
      return { url: "https://shop.example/item", title: "Item", snapshot: page };
    },
  };
  const realFetch = globalThis.fetch;
  const asked: string[] = [];
  globalThis.fetch = (async (_url: string, init: { body: string }) => {
    const model = JSON.parse(init.body).model;
    asked.push(model);
    const content = model === "tiny" ? "<tool_call> <function=click> </tool_call>" : '{"action":"done","answer":"$599","evidence":["$599"]}';
    return new Response(JSON.stringify({ choices: [{ message: { content } }] }));
  }) as typeof fetch;
  try {
    const result = await runBrowserTask({ goal: "價格", url: "https://shop.example/item", session: "t", worker,
      llm: { endpoint: "http://llm/chat", model: "tiny", local: true },
      fallbackLlm: { endpoint: "http://llm/chat", model: "good", local: true } });
    assert.equal(result.status, "TASK_DONE");
    assert.equal(result.planner_switched, "tiny → good");
    assert.deepEqual(asked, ["tiny", "tiny", "good"]);
  } finally {
    globalThis.fetch = realFetch;
  }
});
