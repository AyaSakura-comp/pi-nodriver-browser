import test from "node:test";
import assert from "node:assert/strict";
import { parseAction, runBrowserTask } from "../browser-agent.ts";

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
  assert.deepEqual(parseAction("<think>x</think>```json\n{\"action\":\"click\",\"ref\":\"e2\"}\n```"), { action: "click", ref: "e2" });
  assert.equal(parseAction("{\"action\":\"done\",\"answer\":\"a {b} c\"} trailing").answer, "a {b} c");
  assert.equal(parseAction("no json here"), undefined);
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

test("open refuses a guessed deep link on a site nothing mentions", async () => {
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
  assert.equal(parseAction(raw).answer, "1. A $1\n2. B $2");
  assert.equal(parseAction('Note {not json} then {"action":"scroll","direction":"down"}').action, "scroll");
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

test("open takes pages on a site the current page shows and any site's home page", async () => {
  const results = PAGE('[e1] link "行動電源 - PChome 24h購物 https://24h.pchome.com.tw › 通訊"');
  const h = harness([
    '{"action":"open","url":"https://24h.pchome.com.tw/search/?q=%E8%A1%8C%E5%8B%95%E9%9B%BB%E6%BA%90"}',
    '{"action":"open","url":"https://www.thsrc.com.tw/"}',
    '{"action":"open","url":"https://made-up.example/deep/page"}',
    '{"action":"stop","status":"failed","reason":"x"}',
  ], {
    'open "https://shop.example/item"': results,
    'open "https://24h.pchome.com.tw/search/?q=%E8%A1%8C%E5%8B%95%E9%9B%BB%E6%BA%90"': PAGE("搜尋結果"),
    'open "https://www.thsrc.com.tw/"': PAGE("台灣高鐵"),
  });
  const result = await h.run("查行動電源和高鐵");
  assert.ok(h.sent.includes('open "https://24h.pchome.com.tw/search/?q=%E8%A1%8C%E5%8B%95%E9%9B%BB%E6%BA%90"'));
  assert.ok(h.sent.includes('open "https://www.thsrc.com.tw/"'));
  assert.ok(!h.sent.some((c) => c.includes("made-up.example")));
  assert.ok(result.steps.some((step) => step.includes("made-up.example") && step.includes("refused")));
});

test("scrolling through a long page is progress; the bottom of the page is not", async () => {
  const at = (y: number) => `URL: https://shop.example/list\nTitle: List\nScroll: ${y}/3000px (viewport 600px)\n---\nitems from ${y}`;
  const views = [at(600), at(1200), at(1800), at(2400), at(2400), at(2400), at(2400)];
  const sent: string[] = [];
  let current = at(0);
  const worker = {
    async request(command: string) {
      sent.push(command);
      if (command === "screenshot") return { path: "/tmp/none.jpg" };
      if (command === "get text") return { text: current };
      if (command === "scroll down") current = views.shift() ?? current;
      return { url: "https://shop.example/list", title: "List", snapshot: current };
    },
  };
  const realFetch = globalThis.fetch;
  globalThis.fetch = (async () => new Response(JSON.stringify({ choices: [{ message: { content: '{"action":"scroll","direction":"down"}' } }] }))) as typeof fetch;
  try {
    const result = await runBrowserTask({ goal: "read the whole list", url: "https://shop.example/list", session: "t", worker,
      llm: { endpoint: "http://llm/chat", model: "m", local: true } });
    assert.equal(result.status, "TASK_STOPPED");
    assert.equal(sent.filter((c) => c === "scroll down").length, 6, "four scrolls that move the page, then two at the bottom");
    assert.ok(result.steps.includes("scroll down → now at 600/3000px"), result.steps.join("\n"));
    assert.ok(result.steps.includes("scroll down → no visible change"), result.steps.join("\n"));
  } finally {
    globalThis.fetch = realFetch;
  }
});

test("the tool's hard time limit ends the task with its steps; a cancel still throws", async () => {
  const page = PAGE('[e1] link "職缺"');
  const worker = {
    async request(command: string) {
      if (command === "screenshot") return { path: "/tmp/shot.jpg" };
      return { url: "https://shop.example/item", title: "Item", snapshot: page };
    },
  };
  const realFetch = globalThis.fetch;
  // A planner that never answers: the call ends only when its signal aborts.
  globalThis.fetch = ((_url: string, init: { signal: AbortSignal }) => new Promise((_resolve, reject) => {
    init.signal.addEventListener("abort", () => reject(init.signal.reason));
  })) as typeof fetch;
  // AbortSignal.timeout does not keep Node's event loop alive (pi's own handles do).
  const keepAlive = setTimeout(() => undefined, 10_000);
  try {
    const result = await runBrowserTask({ goal: "找職缺", url: "https://shop.example/item", session: "t", worker,
      llm: { endpoint: "http://llm/chat", model: "m", local: true }, signal: AbortSignal.timeout(150) });
    assert.equal(result.status, "TASK_STOPPED");
    assert.match(result.reason ?? "", /time limit/);
    assert.ok(result.steps[0].startsWith("open https://shop.example/item"));
    assert.equal(result.screenshot, "/tmp/shot.jpg");
    const cancel = new AbortController();
    setTimeout(() => cancel.abort(), 50);
    await assert.rejects(runBrowserTask({ goal: "找職缺", url: "https://shop.example/item", session: "t", worker,
      llm: { endpoint: "http://llm/chat", model: "m", local: true }, signal: cancel.signal }));
  } finally {
    clearTimeout(keepAlive);
    globalThis.fetch = realFetch;
  }
});

test("a link in the answer must be one the page links to", async () => {
  const page = PAGE('[e5] link "Python 工程師"\n百商數位');
  const run = async (answer: string) => {
    const worker = {
      async request(command: string) {
        if (command === "screenshot") return { path: "/tmp/none.jpg" };
        if (command === "get text") return { text: page };
        if (command === "get links") return { text: "https://www.104.com.tw/job/89657754?jobsource=jolist_b\nhttps://www.104.com.tw/" };
        return { url: "https://www.104.com.tw/jobs/search/?keyword=Python", title: "Jobs", snapshot: page };
      },
    };
    const realFetch = globalThis.fetch;
    const reply = JSON.stringify({ action: "done", answer, evidence: ["百商數位"] });
    globalThis.fetch = (async () => new Response(JSON.stringify({ choices: [{ message: { content: reply } }] }))) as typeof fetch;
    try {
      return await runBrowserTask({ goal: "Python 職缺", url: "https://www.104.com.tw/jobs/search/?keyword=Python", session: "t", worker,
        llm: { endpoint: "http://llm/chat", model: "m", local: true } });
    } finally {
      globalThis.fetch = realFetch;
    }
  };
  assert.equal((await run("Python 工程師｜百商數位｜https://www.104.com.tw/job/89657754")).status, "TASK_DONE");
  const made = await run("Python 工程師｜百商數位｜https://www.104.com.tw/job/69x4g");
  assert.equal(made.status, "TASK_UNVERIFIED");
  assert.deepEqual(made.missing_evidence, ["link not on the page: https://www.104.com.tw/job/69x4g"]);
  assert.equal(made.reason, "the answer has links that are not on the page");
});

test("a link's address does not trip the irreversible gate", async () => {
  const page = PAGE('[e7] link "購物說明" → /help/checkout-guide');
  const next = PAGE("購物說明 退換貨");
  const h = harness(['{"action":"click","ref":"e7"}', '{"action":"done","answer":"退換貨","evidence":["退換貨"]}'],
    { 'open "https://shop.example/item"': page, "click e7": next });
  const result = await h.run("看購物說明");
  assert.ok(h.sent.includes("click e7"));
  assert.equal(result.status, "TASK_DONE");
  assert.ok(result.steps[1].startsWith('click link "購物說明" →'), result.steps[1]);
});

