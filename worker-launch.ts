/**
 * Starts the shared browser worker (xvfb-run → worker.py → Chrome).
 *
 * One worker serves every Pi process through a single Unix socket, so it must not live
 * in the cgroup of whichever Pi process happened to start it. A plain child of Pi lands
 * in the caller's systemd unit (for example piweb-worker.service). Restarting that unit
 * SIGTERMs Xvfb and every Chrome helper (zygotes, GPU, network, renderers) at once, and
 * the shared Chrome exits with them: Chrome moves only its own browser process into an
 * app-*.scope, the helpers stay behind (reproduced 2026-10-13: Chrome exited 10 ms after
 * the stop; see docs/shared-browser-restart-isolation.md).
 *
 * On Linux the worker therefore gets its own transient unit when the user systemd manager
 * outlives the caller (the caller runs under it, or the user lingers). Otherwise (no
 * manager: containers, CI, other platforms; a login session without lingering, whose
 * manager logind stops at the last logout; or PI_NODRIVER_SYSTEMD=0) it starts as the
 * previous detached child process.
 */
import { execFile as execFileCallback, spawn as spawnProcess, type SpawnOptions } from "node:child_process";
import { randomBytes } from "node:crypto";
import { readFile } from "node:fs/promises";
import { userInfo } from "node:os";
import { promisify } from "node:util";

export const BROWSER_UNIT_PREFIX = "pi-nodriver-browser-";

export interface WorkerLaunchSpec {
  root: string;
  python: string;
  worker: string;
  socket: string;
  screen: string;
}

export interface LaunchedWorker {
  mode: "systemd" | "spawn";
  unit?: string;
  /** Abandon a worker whose socket never appeared. */
  stop(): void;
}

type Env = Record<string, string | undefined>;
type ExecFile = (file: string, args: string[], options: { env: Env; timeout: number }) => Promise<unknown>;
interface ChildLike { pid?: number; unref(): void; on(event: "error", listener: (error: Error) => void): unknown }

export interface LaunchDeps {
  env?: Env;
  platform?: NodeJS.Platform;
  execFile?: ExecFile;
  spawn?: (file: string, args: string[], options: SpawnOptions) => ChildLike;
  unitName?: () => string;
  /** Whether the user manager outlives this process; default reads /proc and logind. */
  durableManager?: () => Promise<boolean>;
}

/**
 * A transient unit lives exactly as long as the user manager. That manager is stopped by
 * logind at the user's last logout unless the user lingers, so a browser unit started from
 * a login session (SSH, tmux in a terminal) could die while Pi keeps running, which a
 * detached child would survive. Use a unit only when that cannot happen.
 */
