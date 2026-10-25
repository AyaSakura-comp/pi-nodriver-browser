import test from 'node:test';
import assert from 'node:assert/strict';
import { createJiti } from '/home/chihmin/src/pi-agent/node_modules/jiti/lib/jiti.mjs';
const jiti = createJiti(import.meta.url, { moduleCache: false, alias: {
 '@earendil-works/pi-coding-agent': '/home/chihmin/src/pi-agent/packages/coding-agent/dist/index.js',
 'typebox': '/home/chihmin/src/pi-agent/node_modules/typebox/build/index.mjs',
} });
const { default: extension } = await jiti.import('../index.ts');
test('midnight changes only appended date context, never the system prefix or previous message', async () => {
 const hooks = new Map(); const tools = new Map();
 extension({on: (name, handler) => {const list=hooks.get(name)??[]; list.push(handler); hooks.set(name,list);}, registerTool: tool => tools.set(tool.name,tool)});
 const handler = hooks.get('before_agent_start')[0];
 const OriginalDate = globalThis.Date;
 let clock='2026-10-04T15:59:59Z';
 globalThis.Date = class extends OriginalDate { constructor(...args) { super(...(args.length ? args : [clock])); } };
 try {
  const first = await handler({systemPrompt:'static prefix'});
  clock='2026-10-04T16:00:01Z';
  const second = await handler({systemPrompt:'static prefix'});
  assert.equal(first.systemPrompt,undefined,'the extension never edits the system prompt');
  assert.equal(second.systemPrompt,undefined);
  assert.equal(first.message.customType,'browser-date-context');
  assert.equal(first.message.display,false);
  assert.notEqual(first.message.content,second.message.content);
  assert.match(first.message.content,/Today: 2026-10-04/);
  assert.match(second.message.content,/Today: 2026-10-05/);
  assert.ok(!tools.get('research').description.includes("system prompt's Today"));
  for (const tool of tools.values()) {
   assert.equal(tool.promptSnippet,undefined,`${tool.name} must not add to the system prompt tool list`);
   assert.equal(tool.promptGuidelines,undefined,`${tool.name} guidance belongs in its description`);
  }
  assert.match(tools.get('research').description,/Usage rules:\n- research is the default tool/);
  assert.match(tools.get('crawl').description,/MANDATORY URL PROVENANCE RULE/);
 } finally {globalThis.Date=OriginalDate;}
});
