import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require = createRequire('/home/chihmin/src/pi-agent/package.json');
const { createJiti } = require('jiti');
const jiti = createJiti(import.meta.url, { alias: { typebox: require.resolve('typebox') }, moduleCache: false });
const register = (await jiti.import('../intent/tool.ts')).default;
function setup() {
  let tool, ensured = 0;
  register({ on() {}, registerTool(t) { tool = t; }, async exec() { throw Error('unexpected hub call'); } }, async () => { ensured++; });
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
