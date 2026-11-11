import test from "node:test";
import assert from "node:assert/strict";
import { parseAction, parseActions, runBrowserTask } from "../browser-agent.ts";

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

test("an action that did not change the page is not run again, and choosing it a third time stops the task", async () => {
  const page = PAGE("[e1] button \"更多\"");
  const reply = '{"action":"click","ref":"e1"}';
  const h = harness([reply, reply, reply], { 'open "https://shop.example/item"': page, "click e1": page });
  const result = await h.run("找規格");
  assert.equal(result.status, "TASK_STOPPED");
  assert.equal(h.sent.filter((c) => c === "click e1").length, 1, "done once; the second time it is refused, the third ends the task");
  assert.ok(result.steps.some((step) => step.includes("not repeated")), result.steps.join("\n"));
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
    if (String(_url).endsWith("/hold")) return new Response("{}");
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

test("scrolling that loads more items shows only the new lines; at the end of the list it is not repeated", async () => {
  const items = (n: number) => Array.from({ length: n }, (_v, i) => `[e${i + 1}] link "item ${i + 1}"`).join("\n");
  const loads = [items(4), items(6), items(6), items(6)];
  const sent: string[] = [];
  let current = items(2);
  const worker = {
    async request(command: string) {
      sent.push(command);
      if (command === "screenshot") return { path: "/tmp/none.jpg" };
      if (command === "get text") return { text: current };
      if (command === "scroll down") current = loads.shift() ?? current;
      return { url: "https://shop.example/list", title: "List", doc: "d1", body: current };
    },
  };
  const realFetch = globalThis.fetch;
  const prompts: string[] = [];
  globalThis.fetch = (async (url: string, init: { body: string }) => {
    if (String(url).endsWith("/hold")) return new Response("{}");
    prompts.push(JSON.parse(init.body).messages.at(-1).content);
    return new Response(JSON.stringify({ choices: [{ message: { content: '{"action":"scroll","direction":"down"}' } }] }));
  }) as typeof fetch;
  try {
    const result = await runBrowserTask({ goal: "read the whole list", url: "https://shop.example/list", session: "t", worker,
      llm: { endpoint: "http://llm/chat", model: "m", local: true } });
    assert.equal(result.status, "TASK_STOPPED");
    assert.equal(sent.filter((c) => c === "scroll down").length, 3, "two that load items, one that loads nothing; then refused, then stopped");
    assert.match(prompts[1], /Same page, 2 new or changed lines/);
    assert.ok(prompts[1].includes('[e3] link "item 3"') && !prompts[1].includes('[e1] link "item 1"'), prompts[1]);
    assert.match(prompts[3], /nothing new appeared/);
    assert.match(prompts[4], /Not done again/);
    assert.ok(result.steps.includes("scroll down → nothing new loaded"), result.steps.join("\n"));
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

/** A fake worker whose pages have the parts the real one returns (doc, body, links), and a planner
 * whose every request is kept. */
function session(replies: string[], pages: Record<string, { doc: string; body: string; url?: string; title?: string; links?: Record<string, string> }>) {
  const sent: string[] = [];
  const requests: any[] = [];
  let current = "";
  const worker = {
    async request(command: string) {
      sent.push(command);
      if (command === "screenshot") return { path: "/tmp/none.jpg" };
      if (command === "get text") return { text: current };
      if (command === "get links") return { text: "" };
      const page = pages[command];
      if (page === undefined) throw new Error(`unexpected command ${command}`);
      current = page.body;
      return { url: page.url ?? "https://shop.example/list", title: page.title ?? "Shop", ...page };
    },
  };
  const queue = [...replies];
  const realFetch = globalThis.fetch;
  globalThis.fetch = (async (url: string, init: { body: string }) => {
    const body = JSON.parse(init.body);
    requests.push({ url: String(url), ...body });
    if (String(url).endsWith("/hold")) return new Response('{"released":true}');
    return new Response(JSON.stringify({ choices: [{ message: { content: queue.shift() ?? '{"action":"stop","status":"failed","reason":"out of replies"}' } }],
      timings: { prompt_n: 10, cache_n: 0, prompt_ms: 1, predicted_n: 5, predicted_ms: 1 } }));
  }) as typeof fetch;
  const run = (goal: string, extra: Record<string, unknown> = {}) =>
    runBrowserTask({ goal, session: "t", worker, llm: { endpoint: "http://llm/v1/chat/completions", model: "m", local: true },
      url: "https://shop.example/list", ...extra }).finally(() => { globalThis.fetch = realFetch; });
  const chats = () => requests.filter((r) => !r.url.endsWith("/hold"));
  return { sent, requests, chats, run };
}

test("parseActions takes one action, an array of actions, or prose around them", () => {
  assert.deepEqual(parseActions('[{"action":"select","ref":"e2","option":"台北"},{"action":"click","ref":"e9"}]').map((a) => a.action), ["select", "click"]);
  assert.deepEqual(parseActions('I will fill the form.\n{"actions":[{"action":"type","ref":"e1","text":"a"},{"action":"click","ref":"e2"}]}').length, 2);
  assert.deepEqual(parseActions('[{"reason":"no action here","ref":"e60"}]'), []);
  assert.equal(parseActions('note [1] then {"action":"more"}')[0].action, "more");
});

test("the task is one growing conversation: every request repeats the one before it and adds to it", async () => {
  const h = session(['{"action":"click","ref":"e2"}', 'Now I answer.\n{"action":"done","answer":"$599","evidence":["$599"]}'], {
    'open "https://shop.example/list"': { doc: "d1", body: '[e1] link "home"\n[e2] link "行動電源"' },
    "click e2": { doc: "d2", body: "行動電源\n$599", url: "https://shop.example/item" },
  });
  const result = await h.run("價格");
  assert.equal(result.status, "TASK_DONE");
  const [first, second] = h.chats();
  assert.deepEqual(second.messages.slice(0, first.messages.length), first.messages, "an engine that cannot cut its state back reads only what is added");
  assert.equal(second.messages[2].role, "assistant");
  assert.equal(second.messages[2].content, '{"action":"click","ref":"e2"}', "the reply is kept as its bare action");
  assert.match(second.messages[3].content, /Page: Shop\nURL: https:\/\/shop\.example\/item/);
  assert.equal(result.model_calls, 2);
  assert.equal(result.trace.filter((span) => span.kind === "model").length, 2);
});

test("an answer may use a page seen earlier in the task", async () => {
  const h = session(['{"action":"click","ref":"e9"}', '{"action":"done","answer":"第1頁 A $100；第2頁 B $200","evidence":["A $100","B $200"]}'], {
    'open "https://shop.example/list"': { doc: "d1", body: 'A $100\n[e9] link "下一頁"' },
    "click e9": { doc: "d2", body: "B $200", url: "https://shop.example/list?page=2" },
  });
  const result = await h.run("列出第 1 頁和第 2 頁的商品");
  assert.equal(result.status, "TASK_DONE", JSON.stringify(result.missing_evidence));
  const missing = session(['{"action":"click","ref":"e9"}', '{"action":"done","answer":"C $300","evidence":["C $300"]}'], {
    'open "https://shop.example/list"': { doc: "d1", body: 'A $100\n[e9] link "下一頁"' },
    "click e9": { doc: "d2", body: "B $200" },
  });
  assert.equal((await missing.run("列出商品")).status, "TASK_UNVERIFIED");
});

test("a long page is read in parts with more, without scrolling", async () => {
  const lines = Array.from({ length: 250 }, (_v, i) => `row ${i} ${"x".repeat(60)}`);
  const h = session(['{"action":"more"}', '{"action":"more"}', '{"action":"done","answer":"row 249","evidence":["row 249"]}'], {
    'open "https://shop.example/list"': { doc: "d1", body: lines.join("\n") },
  });
  const result = await h.run("最後一列");
  assert.equal(result.status, "TASK_DONE");
  assert.ok(!h.sent.some((c) => c.startsWith("scroll")));
  const chats = h.chats();
  assert.match(chats[0].messages[1].content, /first part \(\d+ more lines: \{"action":"more"\}/);
  assert.ok(chats[0].messages[1].content.includes("row 0 ") && !chats[0].messages[1].content.includes("row 249 "));
  assert.match(chats[1].messages.at(-1).content, /^Next part of the page \(this is its end\)/);
  assert.ok(chats[1].messages.at(-1).content.includes("row 249 "));
  assert.match(chats[2].messages.at(-1).content, /^There is no more text on this page\./);
});

test("an update of the same page lists only what is new, and says where", async () => {
  const before = '[e1] select "出發站" selected="南港"\n[e2] textbox "時間" value="11:00"\n[e3] button "查詢"';
  const after = '[e1] select "出發站" selected="台北"\n[e2] textbox "時間" value="11:00"\n[e3] button "查詢"';
  const h = session(['{"action":"select","ref":"e1","option":"台北"}', '{"action":"stop","status":"failed","reason":"x"}'], {
    'open "https://shop.example/list"': { doc: "d1", body: before },
    'select e1 "台北"': { doc: "d1", body: after },
  });
  await h.run("查時刻");
  const update = h.chats()[1].messages.at(-1).content;
  assert.match(update, /Same page, 1 new or changed lines, 1 earlier lines are gone:/);
  assert.ok(update.includes('[e1] select "出發站" selected="台北"'));
  assert.ok(!update.includes("查詢"), update);
});

test("several actions in one reply fill a form; they stop when the page changes", async () => {
  const form = '[e1] select "出發站" selected="南港"\n[e2] textbox "時間"\n[e3] button "查詢"';
  const h = session([
    '[{"action":"select","ref":"e1","option":"台北"},{"action":"type","ref":"e2","text":"15:00"},{"action":"click","ref":"e3"},{"action":"click","ref":"e2"}]',
    '{"action":"done","answer":"15:11","evidence":["15:11"]}',
  ], {
    'open "https://shop.example/list"': { doc: "d1", body: form },
    'select e1 "台北"': { doc: "d1", body: form.replace("南港", "台北") },
    'type e2 "15:00"': { doc: "d1", body: form.replace("南港", "台北").replace('"時間"', '"時間" value="15:00"') },
    "click e3": { doc: "d2", body: "車次 0837 15:11", url: "https://shop.example/result" },
  });
  const result = await h.run("查 15:00 後的車");
  assert.equal(result.status, "TASK_DONE");
  assert.deepEqual(h.sent.slice(0, 4), ['open "https://shop.example/list"', 'select e1 "台北"', 'type e2 "15:00"', "click e3"]);
  assert.equal(h.chats().length, 2, "one reply for the form, one for the answer");
  assert.match(h.chats()[1].messages.at(-1).content, /The other 1 action was not run: the page changed\./);
});

test("a ref in the answer becomes the link's address", async () => {
  const h = session(['{"action":"done","answer":"Python 工程師 [e5]","evidence":["百商數位"]}'], {
    'open "https://shop.example/list"': { doc: "d1", body: '[e5] link "Python 工程師"\n百商數位', links: { e5: "https://www.104.com.tw/job/89657754?jobsource=x" } },
  });
  const result = await h.run("Python 職缺和連結");
  assert.equal(result.status, "TASK_DONE", JSON.stringify(result.missing_evidence));
  assert.equal(result.answer, "Python 工程師 https://www.104.com.tw/job/89657754?jobsource=x");
});

test("going back and forth between two pages is stopped at the second round", async () => {
  // A login wall that prints the time and a request id is the same wall each time.
  const wall = (n: number) => `請登入\n看起來您尚未登入。請登入以繼續，或返回首頁。\n日期時間: 2026-11-11T02:49:0${n}Z\nID: 66340f1caf${n}-6a61-479f-881b-c8860f7016a2\n[e5] button "返回首頁"`;
  const home = '[e14] searchbox "搜尋"';
  let visits = 0;
  const h = session(['{"action":"click","ref":"e5"}', '{"action":"type","ref":"e14","text":"手機散熱器","submit":true}',
    '{"action":"click","ref":"e5"}', '{"action":"stop","status":"needs_input","reason":"需要登入"}'], {
    'open "https://shop.example/list"': { doc: "w", body: wall(1) },
    "click e5": { doc: "h", body: home, url: "https://shop.example/" },
    'type e14 "手機散熱器" --submit': { get doc() { return "w"; }, get body() { return wall(++visits + 1); } } as any,
  });
  const result = await h.run("列出手機散熱器");
  assert.equal(result.status, "TASK_NEEDS_INPUT");
  assert.equal(h.sent.filter((c) => c === "click e5").length, 1);
  const chats = h.chats();
  assert.match(chats[0].messages[1].content, /looks like a login or verification wall/);
  assert.match(chats[3].messages.at(-1).content, /Not done again: "click button "返回首頁"" was already done on this same page/);
});

test("the task's first page may be any address; later a guessed deep link is refused", async () => {
  const h = session(['{"action":"open","url":"https://rate.bot.com.tw/xrt?Lang=zh-TW"}', '{"action":"open","url":"https://guessed.example/a/b"}',
    '{"action":"done","answer":"33.155","evidence":["33.155"]}'], {
    snapshot: { doc: "", body: "" } as any,
    'open "https://rate.bot.com.tw/xrt?Lang=zh-TW"': { doc: "d1", body: "美金 (USD) 32.485 33.155", url: "https://rate.bot.com.tw/xrt?Lang=zh-TW" },
  });
  const result = await h.run("台銀美金現金賣出匯率", { url: undefined });
  assert.equal(result.status, "TASK_DONE");
  assert.ok(h.sent.includes('open "https://rate.bot.com.tw/xrt?Lang=zh-TW"'));
  assert.ok(!h.sent.some((c) => c.includes("guessed.example")));
});

test("a local engine is asked to hold itself for the task and is let go at its end", async () => {
  const h = session(['{"action":"done","answer":"ok","evidence":["ok"]}'], { 'open "https://shop.example/list"': { doc: "d1", body: "ok" } });
  await h.run("x");
  const chat = h.chats()[0];
  assert.equal(chat.hold.keep, false);
  assert.ok(chat.hold.seconds > 0 && chat.hold.key.startsWith("browser:t:"));
  const release = h.requests.at(-1);
  assert.equal(release.url, "http://llm/v1/hold");
  assert.equal(release.key, chat.hold.key);
});

test("time spent queued behind other requests is not the task's own time", async () => {
  const worker = {
    async request(command: string) {
      if (command === "screenshot") return { path: "/tmp/none.jpg" };
      if (command === "get text") return { text: "ok" };
      return { url: "https://shop.example/list", title: "Shop", doc: "d1", body: "ok" };
    },
  };
  const realFetch = globalThis.fetch;
  globalThis.fetch = (async (url: string) => {
    if (String(url).endsWith("/hold")) return new Response("{}");
    await new Promise((resolve) => setTimeout(resolve, 120));
    return new Response(JSON.stringify({ choices: [{ message: { content: '{"action":"done","answer":"ok","evidence":["ok"]}' } }],
      timings: { prompt_n: 10, cache_n: 0, prompt_ms: 20, predicted_n: 5, predicted_ms: 10 } }));
  }) as typeof fetch;
  try {
    const result = await runBrowserTask({ goal: "x", url: "https://shop.example/list", session: "t", worker,
      llm: { endpoint: "http://llm/v1/chat/completions", model: "m", local: true } });
    assert.ok(result.wait_ms >= 80 && result.wait_ms <= result.llm_ms - 30, `${result.wait_ms} of ${result.llm_ms}`);
  } finally {
    globalThis.fetch = realFetch;
  }
});
