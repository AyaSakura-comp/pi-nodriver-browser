// Loads index.ts the way Pi does (jiti, Pi's package and typebox from the local pi-agent
// checkout) and registers it against a fake Pi and an optional fake worker.
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createJiti } from '/home/chihmin/src/pi-agent/node_modules/jiti/lib/jiti.mjs';

const jiti = createJiti(import.meta.url, { moduleCache: false, alias: {
  '@earendil-works/pi-coding-agent': '/home/chihmin/src/pi-agent/packages/coding-agent/dist/index.js',
  'typebox': '/home/chihmin/src/pi-agent/node_modules/typebox/build/index.mjs',
} });

/** config: browser-config.json content (written to a fresh PI_AGENT_DIR); worker: a fake WorkerClient. */
export async function register({ config, worker } = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'pi-nodriver-'));
  if (config) writeFileSync(join(dir, 'browser-config.json'), JSON.stringify(config));
  process.env.PI_AGENT_DIR = dir;
  const { default: extension } = await jiti.import(new URL('../../index.ts', import.meta.url).pathname);
  const hooks = new Map();
  const tools = new Map();
  extension({ on: (name, handler) => hooks.set(name, [...(hooks.get(name) ?? []), handler]),
    registerTool: (tool) => tools.set(tool.name, tool) }, worker);
  const fire = async (name, event, ctx) => {
    let out;
    for (const handler of hooks.get(name) ?? []) out = (await handler(event, ctx)) ?? out;
    return out;
  };
  return { hooks, tools, fire };
}

export const message = (role, text) => ({ type: 'message', message: { role, content: [{ type: 'text', text }] } });
