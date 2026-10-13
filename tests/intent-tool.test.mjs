import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require = createRequire('/home/chihmin/src/pi-agent/package.json');
const { createJiti } = require('jiti');
const jiti = createJiti(import.meta.url, { alias: { typebox: require.resolve('typebox') }, moduleCache: false });
// FULL is read at import time: load the module once per mode.
process.env.LAYA_INTENT_MODE = 'full';
const register = (await jiti.import('../intent/tool.ts')).default;
delete process.env.LAYA_INTENT_MODE;
const goalModule = await jiti.import('../intent/tool.ts');
function setup(reg = register) {
  let tool, ensured = 0;
  reg({ on() {}, registerTool(t) { tool = t; }, async exec() { throw Error('unexpected hub call'); } }, async () => { ensured++; });
  return { tool, ensured: () => ensured, ctx: { sessionManager: { getSessionId: () => 'test' } } };
}
test('resolve is available and verifies by observe without replay', async () => {
  const old = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, opts) => {
    calls.push([url, opts?.body]);
    return Response.json(url.endsWith('/health') ? { ok: true } : { status: 'OBSERVED', url: 'about:blank' });
  };
  try {
    const s = setup();
    assert.match(JSON.stringify(s.tool.parameters), /resolve/);
    const r = await s.tool.execute('1', { action: 'resolve', target: 'ECONNREFUSED' }, undefined, undefined, s.ctx);
    assert.equal(r.details.status, 'RESOLVED');
    assert.equal(s.ensured(), 1);
    assert.deepEqual(calls.filter(c => c[1]).map(c => JSON.parse(c[1]).action), ['observe']);
  } finally { globalThis.fetch = old; }
});
test('lost action stream does not repost the action', async () => {
  const old = globalThis.fetch;
  let posts = 0;
  globalThis.fetch = async (url) => {
    if (url.endsWith('/health')) return Response.json({ ok: true });
    posts++;
    throw Error('connection reset after click');
  };
  try {
    const s = setup();
    const r = await s.tool.execute('1', { action: 'click', target: 'submit' }, undefined, undefined, s.ctx);
    assert.equal(r.details.status, 'EXECUTION_UNCERTAIN');
    assert.equal(posts, 1);
  } finally { globalThis.fetch = old; }
});
test('resolve does not claim success on page observation error', async () => {
  const old = globalThis.fetch;
  globalThis.fetch = async url => Response.json(url.endsWith('/health') ? { ok: true } : { status: 'ERROR', message: 'target closed' });
  try {
    const s = setup();
    const r = await s.tool.execute('1', { action: 'resolve' }, undefined, undefined, s.ctx);
    assert.equal(r.details.status, 'RESOLVE_BLOCKED');
  } finally { globalThis.fetch = old; }
});

test('missing stream route falls back once; successful stream never falls back', async () => {
  const old = globalThis.fetch;
  try {
    for (const missing of [false, true]) {
      const calls = [];
      globalThis.fetch = async url => {
        calls.push(url);
        if (url.endsWith('/health')) return Response.json({ ok: true });
        if (url.endsWith('/act')) return Response.json({ status: 'READ', text: 'fixture' });
        return missing ? new Response('', { status: 404 }) : new Response('data: {"type":"result","data":{"status":"READ","text":"fixture"}}\n\n');
      };
      const s = setup();
      const r = await s.tool.execute('1', { action: 'read' }, undefined, undefined, s.ctx);
      assert.equal(r.details.status, 'READ');
      assert.equal(calls.filter(c => c.endsWith('/act')).length, Number(missing));
    }
  } finally { globalThis.fetch = old; }
});
test('failed preflight never dispatches original action', async () => {
  const old = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async url => { calls.push(url); throw Error('ECONNREFUSED'); };
  try {
    const s = setup();
    const r = await s.tool.execute('1', { action: 'click', target: 'Pay' }, undefined, undefined, s.ctx);
    assert.equal(r.details.status, 'RESOLVE_BLOCKED');
    assert.ok(calls.every(c => c.endsWith('/health')));
  } finally { globalThis.fetch = old; }
});

const streamOf = (...events) => new Response(events.map(e => `data: ${JSON.stringify(e)}\n\n`).join(''));
function captureStream(result, progress = []) {
  const posted = [];
  globalThis.fetch = async (url, opts) => {
    if (url.endsWith('/health')) return Response.json({ ok: true });
    posted.push(JSON.parse(opts.body));
    return streamOf(...progress.map(message => ({ type: 'progress', stage: 'task_step', message })), { type: 'result', data: result });
  };
  return posted;
}
test('goal-only mode exposes only url, goal and confirm and always dispatches a task', async () => {
  const old = globalThis.fetch;
  try {
    assert.equal(goalModule.FULL, false);
    const s = setup(goalModule.default);
    assert.deepEqual(Object.keys(s.tool.parameters.properties).sort(), ['confirm', 'goal', 'url']);
    assert.deepEqual(s.tool.parameters.required, ['goal']);
    const posted = captureStream({ status: 'TASK_DONE', text: 'price $4' }, ['📋 step 1 click "Pricing"', '✓ step 1 CLICKED']);
    const r = await s.tool.execute('1', { url: 'https://example.test/', goal: 'Opus price', action: 'click', target: 'Buy' }, undefined, undefined, s.ctx);
    assert.equal(posted.length, 1);
    assert.equal(posted[0].action, 'task');
    assert.equal(posted[0].target, undefined, 'single-step fields never reach the service');
    assert.equal(r.details.status, 'TASK_DONE');
    assert.equal(r.details.events.length, 2);
    assert.match(r.details.events[0], /^\d+\.\ds 📋 step 1 click "Pricing"$/);
    assert.match(r.content[0].text, /"events"/, 'the event log reaches the agent, not just the UI');
  } finally { globalThis.fetch = old; }
});
test('planner uses the session model only when its API speaks chat/completions', async () => {
  const old = globalThis.fetch;
  try {
    const cases = [
      [{ id: 'claude-opus-5-5', provider: 'anthropic', api: 'anthropic-messages', baseUrl: 'https://api.anthropic.com' }, 'https://api.anthropic.com/v1/chat/completions'],
      [{ id: 'qwen', provider: 'local-llama', api: 'openai-completions', baseUrl: 'http://localhost:8001/v1' }, 'http://localhost:8001/v1/chat/completions'],
      [{ id: 'gemini-3.5-flash', provider: 'gemini', api: 'openai-completions', baseUrl: 'https://generativelanguage.googleapis.com/v1beta/openai/' }, 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'],
      [{ id: 'gpt-5.6-sol', provider: 'openai-codex', api: 'openai-codex-responses', baseUrl: 'https://chatgpt.com/backend-api' }, undefined],
    ];
    for (const [model, endpoint] of cases) {
      const s = setup(goalModule.default);
      const ctx = { ...s.ctx, model, modelRegistry: { getApiKeyAndHeaders: async () => ({ ok: true, apiKey: 'k' }) } };
      const posted = captureStream({ status: 'TASK_DONE' });
      const r = await s.tool.execute('1', { goal: 'g' }, undefined, undefined, ctx);
      assert.equal(posted[0].base_url, endpoint, model.id);
      assert.equal(posted[0].model, endpoint ? model.id : undefined, model.id);
      assert.equal(posted[0].headers?.Authorization, endpoint ? 'Bearer k' : undefined, 'no session credentials leave for an unused model');
      assert.match(r.details.planner, endpoint ? new RegExp(model.id) : /local default planner/);
    }
  } finally { globalThis.fetch = old; }
});
