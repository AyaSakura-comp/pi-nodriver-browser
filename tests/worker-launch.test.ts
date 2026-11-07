import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync } from 'node:fs';
import { createServer, type Socket } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { BROWSER_UNIT_PREFIX, WorkerClient, forwardedEnv, launchWorker, systemdRunArgs, userManagerOutlivesCaller } from '../worker-launch.ts';

const spec = {
  root: '/opt/ext', python: '/opt/ext/.venv/bin/python', worker: '/opt/ext/worker.py',
  socket: '/home/u/.pi/agent/nodriver-browser.sock', screen: '1366x768x24',
};
const fakeChild = (pid: number) => ({ pid, unref() {}, on() { return this; } });

test('forwards the Pi environment except display, systemd per-unit and invalid names', () => {
  const env = forwardedEnv({
    PATH: '/usr/bin', HOME: '/home/u', PI_NODRIVER_MAX_TABS: '9', RESEARCH_CRAWL_TIMEOUT: '5',
    TAVILY_API_KEY: 'secret', EMPTY: '', DISPLAY: ':0', XAUTHORITY: '/x', WAYLAND_DISPLAY: 'wayland-0',
    INVOCATION_ID: 'abc', JOURNAL_STREAM: '8:9', MANAGERPID: '1', SYSTEMD_EXEC_PID: '2', NOTIFY_SOCKET: '/run/n',
    LISTEN_FDS: '1', LISTEN_PID: '3', MEMORY_PRESSURE_WATCH: '/sys/x', MEMORY_PRESSURE_WRITE: 'c29tZQ==',
    'BASH_FUNC_f%%': '() { :; }', 'BAD.NAME': '1', NUL_VALUE: 'a\0b', UNDEFINED: undefined,
  });
  assert.deepEqual(Object.keys(env).sort(),
    ['EMPTY', 'HOME', 'PATH', 'PI_NODRIVER_MAX_TABS', 'RESEARCH_CRAWL_TIMEOUT', 'TAVILY_API_KEY']);
  assert.equal(env.TAVILY_API_KEY, 'secret');
});

test('systemd-run argv names the variables but never carries their values', () => {
  const unit = `${BROWSER_UNIT_PREFIX}0123456789ab`;
  const args = systemdRunArgs(spec, unit, ['PATH', 'TAVILY_API_KEY']);
  for (const flag of ['--user', '--collect', '--quiet', `--unit=${unit}`, '--working-directory=/opt/ext',
    '--setenv=PATH', '--setenv=TAVILY_API_KEY']) assert.ok(args.includes(flag), flag);
  assert.ok(args.every((arg) => !/^--setenv=[^=]+=/.test(arg)), 'values must come from the environment, not argv');
  assert.deepEqual(args.slice(args.indexOf('--') + 1),
    ['xvfb-run', '-a', '-s', '-screen 0 1366x768x24', spec.python, spec.worker, '--server', spec.socket]);
});

test('dollar signs in paths survive ExecStart variable expansion', () => {
  // ExecStart expands ${VAR} anywhere in an argument and $VAR as a whole word.
  const args = systemdRunArgs({ ...spec, socket: '/tmp/${HOME}/$USER.sock' }, 'unit', []);
  assert.equal(args.at(-1), '/tmp/$${HOME}/$$USER.sock');
});

test('starts the worker in its own transient user unit with values passed by environment', async () => {
  const calls: Array<{ file: string; args: string[]; options: { env: Record<string, string | undefined> } }> = [];
  const launched = await launchWorker(spec, {
    platform: 'linux', env: { PATH: '/usr/bin', SECRET_TOKEN: 's', DISPLAY: ':1' }, durableManager: async () => true,
    unitName: () => `${BROWSER_UNIT_PREFIX}test`,
    execFile: async (file, args, options) => { calls.push({ file, args, options }); },
    spawn: () => { throw new Error('legacy spawn must not run'); },
  });
  assert.equal(launched.mode, 'systemd');
  assert.equal(launched.unit, `${BROWSER_UNIT_PREFIX}test`);
  assert.equal(calls[0].file, 'systemd-run');
  assert.deepEqual(calls[0].options.env, { PATH: '/usr/bin', SECRET_TOKEN: 's' });
  assert.ok(calls[0].args.includes('--setenv=SECRET_TOKEN'));
  assert.ok(!calls[0].args.some((arg) => arg.includes('=s')), 'secret value leaked into argv');

  launched.stop();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(calls[1], { file: 'systemctl', args: ['--user', 'stop', '--no-block', `${BROWSER_UNIT_PREFIX}test`],
    options: { env: { PATH: '/usr/bin', SECRET_TOKEN: 's' }, timeout: 10_000 } });
});

test('unit names are unique so a replacement never collides with a lingering unit', async () => {
  const units = new Set<string>();
  for (let i = 0; i < 20; i++) {
    const launched = await launchWorker(spec, { platform: 'linux', env: { PATH: '/usr/bin' }, durableManager: async () => true,
      execFile: async () => {} });
    assert.match(launched.unit ?? '', new RegExp(`^${BROWSER_UNIT_PREFIX}[0-9a-f]{12}$`));
    units.add(launched.unit!);
  }
  assert.equal(units.size, 20);
});

