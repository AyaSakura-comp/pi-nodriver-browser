import test from 'node:test';
import assert from 'node:assert/strict';
import { message, register } from './fixtures/extension.mjs';

const Y = new Date().getFullYear();
const QUERIES = {q1: 'q1', q2: 'q2', q3: 'q3', q4: 'q4'};

/** Stands in for WorkerClient: research streams `prefixes` as evidence frames, then replies with `packet`. */
function fakeWorker() {
 const worker = {
  calls: [], added: [], cleaned: [], prefixes: [], packet: 'stable prefix\nfooter', fail: false,
  async research(params, sessionId, signal, onEvidence) {
   worker.calls.push({params, sessionId, signal});
   await new Promise(setImmediate);  // frames arrive after the request, as on the socket
   for (const prefix of worker.prefixes) onEvidence(prefix);
   if (worker.fail) throw new Error('research_failed');
   return {id: 1, ok: true, type: 'result', action: 'research', jobId: params.jobId, status: 'collected',
    fullEvidenceDelivered: true, text: worker.packet};
  },
  async addQuery(sessionId, payload) { worker.added.push(payload); },
  async request() { return {}; },
  async cleanupSession(sessionId) { worker.cleaned.push(sessionId); },
  disconnect() {},
 };
 return worker;
}

async function setup({prefill = false, user = '使用者的問題'} = {}) {
 process.env.RESEARCH_ACTIVE_PREFILL = prefill ? '1' : '0';
 const worker = fakeWorker();
 const {tools, fire} = await register({worker});
 let branch = [];
 const ctx = {getContextUsage: () => ({tokens: 1000, contextWindow: 200000}),
  sessionManager: {getSessionId: () => 'owner', getBranch: () => branch},
  prefill: {begin: () => { throw new Error('research must not drive a disabled prefill'); }}};
 const ask = async (text, previousAnswer = '') => {
  branch = previousAnswer ? [message('user', 'x'), message('assistant', previousAnswer)] : [];
  await fire('input', {text}, ctx);
 };
 const research = (callId, params = QUERIES, onUpdate, context = ctx) => tools.get('research').execute(callId, params, undefined, onUpdate, context);
 // A streamed research tool call: argument deltas, then its end.
 const stream = (delta, {id = 'tc-1', name = 'research', type = 'toolcall_delta'} = {}) =>
  fire('message_update', {assistantMessageEvent: {type, contentIndex: 0, delta, partial: {content: [{type: 'toolCall', id, name}]}}}, ctx);
 const end = (stopReason, ids = []) =>
  fire('message_end', {message: {role: 'assistant', stopReason, content: ids.map((id) => ({type: 'toolCall', id, name: 'research'}))}}, ctx);
 if (user) await ask(user);
 return {worker, tools, fire, ctx, ask, research, stream, end};
}

test('evidence streams as cumulative updates and the result extends the last one', async () => {
 const {worker, research} = await setup();
 worker.prefixes = ['stable', 'stable prefix', 'stable prefix'];
 const updates = [];
 const result = await research('call-1', QUERIES, (update) => updates.push(update));
 assert.deepEqual(updates.map((u) => u.content.map((c) => c.text).join('')), ['', 'stable', 'stable prefix'],
  'status first (no text), then each longer committed prefix once');
 assert.deepEqual(updates.map((u) => u.details.status), ['running', 'evidence', 'evidence']);
 assert.equal(updates[0].details.jobId, worker.calls[0].params.jobId);
 assert.ok(result.content[0].text.startsWith('stable prefix'), 'the result extends the last update');
 assert.equal(result.details.text, undefined, 'the packet is the content, not a detail');
 assert.equal(result.details.status, 'collected');
 assert.equal(result.details.prefill, undefined);
 await research('call-2');  // no onUpdate callback: nothing is sent
});

