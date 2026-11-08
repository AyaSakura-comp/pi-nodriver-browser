/**
 * The shared browser worker (xvfb-run → worker.py → Chrome): how it is started, and the one
 * socket client every tool uses.
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
import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { createConnection, type Socket } from "node:net";
import { homedir, userInfo } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
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

const ROOT = fileURLToPath(new URL(".", import.meta.url));
export const SOCKET = process.env.PI_NODRIVER_SOCKET || join(homedir(), ".pi", "agent", "nodriver-browser.sock");

/** Starts this extension's worker.py serving `socket`. */
export async function startWorker(socket: string): Promise<LaunchedWorker> {
  const python = join(ROOT, ".venv", "bin", "python");
  if (!existsSync(python)) throw new Error(`The browser worker's Python environment is missing (${python}); run install.sh.`);
  return launchWorker({ root: ROOT, python, worker: join(ROOT, "worker.py"), socket, screen: "1366x768x24" });
}

const MARKER = "__PI_NODRIVER__";

export type WorkerResponse = { id: number; ok: boolean; text?: string; error?: string; [key: string]: any };

/** The research request; the model writes only the queries, the tool fixes everything else. */
export interface ResearchParams {
  jobId: string;
  question: string;
  queries: string[];
  clock: { iso: string; timezone: string };
  answer: "breadth" | "depth";
  crawlConcurrency: number;
  crawlWordBudget: number;
  /** Queries after the first arrive by research-add-query while the call is still being written. */
  streaming?: boolean;
  /** Latency budget: deliver what is committed this many ms after the job starts. */
  deadlineMs?: number;
  /** Rough tokens of evidence passages (default 6000). */
  evidenceBudget?: number;
  /** How long a finished job waits for images still downloading (default 1000). */
  imageWaitMs?: number;
}

function connect(path: string): Promise<Socket> {
  return new Promise((resolve, reject) => {
    const socket = createConnection(path);
    const timer = setTimeout(() => {
      socket.destroy();
      reject(new Error(`Timed out connecting to browser daemon: ${path}`));
    }, 500);
    socket.once("connect", () => {
      clearTimeout(timer);
      resolve(socket);
    });
    socket.once("error", (error) => {
      clearTimeout(timer);
      socket.destroy();
      reject(error);
    });
  });
}

type Pending = { onFrame?: (frame: WorkerResponse) => void; settle: (error?: Error, reply?: WorkerResponse) => void };

/**
 * This process's one connection to the worker. A request is one JSON line
 * {id, command, sessionId, research?}; its reply is one line MARKER + JSON with the same id,
 * which research precedes with progress frames. {cancelId} cancels a request.
 */
export class WorkerClient {
  private readonly path: string;
  private readonly start?: (socket: string) => Promise<LaunchedWorker>;
  private socket?: Socket;
  private connecting?: Promise<Socket>;
  private nextId = 1;
  private readonly pending = new Map<number, Pending>();
  /** Sessions this process used: only they get a session-cleanup, which must not start a worker. */
  private readonly sessions = new Set<string>();

  /** Without `start` the client only connects (the live harness must never start a worker). */
  constructor(path: string, start?: (socket: string) => Promise<LaunchedWorker>) {
    this.path = path;
    this.start = start;
  }

  /** A browser command; replayed once on a new connection when the worker dropped it (restart). */
  async request(command: string, sessionId: string, signal?: AbortSignal): Promise<WorkerResponse> {
    this.sessions.add(sessionId);
    try {
      return await this.send(command, sessionId, signal);
    } catch (error) {
      if (!/connection closed|Timed out connecting/.test(String((error as Error).message))) throw error;
      return this.send(command, sessionId, signal);
    }
  }

  /** One research job; its evidence frames go to onEvidence. No replay: a lost reply may already have used its searches. */
  research(params: ResearchParams, sessionId: string, signal: AbortSignal, onEvidence: (prefix: string) => void) {
    this.sessions.add(sessionId);
    return this.send("research", sessionId, signal, params, (frame) => {
      if (frame.phase === "evidence" && frame.jobId === params.jobId && typeof frame.prefix === "string") onEvidence(frame.prefix);
    });
  }

  /** Delivers {jobId, query, index} or {jobId, done: true} to a streaming research job. */
  async addQuery(sessionId: string, payload: Record<string, unknown>) {
    await this.send("research-add-query", sessionId, undefined, payload);
  }

  async cleanupSession(sessionId: string) {
    if (this.sessions.delete(sessionId)) await this.send("session-cleanup", sessionId);
  }

  disconnect() {
    const socket = this.socket;
    this.socket = undefined;
    for (const request of [...this.pending.values()]) request.settle(new Error("Browser daemon connection closed"));
    socket?.destroy();
  }

  private async send(command: string, sessionId: string, signal?: AbortSignal, research?: object,
    onFrame?: Pending["onFrame"]): Promise<WorkerResponse> {
    const socket = await this.connection();
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const settle = (error?: Error, reply?: WorkerResponse, cancel = false) => {
        if (!this.pending.delete(id)) return;
        clearTimeout(timer);
        signal?.removeEventListener("abort", abort);
        if (cancel) socket.write(`${JSON.stringify({ id: this.nextId++, cancelId: id, sessionId })}\n`);
        if (error) reject(error);
        else resolve(reply!);
      };
      const abort = () => settle(new Error("Browser command cancelled"), undefined, true);
      const timer = setTimeout(() => settle(new Error(`Browser command timed out: ${command}`), undefined, true),
        research ? 300_000 : 90_000);
      this.pending.set(id, { onFrame, settle });
      if (signal?.aborted) return abort();
      signal?.addEventListener("abort", abort, { once: true });
      socket.write(`${JSON.stringify({ id, command, sessionId, ...(research && { research }) })}\n`, (error) => {
        if (error) settle(error);
      });
    });
  }

  private connection(): Promise<Socket> {
    if (this.socket && !this.socket.destroyed) return Promise.resolve(this.socket);
    return this.connecting ??= this.connectOrStart().finally(() => {
      this.connecting = undefined;
    });
  }

  private async connectOrStart(): Promise<Socket> {
    try {
      return this.attach(await connect(this.path));
    } catch (error) {
      if (!this.start) throw error;
    }
    // The worker unlinks a stale socket itself once it holds the socket's lock.
    const launched = await this.start(this.path);
    let lastError: unknown;
    for (let attempt = 0; attempt < 100; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, 100));
      try {
        return this.attach(await connect(this.path));
      } catch (error) {
        lastError = error;
      }
    }
    launched.stop();
    throw lastError;
  }

  private attach(socket: Socket): Socket {
    this.socket = socket;
    createInterface({ input: socket }).on("line", (line) => {
      if (!line.startsWith(MARKER)) return;
      let reply: WorkerResponse;
      try {
        reply = JSON.parse(line.slice(MARKER.length));
      } catch {
        return;
      }
      const request = this.pending.get(reply.id);
      if (reply.type === "progress") request?.onFrame?.(reply);
      else request?.settle(reply.ok ? undefined : new Error(reply.error || "Nodriver command failed"), reply);
    });
    socket.on("error", () => {});
    socket.on("close", () => {
      if (this.socket !== socket) return;
      this.socket = undefined;
      for (const request of [...this.pending.values()]) request.settle(new Error("Browser daemon connection closed"));
    });
    return socket;
  }
}