test('falls back to the previous detached xvfb-run child when systemd-run fails', async () => {
  const spawned: Array<{ file: string; args: string[]; options: Record<string, unknown> }> = [];
  const launched = await launchWorker(spec, {
    platform: 'linux', env: { PATH: '/usr/bin', WAYLAND_DISPLAY: 'w', DISPLAY: ':1' }, durableManager: async () => true,
    execFile: async () => { throw Object.assign(new Error('Failed to connect to bus'), { code: 1 }); },
    spawn: (file, args, options) => { spawned.push({ file, args, options }); return fakeChild(4242) as never; },
  });
  assert.equal(launched.mode, 'spawn');
  assert.equal(spawned[0].file, 'xvfb-run');
  assert.deepEqual(spawned[0].args, ['-a', '-s', '-screen 0 1366x768x24', spec.python, spec.worker, '--server', spec.socket]);
  // Unchanged legacy contract: full environment minus WAYLAND_DISPLAY, detached, cwd at the extension.
  assert.deepEqual(spawned[0].options.env, { PATH: '/usr/bin', DISPLAY: ':1' });
  assert.equal(spawned[0].options.detached, true);
  assert.equal(spawned[0].options.cwd, '/opt/ext');
  assert.equal(spawned[0].options.stdio, 'ignore');
});

test('PI_NODRIVER_SYSTEMD=0 and non-Linux hosts keep the previous child process', async () => {
  for (const deps of [{ platform: 'linux' as const, env: { PI_NODRIVER_SYSTEMD: '0' }, durableManager: async () => true },
    { platform: 'darwin' as const, env: {}, durableManager: async () => true }]) {
    let systemdCalls = 0;
    const launched = await launchWorker(spec, {
      ...deps, execFile: async () => { systemdCalls++; }, spawn: () => fakeChild(7) as never,
    });
    assert.equal(launched.mode, 'spawn');
    assert.equal(systemdCalls, 0);
  }
});

test('a systemd-run that fails after creating the unit is stopped before the fallback starts', async () => {
  const calls: Array<{ file: string; args: string[] }> = [];
  const spawned: string[] = [];
  const launched = await launchWorker(spec, {
    platform: 'linux', env: { PATH: '/usr/bin' }, durableManager: async () => true,
    unitName: () => `${BROWSER_UNIT_PREFIX}slow`,
    execFile: async (file, args) => {
      calls.push({ file, args });
      if (file === 'systemd-run') throw Object.assign(new Error('timed out'), { killed: true, signal: 'SIGTERM' });
    },
    spawn: (file) => { spawned.push(file); return fakeChild(5) as never; },
  });
  assert.equal(launched.mode, 'spawn');
  assert.deepEqual(spawned, ['xvfb-run']);
  // Two workers would race for the socket lock and the loser could be SIGKILLed mid-start.
  assert.deepEqual(calls[1], { file: 'systemctl', args: ['--user', 'stop', `${BROWSER_UNIT_PREFIX}slow`] });
});

test('keeps the plain child when the user manager may stop before Pi (no linger, login session)', async () => {
  let systemdCalls = 0;
  const launched = await launchWorker(spec, {
    platform: 'linux', env: { PATH: '/usr/bin' }, durableManager: async () => false,
    execFile: async () => { systemdCalls++; }, spawn: () => fakeChild(9) as never,
  });
  assert.equal(launched.mode, 'spawn');
  assert.equal(systemdCalls, 0, 'a unit under a short-lived user manager would die at the last logout');
});

test('user manager durability: service of the manager, or lingering', async () => {
  const service = '0::/user.slice/user-1000.slice/user@1000.service/app.slice/piweb-worker.service\n';
  const login = '0::/user.slice/user-1000.slice/session-4.scope\n';
  const linger = (answer: string) => async () => answer;
  assert.equal(await userManagerOutlivesCaller(service, linger('Linger=no\n')), true);
  assert.equal(await userManagerOutlivesCaller(login, linger('Linger=yes\n')), true);
  assert.equal(await userManagerOutlivesCaller(login, linger('Linger=no\n')), false);
  assert.equal(await userManagerOutlivesCaller(login, async () => { throw new Error('no logind'); }), false);
  assert.equal(await userManagerOutlivesCaller('', linger('Linger=yes\n')), true);
});

// ---- the socket client ------------------------------------------------------------------

const MARKER = '__PI_NODRIVER__';
const tempSocket = () => join(mkdtempSync(join(tmpdir(), 'pi-client-')), 'worker.sock');
const research = { jobId: 'job', question: 'q', queries: ['q'], clock: { iso: '2026-01-01T00:00:00.000Z', timezone: 'UTC' },
  answer: 'breadth' as const, crawlConcurrency: 32, crawlWordBudget: 20000 };