test('the research request carries exactly the declared fields', async () => {
 const {worker, research} = await setup();
 await research('call-1', {q1: 'q1', q2: ' ', q3: 'q3', q4: 'q4'});
 const {params} = worker.calls[0];
 assert.deepEqual(Object.keys(params).sort(), ['answer', 'clock', 'crawlConcurrency', 'crawlWordBudget', 'jobId', 'queries', 'question']);
 assert.deepEqual(params.queries, ['q1', 'q3', 'q4']);
 assert.equal(params.question, '使用者的問題');
 assert.equal(params.answer, 'breadth');
 assert.equal(params.crawlConcurrency, 32);
 assert.equal(params.crawlWordBudget, 20000);
 assert.deepEqual(Object.keys(params.clock).sort(), ['iso', 'timezone']);
 assert.ok(Number.isFinite(Date.parse(params.clock.iso)));
 const bare = await setup({user: ''});
 await bare.research('call-2');
 assert.equal(bare.worker.calls[0].params.question, 'q1 ; q2 ; q3 ; q4', 'without a user message the queries are the question');
});

test('research starts while q1 is written; q2-q4 join it and execute adopts it', async () => {
 const {worker, research, stream, end} = await setup({user: '台積電收盤價'});
 worker.prefixes = ['stable', 'stable prefix'];
 await stream('{');
 await stream('"q1": "台積電 收盤價');
 assert.equal(worker.calls.length, 0, 'an unfinished value starts nothing');
 await stream(` ${Y}-09-30", `);
 assert.equal(worker.calls.length, 1, 'the job starts as soon as q1 is complete');
 assert.deepEqual(worker.calls[0].params.queries, [`台積電 收盤價 ${Y}-09-30`]);
 assert.equal(worker.calls[0].params.streaming, true);
 assert.equal(worker.calls[0].params.question, '台積電收盤價');
 await stream(`"q2": "TSMC close ${Y - 1}", `);
 await stream(`"q2": "TSMC close ${Y - 1}", `);
 assert.deepEqual(worker.added, [{jobId: worker.calls[0].params.jobId, query: `TSMC close ${Y}`, index: 1}], 'q2 joins the running job once');
 await stream('', {type: 'toolcall_end'});
 assert.deepEqual(worker.added.at(-1), {jobId: worker.calls[0].params.jobId, done: true}, 'the end of the call closes the query stream');
 await stream('{"q1": "other"}', {id: 'tc-x', name: 'browser_intent'});
 assert.equal(worker.calls.length, 1, 'other tools are ignored');
 await new Promise(setImmediate);
 await end('toolUse', ['tc-1']);
 const log = [];
 const adopted = await research('tc-1', {...QUERIES, q1: `台積電 收盤價 ${Y}-09-30`}, (u) => log.push(u.content.map((c) => c.text).join('')));
 assert.equal(worker.calls.length, 1, 'execute adopts the running job instead of starting another');
 assert.deepEqual(log, ['', 'stable prefix'], 'evidence committed before adoption is replayed as one update');
 assert.ok(adopted.content[0].text.startsWith('stable prefix'));
});

test('a job whose call is not executed is cancelled and frees the session', async () => {
 const {worker, stream, end, research} = await setup();
 await stream('{"q1": "orphan query", ', {id: 'tc-3'});
 await stream('{"q1": "second call", ', {id: 'tc-4'});
 assert.equal(worker.calls.length, 1, 'one research job per session');
 await assert.rejects(research('tc-4'), /research_session_busy/);
 await end('stop');
 assert.equal(worker.calls[0].signal.aborted, true, 'no tool use: the job is cancelled');
 await stream('{"q1": "orphan query", ', {id: 'tc-5'});
 assert.equal(worker.calls.length, 2, 'the session is free again');
 await end('toolUse', ['tc-6']);
 assert.equal(worker.calls[1].signal.aborted, true, 'another call was executed: the job is cancelled');
});

test('a started job whose call never executes is dropped after a minute', async (t) => {
 const {worker, stream, research} = await setup();
 t.mock.timers.enable({apis: ['setTimeout']});
 await stream('{"q1": "rejected by validation later", ', {id: 'tc-7'});
 t.mock.timers.tick(60_000);
 t.mock.timers.reset();
 assert.equal(worker.calls[0].signal.aborted, true);
 await research('tc-8');
 assert.equal(worker.calls.length, 2);
});

