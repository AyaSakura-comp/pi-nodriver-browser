import assert from 'node:assert/strict';
import { test } from 'node:test';
import { BROWSER_UNIT_PREFIX, forwardedEnv, launchWorker, systemdRunArgs, userManagerOutlivesCaller } from '../worker-launch.ts';

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