/** A stand-in worker on `path`; answer() decides what each request gets back. */
async function fakeWorker(answer: (request: any, reply: (frame: object) => void, connection: Socket, index: number) => void,
  path = tempSocket()) {
  const received: any[] = [];
  let connections = 0;
  const server = createServer((connection) => {
    const index = connections++;
    let buffer = '';
    connection.on('data', (chunk) => {
      buffer += chunk;
      for (let newline = buffer.indexOf('\n'); newline >= 0; newline = buffer.indexOf('\n')) {
        const request = JSON.parse(buffer.slice(0, newline));
        buffer = buffer.slice(newline + 1);
        received.push(request);
        answer(request, (frame) => connection.write(`${MARKER}${JSON.stringify(frame)}\n`), connection, index);
      }
    });
  });
  await new Promise<void>((resolve) => server.listen(path, resolve));
  return { path, received, connections: () => connections, close: () => new Promise((resolve) => server.close(resolve)) };
}

async function until(condition: () => boolean) {
  for (let i = 0; i < 400 && !condition(); i++) await new Promise((resolve) => setTimeout(resolve, 5));
  assert.ok(condition(), 'condition not reached');
}

test('research progress frames reach only their job and never settle the request', async () => {
  const worker = await fakeWorker((request, reply) => {
    reply({ id: request.id, type: 'progress', phase: 'evidence', jobId: 'other', prefix: 'not mine' });
    reply({ id: request.id, type: 'progress', phase: 'evidence', jobId: request.research.jobId, prefix: 'mine' });
    reply({ id: request.id, ok: true, type: 'result', action: 'research', jobId: request.research.jobId, text: 'complete' });
  });
  const client = new WorkerClient(worker.path);
  const seen: string[] = [];
  const reply = await client.research(research, 's1', new AbortController().signal, (prefix) => seen.push(prefix));
  assert.equal(reply.text, 'complete');
  assert.deepEqual(seen, ['mine']);
  assert.deepEqual(worker.received[0], { id: 1, command: 'research', sessionId: 's1', research });
  client.disconnect();
  await worker.close();
});

test('a cancelled request is cancelled on the worker too', async () => {
  const worker = await fakeWorker(() => {});
  const client = new WorkerClient(worker.path);
  const abort = new AbortController();
  const pending = client.request('open "https://example.com/"', 's1', abort.signal);
  await until(() => worker.received.length === 1);
  abort.abort();
  await assert.rejects(pending, /Browser command cancelled/);
  await until(() => worker.received.length === 2);
  assert.equal(worker.received[1].cancelId, worker.received[0].id);
  assert.equal(worker.received[1].sessionId, 's1');
  client.disconnect();
  await worker.close();
});

test('disconnect settles pending requests at once', async () => {
  const worker = await fakeWorker(() => {});
  const client = new WorkerClient(worker.path);
  const pending = client.research(research, 's1', new AbortController().signal, () => {});
  await until(() => worker.received.length === 1);
  client.disconnect();
  await assert.rejects(pending, /connection closed/);
  await worker.close();
});

test('a command the worker dropped (restart) is replayed once on a new connection; research never is', async () => {
  const worker = await fakeWorker((request, reply, connection, index) => {
    if (index === 0 || request.command === 'research') connection.destroy();
    else reply({ id: request.id, ok: true, text: 'second try' });
  });
  const client = new WorkerClient(worker.path);
  assert.equal((await client.request('snapshot', 's1')).text, 'second try');
  assert.equal(worker.connections(), 2);
  await assert.rejects(client.research(research, 's1', new AbortController().signal, () => {}), /connection closed/);
  assert.equal(worker.received.filter((request) => request.command === 'research').length, 1);
  client.disconnect();
  await worker.close();
});

test('the client starts a worker only when it was given a way to, then connects to it', async () => {
  const missing = new WorkerClient(tempSocket());
  await assert.rejects(missing.request('snapshot', 's1'), /ENOENT/, 'a connect-only client (live harness) never starts one');
  const path = tempSocket();
  let worker: Awaited<ReturnType<typeof fakeWorker>> | undefined;
  const starts: string[] = [];
  const client = new WorkerClient(path, async (socket) => {
    starts.push(socket);
    worker = await fakeWorker((request, reply) => reply({ id: request.id, ok: true, text: 'up' }), socket);
    return { mode: 'spawn', stop() {} };
  });
  assert.equal((await client.request('snapshot', 's1')).text, 'up');
  assert.deepEqual(starts, [path]);
  client.disconnect();
  await worker!.close();
});

test('session-cleanup goes only to sessions this process used', async () => {
  const worker = await fakeWorker((request, reply) => reply({ id: request.id, ok: true }));
  const client = new WorkerClient(worker.path);
  await client.cleanupSession('never-used');
  await client.request('snapshot', 's1');
  await client.cleanupSession('s1');
  await client.cleanupSession('s1');
  assert.deepEqual(worker.received.map((request) => `${request.command} ${request.sessionId}`), ['snapshot s1', 'session-cleanup s1']);
  client.disconnect();
  await worker.close();
});