test('a call RESEARCH_DONE_GUARD will block starts no job; asking for more does', async () => {
 const {worker, fire, ctx, ask, stream, end} = await setup({user: '台積電收盤價'});
 await fire('tool_result', {toolName: 'research', isError: false, content: [{type: 'text', text: 'evidence'}]}, ctx);
 await stream('{"q1": "blocked follow-up query", ', {id: 'tc-9'});
 assert.equal(worker.calls.length, 0, 'a call the guard will block starts no speculative job');
 await ask('幫我再查更多');
 await stream('{"q1": "requested follow-up query", ', {id: 'tc-10'});
 assert.equal(worker.calls.length, 1, 'an explicit request for more still starts one');
 await end('stop');
});

test('prefill: begin at the start, append each committed piece, end with the reply, cancel when not delivered', async () => {
 const {worker, research, ctx} = await setup({prefill: true});
 const log = [];
 const handle = {append: (text) => { log.push(['append', text]); return true; }, end: () => log.push(['end']),
  cancel: (reason) => log.push(['cancel', reason]), stats: () => ({state: 'ended', warmedChars: 13})};
 const context = {...ctx, prefill: {begin: (id) => { log.push(['begin', id]); return handle; }}};
 worker.prefixes = ['stable', 'stable prefix'];
 const result = await research('call-1', QUERIES, undefined, context);
 assert.deepEqual(log, [['begin', 'call-1'], ['append', 'stable'], ['append', ' prefix'], ['end']]);
 assert.deepEqual(result.details.prefill, {state: 'ended', warmedChars: 13});
 log.length = 0;
 worker.prefixes = ['stable', 'rewritten'];
 await research('call-2', QUERIES, undefined, context);
 assert.deepEqual(log, [['begin', 'call-2'], ['append', 'stable'], ['cancel', 'prefix_rewritten'], ['end']]);
 log.length = 0;
 worker.prefixes = ['stable'];
 worker.fail = true;
 await assert.rejects(research('call-3', QUERIES, undefined, context), /research_failed/);
 assert.deepEqual(log, [['begin', 'call-3'], ['append', 'stable'], ['cancel', 'research_failed']]);
});

test('the packet is delivered whole unless it is too large for the tool or the context', async () => {
 const {worker, research, ctx} = await setup({user: '聯亞科技有上市嗎'});
 worker.packet = 'X'.repeat(60000);
 const medium = await research('c1');
 assert.equal(medium.details.fullEvidenceDelivered, true, 'no 50 KiB cap on the research handoff');
 assert.ok(medium.content[0].text.startsWith(worker.packet));
 assert.equal(medium.details.estimatedInputTokens, 30000);
 const crowded = await research('c2', QUERIES, undefined, {...ctx, getContextUsage: () => ({tokens: 190000, contextWindow: 200000})});
 assert.equal(crowded.details.reason, 'packet_too_large', 'beyond half the context left after a 16k reserve');
 worker.packet = 'X'.repeat(2 * 1024 * 1024 + 1);
 const large = await research('c3');
 assert.equal(large.details.status, 'incomplete');
 assert.equal(large.details.fullEvidenceDelivered, false);
 assert.equal(large.details.reason, 'packet_too_large');
 assert.ok(large.content[0].text.length < 1000);
 assert.ok(!large.content[0].text.includes('«'), 'an undelivered packet must not ask for an answer');
});

test('the user question closes the packet; a picked item or a request for detail switches to depth', async () => {
 const {worker, research, ask} = await setup({user: ''});
 worker.packet = 'Full evidence';
 const answer = () => worker.calls.at(-1).params.answer;
 await ask('[Web user: web]\n聯亞科技有上市嗎');
 const asked = await research('c1');
 assert.match(asked.content[0].text, /^Full evidence\nThe user's question: «聯亞科技有上市嗎»\nAnswer it now, breadth first: direct answer in the first sentence;[^\n]*\n$/,
  'the question and a compact checklist close the packet');
 assert.equal(answer(), 'breadth', 'a new question gets the breadth-first overview');
 await ask('[Web user: web]\n<quoted_selection> 大新營萬聖市集 </quoted_selection>\n\n這什麼？', '1. 大新營萬聖市集 10/31-11/1');
 const picked = await research('c2');
 assert.equal(answer(), 'depth', 'picking an item from the previous answer switches to depth');
 assert.match(picked.content[0].text, /\nAnswer it now, in depth: /);
 await ask('[Web user: web]\n台北週末有什麼展覽', '1. 大新營萬聖市集 10/31-11/1');
 await research('c3');
 assert.equal(answer(), 'breadth', 'a new topic after an answer is breadth first again');
 await ask('[Web user: web]\n詳細介紹台積電 2 奈米製程');
 await research('c4');
 assert.equal(answer(), 'depth', 'asking for detail in so many words is depth on any turn');
 await ask('[Web user: web]\n這個是什麼');
 await research('c5');
 assert.equal(answer(), 'breadth', 'a pointer word with no earlier answer is not a pick');
 await ask('好', '需要我再搜尋更多資料嗎？');
 await research('c6');
 assert.equal(answer(), 'depth', 'a yes to the offer to search more goes deeper');
});