export async function userManagerOutlivesCaller(
  cgroup: string, showLinger: () => Promise<string>,
): Promise<boolean> {
  // The caller is itself managed by the user manager: it cannot outlive it either.
  if (/\/user@\d+\.service\//.test(cgroup)) return true;
  // Unknown cgroup layout: systemd-run's own failure decides, as before.
  if (!/\/session-[^/]+\.scope$/m.test(cgroup)) return true;
  try {
    return /^Linger=yes$/m.test(await showLinger());
  } catch {
    return false;
  }
}

/** Variables systemd sets per unit; a copy from the caller's unit would mislabel the new one. */
const SYSTEMD_PER_UNIT = new Set([
  "INVOCATION_ID", "JOURNAL_STREAM", "MANAGERPID", "SYSTEMD_EXEC_PID", "NOTIFY_SOCKET",
  "LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES", "WATCHDOG_PID", "WATCHDOG_USEC",
  "MEMORY_PRESSURE_WATCH", "MEMORY_PRESSURE_WRITE", "TRIGGER_UNIT", "TRIGGER_PATH",
  "TRIGGER_TIMER_REALTIME_USEC", "TRIGGER_TIMER_MONOTONIC_USEC", "MONITOR_SERVICE_RESULT",
  "MONITOR_EXIT_CODE", "MONITOR_EXIT_STATUS", "MONITOR_INVOCATION_ID", "MONITOR_UNIT",
  "CREDENTIALS_DIRECTORY", "RUNTIME_DIRECTORY", "STATE_DIRECTORY", "CACHE_DIRECTORY",
  "LOGS_DIRECTORY", "CONFIGURATION_DIRECTORY", "PIDFILE", "MAINPID", "SERVICE_RESULT",
  "EXIT_CODE", "EXIT_STATUS",
]);
/** xvfb-run provides the display; the legacy child already dropped WAYLAND_DISPLAY. */
const DISPLAY_VARIABLES = new Set(["DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY"]);
/** systemd rejects other names (e.g. exported bash functions) and would refuse the whole unit. */
const VALID_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** The caller's environment, as the legacy child received it, minus what cannot or must not cross units. */
export function forwardedEnv(env: Env): Record<string, string> {
  const forwarded: Record<string, string> = {};
  for (const [name, value] of Object.entries(env)) {
    if (value === undefined || value.includes("\0") || !VALID_NAME.test(name)) continue;
    if (SYSTEMD_PER_UNIT.has(name) || DISPLAY_VARIABLES.has(name)) continue;
    forwarded[name] = value;
  }
  return forwarded;
}

/** ExecStart expands $VAR and ${VAR}; "$$" is a literal dollar sign. (A replacer function:
 *  in a replacement *string*, "$$" would itself collapse to a single "$".) */
const literal = (arg: string) => arg.replaceAll("$", () => "$$");

/**
 * `--setenv=NAME` without a value makes systemd-run copy NAME from its own environment,
 * so secrets never appear in argv (world-readable /proc/<pid>/cmdline).
 */
export function systemdRunArgs(spec: WorkerLaunchSpec, unit: string, envNames: string[]): string[] {
  return [
    "--user", "--collect", "--quiet", `--unit=${unit}`,
    "--description=Pi nodriver browser (Xvfb, worker and Chrome; outlives the Pi process that started it)",
    `--working-directory=${spec.root}`,
    // The worker closes its clients on SIGTERM; never wait the default 90 s.
    "--property=TimeoutStopSec=20",
    ...envNames.map((name) => `--setenv=${name}`),
    "--",
    "xvfb-run", "-a", "-s", `-screen 0 ${spec.screen}`,
    ...[spec.python, spec.worker, "--server", spec.socket].map(literal),
  ];
}

const execFilePromise = promisify(execFileCallback) as unknown as ExecFile;

const defaultDurableManager = (execFile: ExecFile, env: Env) => async () => userManagerOutlivesCaller(
  await readFile("/proc/self/cgroup", "utf8").catch(() => ""),
  async () => String(await execFile("loginctl", ["show-user", userInfo().username, "--property=Linger"],
    { env, timeout: 3_000 }).then((result) => (result as { stdout?: unknown }).stdout ?? "")));

export async function launchWorker(spec: WorkerLaunchSpec, deps: LaunchDeps = {}): Promise<LaunchedWorker> {
  const env = deps.env ?? process.env;
  const execFile = deps.execFile ?? execFilePromise;
  if ((deps.platform ?? process.platform) === "linux" && env.PI_NODRIVER_SYSTEMD !== "0" &&
      await (deps.durableManager ?? defaultDurableManager(execFile, env))()) {
    const unit = deps.unitName?.() ?? `${BROWSER_UNIT_PREFIX}${randomBytes(6).toString("hex")}`;
    const unitEnv = forwardedEnv(env);
    try {
      await execFile("systemd-run", systemdRunArgs(spec, unit, Object.keys(unitEnv)), { env: unitEnv, timeout: 10_000 });
      return {
        mode: "systemd",
        unit,
        stop: () => {
          execFile("systemctl", ["--user", "stop", "--no-block", unit], { env: unitEnv, timeout: 10_000 }).catch(() => {});
        },
      };
    } catch {
      // No user manager (container, CI, SSH session): fall back to the plain child. A timed-out
      // systemd-run may still have created the unit; stop it first so two workers never race
      // for the socket lock (the loser would be SIGKILLed mid-start). Unknown units: no-op.
      await execFile("systemctl", ["--user", "stop", unit], { env: unitEnv, timeout: 10_000 }).catch(() => {});
    }
  }
  const childEnv: Env = { ...env };
  delete childEnv.WAYLAND_DISPLAY;
  const child = (deps.spawn ?? spawnProcess)(
    "xvfb-run",
    ["-a", "-s", `-screen 0 ${spec.screen}`, spec.python, spec.worker, "--server", spec.socket],
    { cwd: spec.root, stdio: "ignore", detached: true, env: childEnv },
  );
  // A missing xvfb-run must surface as "did not start", not crash Pi with an unhandled error.
  child.on("error", () => {});
  child.unref();
  return {
    mode: "spawn",
    stop: () => {
      if (!child.pid) return;
      try {
        process.kill(-child.pid, "SIGTERM");
      } catch {
        // Process may already have exited while the final connection attempt ran.
      }
    },
  };
}
