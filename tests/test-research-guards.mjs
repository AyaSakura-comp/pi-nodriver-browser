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
 assert.deepEqual(await turn({assistant: '需要我再搜尋更多資料嗎？', user: '[Discord user: a]\n好啊'}), {crawl: false, research: false});
 assert.deepEqual(await turn({assistant: 'Want me to search for more?', user: 'ok'}), {crawl: false, research: false});
 assert.deepEqual(await turn({assistant: '要用這張當頭像嗎？', user: '好啊'}), {crawl: true, research: true});
 assert.deepEqual(await turn({user: '生成兔娘', skill: true}), {crawl: false, research: true});
});

test('crawlGuards false exempts only crawl', async () => {
 const turn = await load({crawlGuards: false});
 assert.equal((await turn({user: '台北天氣如何', research: false})).crawl, false);
 assert.deepEqual(await turn({user: '台北天氣如何'}), {crawl: false, research: true});
});
