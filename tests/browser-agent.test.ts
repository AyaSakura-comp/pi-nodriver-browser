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
