import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createJiti } from '/home/chihmin/src/pi-agent/node_modules/jiti/lib/jiti.mjs';
const jiti = createJiti(import.meta.url, { moduleCache: false, alias: {
 '@earendil-works/pi-coding-agent': '/home/chihmin/src/pi-agent/packages/coding-agent/dist/index.js',
 'typebox': '/home/chihmin/src/pi-agent/node_modules/typebox/build/index.mjs',
} });

async function load(config) {
 const dir = mkdtempSync(join(tmpdir(), 'pi-guards-'));
 process.env.PI_BROWSER_CONFIG = join(dir, 'browser-config.json');
 writeFileSync(process.env.PI_BROWSER_CONFIG, JSON.stringify(config));
 const { default: extension } = await jiti.import('../index.ts');
 const hooks = new Map();
 extension({on: (name, handler) => hooks.set(name, [...(hooks.get(name) ?? []), handler]), registerTool() {}});
 let branch = [];
 const ctx = {sessionManager: {getSessionId: () => 's1', getBranch: () => branch, getEntries: () => branch}};
 const fire = async (name, event) => { let out; for (const h of hooks.get(name) ?? []) out = (await h(event, ctx)) ?? out; return out; };
 const msg = (role, text) => ({type: 'message', message: {role, content: [{type: 'text', text}]}});
 // One user turn: optional prior assistant text, research done, then probe tools.
 return async ({assistant = '', user, skill = false, research = true}) => {
  branch = assistant ? [msg('user', 'x'), msg('assistant', assistant)] : [];
  await fire('input', {text: user});
  if (skill) await fire('tool_call', {toolName: 'read', input: {path: '/home/u/.pi/agent/skills/create-image/SKILL.md'}});
  if (research) await fire('tool_result', {toolName: 'research', isError: false, content: [{type: 'text', text: 'ok'}]});
  const blocked = async (toolName) => Boolean((await fire('tool_call', {toolName, input: {urls: ['https://example.com/']}}))?.block);
  return {crawl: await blocked('crawl'), research: await blocked('research')};
 };
}

test('guarded: research done blocks follow-up lookups unless the user asked for more', async () => {
 const turn = await load({crawlGuards: true});
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
 const turn = await load({crawlGuards: false});
 assert.equal((await turn({user: '台北天氣如何', research: false})).crawl, false);
 assert.deepEqual(await turn({user: '台北天氣如何'}), {crawl: false, research: true});
});

test('after research, one browser_intent on an exact research URL is allowed; modified URLs and a second call are not', async () => {
 const dir = mkdtempSync(join(tmpdir(), 'pi-guards-'));
 process.env.PI_BROWSER_CONFIG = join(dir, 'browser-config.json');
 writeFileSync(process.env.PI_BROWSER_CONFIG, JSON.stringify({crawlGuards: false}));
 const { default: extension } = await jiti.import('../index.ts');
 const hooks = new Map();
 extension({on: (name, handler) => hooks.set(name, [...(hooks.get(name) ?? []), handler]), registerTool() {}});
 const ctx = {sessionManager: {getSessionId: () => 's1', getBranch: () => [], getEntries: () => []}};
 const fire = async (name, event) => { let out; for (const h of hooks.get(name) ?? []) out = (await h(event, ctx)) ?? out; return out; };
 const intent = async (input) => (await fire('tool_call', {toolName: 'browser_intent', input}))?.block ? 'blocked' : 'allowed';
 const turn = async () => {
  await fire('input', {text: '鼎泰豐101分店即時叫號'});
  await fire('tool_result', {toolName: 'research', isError: false, content: [{type: 'text', text: '[s1] 鼎泰豐 - 現場到號查詢 — https://www.dintaifung.tw/Queue/ 預計停止取號 -'}]});
 };
 // The real failure: live queue numbers are JavaScript-rendered, research only saw "-".
 await turn();
 const reason = (await fire('tool_call', {toolName: 'browser_intent', input: {url: 'https://www.dintaifung.tw/Queue/?type=3', goal: 'select 101店'}}))?.reason ?? '';
 assert.match(reason, /^RESEARCH_DONE_GUARD: .*ONE browser_intent call whose url is copied exactly/, 'a modified URL is not a research URL, and the block says how to do it right');
 assert.equal(await intent({url: 'https://www.dintaifung.tw/Queue/', goal: 'select 101店'}), 'allowed', 'the corrected retry in the same message still works');
 await turn();
 assert.equal(await intent({url: 'https://www.dintaifung.tw/Queue/', goal: 'select 101店 and read the live queue numbers'}), 'allowed');
 assert.equal(await intent({url: 'https://www.dintaifung.tw/Queue/', goal: 'again'}), 'blocked', 'only one per message');
 await turn();
 assert.equal(await intent({goal: 'read live numbers at https://www.dintaifung.tw/Queue/'}), 'allowed', 'URL inside a goal-only call');
 await turn();
 assert.equal(await intent({goal: 'continue on the current page'}), 'blocked', 'no URL: nothing tied to research');
 assert.equal((await fire('tool_call', {toolName: 'research', input: {}}))?.block, true, 'research itself stays blocked');
});
