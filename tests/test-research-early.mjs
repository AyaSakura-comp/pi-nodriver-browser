// Early answer: research returns the search snippets once every query has its results, the pages keep
// loading, and research_details returns the evidence after what research gave (the conversation only grows).
import test from 'node:test';
import assert from 'node:assert/strict';
import { register } from './fixtures/extension.mjs';

const QUERIES = {q1: 'q1', q2: 'q2', q3: 'q3', q4: 'q4'};
const HEAD = 'date\n# Research evidence\n';
const SNIPPETS = HEAD + '\n## Top search results\n- [s1] Title — https://a.example: snippet\n';
const PAGES = SNIPPETS + '\n## [s1] Title — https://a.example\npage passage\n';
const PACKET = PAGES + '\nclosing rules\n';

/** A worker whose evidence frames and reply the test releases step by step. */
function heldWorker() {
 let reply, onEvidence;
 const worker = {
  calls: [],
  research(params, sessionId, signal, callback) {
   worker.calls.push({params, signal});
   onEvidence = callback;
   return new Promise((resolve, reject) => {
    reply = resolve;
    signal.addEventListener('abort', () => reject(new Error('aborted')), {once: true});
   });
  },
  frame: (prefix, searched) => onEvidence(prefix, searched),
  finish: (text = PACKET) => reply({action: 'research', jobId: worker.calls.at(-1).params.jobId, status: 'collected',
   fullEvidenceDelivered: true, text}),
  async addQuery() {}, async request() { return {}; }, async cleanupSession() {}, disconnect() {},
 };
 return worker;
}

async function setup({prefill} = {}) {
 process.env.RESEARCH_ACTIVE_PREFILL = prefill ? '1' : '0';
 delete process.env.RESEARCH_EARLY_ANSWER;
 const worker = heldWorker();
 const {tools, fire} = await register({worker});
 const ctx = {getContextUsage: () => ({tokens: 1000, contextWindow: 200000}),
  sessionManager: {getSessionId: () => 'owner', getBranch: () => []}, ...(prefill && {prefill})};
 await fire('input', {text: '使用者的問題'}, ctx);
 const tick = () => new Promise(setImmediate);
 return {worker, tools, fire, ctx, tick,
  research: (id, onUpdate) => tools.get('research').execute(id, QUERIES, undefined, onUpdate, ctx),
  details: (id, onUpdate) => tools.get('research_details').execute(id, {}, undefined, onUpdate, ctx)};
}

test('snippets first once the searches are in, then the page evidence after them', async () => {
 const {worker, research, details, tick} = await setup();
 const first = research('call-1');
 await tick();
 worker.frame(HEAD, false);
 worker.frame(SNIPPETS, false);
 await tick();
 worker.frame(SNIPPETS, true);
 const early = await first;
 assert.ok(early.content[0].text.startsWith(SNIPPETS), 'the snippets as committed');
 assert.match(early.content[0].text, /preliminary answer/);
 assert.match(early.content[0].text, /research_details/);
 assert.equal(early.details.phase, 'preliminary');
 assert.equal(worker.calls[0].signal.aborted, false, 'the pages keep loading');
 const updates = [];
 const rest = details('call-2', (u) => updates.push(u.content[0].text));
 await tick();
 worker.frame(PAGES, true);
 worker.finish();
 const result = await rest;
 assert.deepEqual(updates, [PAGES.slice(SNIPPETS.length)], 'only the evidence after the snippets');
 assert.ok(result.content[0].text.startsWith(PACKET.slice(SNIPPETS.length)), 'the packet after the snippets');
 assert.match(result.content[0].text, /The user's question: «使用者的問題»/);
 assert.equal(result.details.status, 'collected');
 const none = await details('call-3');
 assert.equal(none.details.status, 'none', 'nothing pending: the earlier result is all the evidence');
});

test('a job that finishes before its searches report goes out whole', async () => {
 const {worker, research, tick} = await setup();
 const first = research('call-1');
 await tick();
 worker.frame(PAGES, false);
 worker.finish();
 const result = await first;
 assert.ok(result.content[0].text.startsWith(PACKET));
 assert.equal(result.details.phase, undefined);
});

test('without a snippet the full packet is awaited', async (t) => {
 const {worker, research, tick} = await setup();
 const first = research('call-1');
 await tick();
 worker.frame(HEAD, true);
 await tick();
 worker.finish();
 const result = await first;
 assert.ok(result.content[0].text.startsWith(PACKET), 'no snippet to answer from: no preliminary step');
});

test('after the wait limit the snippets so far go out', async (t) => {
 t.mock.timers.enable({apis: ['setTimeout']});
 const {worker, research, tick} = await setup();
 const first = research('call-1');
 await tick();
 worker.frame(SNIPPETS, false);
 t.mock.timers.tick(1500);
 t.mock.timers.reset();
 const early = await first;
 assert.equal(early.details.phase, 'preliminary');
 worker.finish();
});

test('the next user message cancels a crawl nobody asked for', async () => {
 const {worker, research, fire, ctx, tick, details} = await setup();
 const first = research('call-1');
 await tick();
 worker.frame(SNIPPETS, true);
 await first;
 await fire('input', {text: '下一個問題'}, ctx);
 assert.equal(worker.calls[0].signal.aborted, true);
 assert.equal((await details('call-2')).details.status, 'none');
});

test('prefill: research_details warms only what follows the snippets, in order', async () => {
 const log = [];
 const handle = (id) => ({append: (text) => { log.push([id, 'append', text]); return true; }, end: () => log.push([id, 'end']),
  cancel: (reason) => log.push([id, 'cancel', reason]), stats: () => ({state: 'ended'})});
 const {worker, research, details, tick} = await setup({prefill: {begin: (id) => { log.push([id, 'begin']); return handle(id); }}});
 const first = research('call-1');
 await tick();
 worker.frame(SNIPPETS, true);
 await first;
 assert.deepEqual(log, [['call-1', 'begin'], ['call-1', 'append', SNIPPETS], ['call-1', 'end']]);
 log.length = 0;
 const mid = SNIPPETS + '\n## [s1] Title — https://a.example\n';
 worker.frame(mid, true);  // arrives between the two calls: replayed when research_details starts
 const rest = details('call-2');
 await tick();
 worker.frame(PAGES, true);
 worker.finish();
 await rest;
 assert.deepEqual(log, [['call-2', 'begin'], ['call-2', 'append', mid.slice(SNIPPETS.length)],
  ['call-2', 'append', PAGES.slice(mid.length)], ['call-2', 'end']]);
});

test('RESEARCH_EARLY_ANSWER=0 keeps the one-step research', async () => {
 const {worker, tools, ctx, tick} = await setup();
 process.env.RESEARCH_EARLY_ANSWER = '0';
 const fresh = await register({worker});
 delete process.env.RESEARCH_EARLY_ANSWER;
 const first = fresh.tools.get('research').execute('call-1', QUERIES, undefined, undefined, ctx);
 await tick();
 worker.frame(SNIPPETS, true);
 await tick();
 worker.finish();
 assert.ok((await first).content[0].text.startsWith(PACKET));
 assert.ok(tools.has('research_details'));
});
