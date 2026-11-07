import test from 'node:test';
import assert from 'node:assert/strict';
import { message, register } from './fixtures/extension.mjs';

async function load(config) {
 const { fire } = await register({config});
 let branch = [];
 const ctx = {sessionManager: {getSessionId: () => 's1', getBranch: () => branch}};
 const call = async (toolName, input = {urls: ['https://example.com/']}) => (await fire('tool_call', {toolName, input}, ctx)) ?? {};
 // One user turn: optional prior assistant text, research done, then probe tools.
 const turn = async ({assistant = '', user, skill = false, research = true}) => {
  branch = assistant ? [message('user', 'x'), message('assistant', assistant)] : [];
  await fire('input', {text: user}, ctx);
  if (skill) await call('read', {path: '/home/u/.pi/agent/skills/create-image/SKILL.md'});
  if (research) await fire('tool_result', {toolName: 'research', isError: false, content: [{type: 'text', text: 'ok'}]}, ctx);
  return {crawl: Boolean((await call('crawl')).block), research: Boolean((await call('research')).block)};
 };
 return {turn, call, fire, ctx};
}

test('guarded: research done blocks follow-up lookups unless the user asked for more', async () => {
 const {turn} = await load({crawlGuards: true});
 assert.deepEqual(await turn({user: '[Discord user: a]\n台北天氣如何', research: false}).then(r => r.crawl), true);
 assert.deepEqual(await turn({user: '[Discord user: a]\n台北天氣如何'}), {crawl: true, research: true});
 assert.deepEqual(await turn({user: '台南安平一日遊推薦景點'}), {crawl: true, research: true});
 assert.deepEqual(await turn({user: '點開第一個來源'}), {crawl: false, research: false});
 assert.deepEqual(await turn({assistant: '需要我再搜尋更多資料嗎？', user: '[Discord user: a]\n好啊'}), {crawl: false, research: false});
 assert.deepEqual(await turn({assistant: 'Want me to search for more?', user: 'ok'}), {crawl: false, research: false});
 assert.deepEqual(await turn({assistant: '要用這張當頭像嗎？', user: '好啊'}), {crawl: true, research: true});
 assert.deepEqual(await turn({user: '生成兔娘', skill: true}), {crawl: false, research: true});
 assert.deepEqual(await turn({assistant: '要我深度爬取這幾個來源，還是再研究缺的部分？', user: '好'}), {crawl: false, research: false});
 assert.deepEqual(await turn({user: '深入看第二個來源'}), {crawl: false, research: false});
});

test('crawlGuards false exempts only crawl', async () => {
 const {turn} = await load({crawlGuards: false});
 assert.equal((await turn({user: '台北天氣如何', research: false})).crawl, false);
 assert.deepEqual(await turn({user: '台北天氣如何'}), {crawl: false, research: true});
});

test('a failed research still lets crawl run as the fallback', async () => {
 const {call, fire, ctx} = await load({crawlGuards: true});
 await fire('input', {text: '台北天氣如何'}, ctx);
 assert.match((await call('crawl')).reason, /^RESEARCH_FIRST: use the research tool/);
 await fire('tool_result', {toolName: 'research', isError: true, content: [{type: 'text', text: 'research_failed'}]}, ctx);
 assert.equal((await call('crawl')).block, undefined);
});

test('google_search runs only when the user asks for Google; repeated blocked lookups escalate', async () => {
 const {call, fire, ctx} = await load({crawlGuards: false});
 const google = {searches: [{direction: 'd', query: 'q'}]};
 await fire('input', {text: '[Web user: web]\n台北天氣如何'}, ctx);
 assert.match((await call('google_search', google)).reason, /^RESEARCH_FIRST: google_search \(and browser google-search\) only runs/);
 await fire('tool_result', {toolName: 'research', isError: false, content: []}, ctx);
 const first = await call('google_search', google);
 assert.match(first.reason, /^RESEARCH_DONE_GUARD: research already returned enough evidence/);
 assert.match(first.reason, /research already crawled its result pages, so do not crawl on your own either\.$/);
 assert.match((await call('fetch_image', {url: 'https://example.com/a.png'})).reason, /^RESEARCH_DONE_GUARD \(blocked 2 times\): STOP calling tools/);
 await fire('input', {text: '用 Google 搜 台北天氣'}, ctx);
 assert.equal((await call('google_search', google)).block, undefined);
 await fire('tool_result', {toolName: 'research', isError: false, content: []}, ctx);
 assert.equal((await call('google_search', google)).block, undefined, 'asking for Google also asks for more than research');
});

test('after research, one browser_intent per message is allowed (JavaScript pages, actions on a found site); a second is not', async () => {
 const {call, fire, ctx} = await load({crawlGuards: false});
 const intent = async (input) => (await call('browser_intent', input)).block ? 'blocked' : 'allowed';
 const turn = async (text = '鼎泰豐101分店即時叫號') => {
  await fire('input', {text}, ctx);
  await fire('tool_result', {toolName: 'research', isError: false, content: [{type: 'text', text: '[s1] 鼎泰豐 - 現場到號查詢 — https://www.dintaifung.tw/Queue/ 預計停止取號 -'}]}, ctx);
 };
 // Live queue numbers are JavaScript-rendered; research only saw "-".
 await turn();
 assert.equal(await intent({url: 'https://www.dintaifung.tw/Queue/?type=3', goal: 'select 101店'}), 'allowed');
 assert.equal(await intent({url: 'https://www.dintaifung.tw/Queue/', goal: 'again'}), 'blocked', 'only one per message');
 assert.equal((await call('research', {})).block, true, 'research itself stays blocked');
 // 2026-11-04: "幫我關掉" after research found the settings page; the action must be possible.
 await turn('幫我關掉');
 assert.equal(await intent({url: 'https://www.emome.net/', goal: '關閉拒收企業簡訊'}), 'allowed');
 await turn();
 assert.equal(await intent({goal: 'continue on the current page'}), 'allowed', 'goal-only call');
});
