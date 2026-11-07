import test from 'node:test';
import assert from 'node:assert/strict';
import { register } from './fixtures/extension.mjs';

test('midnight changes only appended date context, never the system prefix or previous message', async () => {
 const { tools, fire } = await register();
 const OriginalDate = globalThis.Date;
 let clock='2026-10-04T15:59:59Z';
 globalThis.Date = class extends OriginalDate { constructor(...args) { super(...(args.length ? args : [clock])); } };
 try {
  const first = await fire('before_agent_start', {systemPrompt:'static prefix'});
  clock='2026-10-04T16:00:01Z';
  const second = await fire('before_agent_start', {systemPrompt:'static prefix'});
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
  assert.match(tools.get('crawl').description,/never guess URLs/);
  assert.match(tools.get('browser_intent').description,/Usage rules:\n(?:- .*\n)*- browser_intent takes \{url\?, goal\}/);
 } finally {globalThis.Date=OriginalDate;}
});