test('stale query years move to the current year unless the user asked for that year or the past', async () => {
 const {worker, research, ask} = await setup({user: ''});
 const sent = async (user, q1) => {
  await ask(user);
  await research('c', {...QUERIES, q1});
  return worker.calls.at(-1).params.queries[0];
 };
 assert.equal(await sent('明天台南還有什麼特別的活動', `台南活動 ${Y - 1}年10月4日`), `台南活動 ${Y}年10月4日`);
 assert.equal(await sent('明天台南有什麼活動', `Tainan events October 4 ${Y - 1}`), `Tainan events October 4 ${Y}`);
 assert.equal(await sent(`${Y - 1}年台南煙火有幾場`, `台南 ${Y - 1} 煙火`), `台南 ${Y - 1} 煙火`, 'the user named the year');
 assert.equal(await sent('去年台南煙火有幾場', `台南 ${Y - 1} 煙火`), `台南 ${Y - 1} 煙火`, 'the user asked about the past');
 assert.equal(await sent('颱風統計', `颱風 ${Y - 11} 統計`), `颱風 ${Y - 11} 統計`, 'older than five years is left alone');
 assert.equal(await sent('明天台南', `台南 ${Y} 10月`), `台南 ${Y} 10月`);
 assert.equal(await sent('查訂單', `訂單 1${Y - 1}1004`), `訂單 1${Y - 1}1004`, 'digits inside a longer number are not a year');
});

test('session shutdown closes this session\'s research tabs but keeps the browser_intent window', async () => {
 const {worker, fire, ctx} = await setup();
 let disconnected = false;
 worker.disconnect = () => { disconnected = true; };
 await fire('session_shutdown', {type: 'session_shutdown', reason: 'quit'}, ctx);
 assert.deepEqual(worker.cleaned, ['owner'], 'not intent-owner: the next turn of the conversation needs its page');
 assert.ok(disconnected);
});

test('browser_intent: piweb progress rows, one task at a time per conversation, a Next line after a block', async () => {
 const {worker, tools, ctx} = await setup();
 let active = 0;
 let most = 0;
 worker.request = async (command) => {
  if (command === 'screenshot') return {path: '/nonexistent/shot.jpg'};
  most = Math.max(most, ++active);
  await new Promise(setImmediate);
  active--;
  return {url: 'https://www.dcard.tw/popular', title: 'Just a moment...', snapshot: 'URL: https://www.dcard.tw/popular\nTitle: Just a moment...\n---\n請稍候'};
 };
 const realFetch = globalThis.fetch;
 globalThis.fetch = async () => new Response(JSON.stringify({choices: [{message: {content: '{"action":"stop","status":"blocked","reason":"Cloudflare 驗證頁"}'}}]}));
 try {
  const rows = [];
  const run = () => tools.get('browser_intent').execute('c1', {url: 'https://www.dcard.tw/popular', goal: '熱門文章'}, undefined,
   (update) => rows.push(update.details.message), {...ctx, model: undefined});
  const [first] = await Promise.all([run(), run()]);
  assert.equal(first.details.status, 'TASK_BLOCKED');
  assert.match(first.content[0].text, /Next: tell the user this site blocks automated browsing\. Do not ask to click its verification/);
  assert.equal(first.details.planner, 'qwen3.8-flash (local default)');
  assert.equal(most, 1, 'parallel calls of one conversation drive its window one after the other');
  assert.ok(rows.includes('🎯 瀏覽任務：熱門文章') && rows.some((row) => /^\d+\.\ds 🛑 TASK_BLOCKED: Cloudflare 驗證頁$/.test(row)), rows.join('\n'));
 } finally {
  globalThis.fetch = realFetch;
 }
});
