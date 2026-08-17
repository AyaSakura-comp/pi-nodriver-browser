import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { extname, join } from "node:path";
import { createConnection, type Socket } from "node:net";
import { fileURLToPath } from "node:url";
import { createInterface } from "node:readline";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { DEFAULT_MAX_BYTES, DEFAULT_MAX_LINES, formatSize, truncateHead } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const ROOT = fileURLToPath(new URL(".", import.meta.url));
const PYTHON = join(ROOT, ".venv", "bin", "python");
const WORKER = join(ROOT, "worker.py");
const MARKER = "__PI_NODRIVER__";
const SOCKET = process.env.PI_NODRIVER_SOCKET || join(homedir(), ".pi", "agent", "nodriver-browser.sock");

const DESCRIPTION = `Browser automation through a persistent, headful Google Chrome controlled by Nodriver under Xvfb.
Workflow: open URL → snapshot -i (get current-viewport @refs like @e1) → interact → re-snapshot after page changes.
Commands:
  open <url> - Navigate to URL
  snapshot -i - List interactive elements in the current viewport with compact @refs
  snapshot -i --full - Return a visual full-page overview only; then scroll and snapshot each relevant viewport
  click <@ref> - Click a snapshot element, including custom controls and open Shadow DOM
  click <x> <y> - Click viewport coordinates (fallback for canvas, cross-origin iframes, and visual-only controls)
  click-text <text> - Click the best visible element matching text or accessible label
  click-css <selector> - Click the first visible element matching a CSS selector, including open Shadow DOM
  click-js <@ref> - Dispatch a deferred DOM click when a site's native mouse handler poisons CDP
  download-info <@ref> - Inspect a download target without clicking it
  download <@ref> [ms] - Click and wait for a verified completed download
  wait-download [ms] - Wait for the active or most recent download
  downloads [limit] - List recent files and in-progress download percentages
  download-latest - Return metadata and the absolute path of the newest completed file
  fill <@ref> <text> - Clear and type
  type <@ref> <text> - Type without clearing
  select <@ref> <value> - Select dropdown option
  press <key> - Press Enter, Tab, Space, Backspace, or text
  scroll <up|down|left|right> [px] - Scroll page
  get text|url|title [@ref] - Get information
  wait <@ref|ms> - Wait for an element or milliseconds
  wait-popup [ms] - Wait for an OAuth/login popup and switch to it
  wait-popup-close [ms] - Wait for the active popup to close and return to its opener
  switch opener - Return to the popup's opener without closing the popup
  dismiss overlays [--cookies=accept|reject-optional|ignore] - Safely dismiss cookie and modal overlays
  screenshot [--full] - Capture screenshot and return it inline
  close - Close only the current Pi session tab
  shutdown - Close Chrome and stop the persistent browser daemon
Use quoted text when an argument contains spaces. Re-run snapshot -i after navigation or major DOM changes. A missing/stale ref automatically returns a fresh DOM snapshot plus a viewport JPG for joint visual inspection, without performing the action; ref-based commands remain blocked until you run snapshot -i, so never retry the old ref.`;

type WorkerResponse = {
  id: number;
  ok: boolean;
  text?: string;
  action?: string;
  screenshotPath?: string;
  error?: string;
  [key: string]: unknown;
};

class NodriverWorker {
  private socket?: Socket;
  private connecting?: Promise<Socket>;
  private nextId = 1;
  private pending = new Map<number, {
    resolve: (value: WorkerResponse) => void;
    reject: (error: Error) => void;
    timer: NodeJS.Timeout;
    removeAbortListener: () => void;
  }>();

  private openSocket(): Promise<Socket> {
    return new Promise((resolve, reject) => {
      const socket = createConnection(SOCKET);
      const timer = setTimeout(() => {
        socket.destroy();
        reject(new Error(`Timed out connecting to browser daemon: ${SOCKET}`));
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

  private attach(socket: Socket) {
    this.socket = socket;
    const lines = createInterface({ input: socket });
    lines.on("line", (line) => {
      if (!line.startsWith(MARKER)) return;
      let response: WorkerResponse;
      try {
        response = JSON.parse(line.slice(MARKER.length));
      } catch {
        return;
      }
      const request = this.pending.get(response.id);
      if (!request) return;
      clearTimeout(request.timer);
      request.removeAbortListener();
      this.pending.delete(response.id);
      if (response.ok) request.resolve(response);
      else request.reject(new Error(response.error || "Nodriver command failed"));
    });
    socket.on("error", () => undefined);
    socket.on("close", () => {
      if (this.socket !== socket) return;
      this.socket = undefined;
      const error = new Error("Browser daemon connection closed");
      for (const request of this.pending.values()) {
        clearTimeout(request.timer);
        request.removeAbortListener();
        request.reject(error);
      }
      this.pending.clear();
    });
  }

  private async connectOrStart(): Promise<Socket> {
    if (!existsSync(PYTHON)) {
      throw new Error(`Nodriver browser environment is missing. Run: python3 -m venv ${join(ROOT, ".venv")} && ${PYTHON} -m pip install nodriver`);
    }
    let child: ReturnType<typeof spawn> | undefined;
    try {
      const socket = await this.openSocket();
      this.attach(socket);
      return socket;
    } catch {
      child = spawn(
        "xvfb-run",
        ["-a", "-s", "-screen 0 960x720x24", PYTHON, WORKER, "--server", SOCKET],
        { cwd: ROOT, stdio: "ignore", detached: true },
      );
      child.unref();
    }

    let lastError: unknown;
    for (let attempt = 0; attempt < 100; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, 100));
      try {
        const socket = await this.openSocket();
        this.attach(socket);
        return socket;
      } catch (error) {
        lastError = error;
      }
    }
    if (child?.pid) {
      try {
        process.kill(-child.pid, "SIGTERM");
      } catch {
        // Process may already have exited while the final connection attempt ran.
      }
    }
    throw lastError instanceof Error ? lastError : new Error("Browser daemon did not start");
  }

  private async connection(): Promise<Socket> {
    if (this.socket && !this.socket.destroyed) return this.socket;
    if (!this.connecting) {
      this.connecting = this.connectOrStart().finally(() => {
        this.connecting = undefined;
      });
    }
    return this.connecting;
  }

  async request(command: string, sessionId: string, signal?: AbortSignal): Promise<WorkerResponse> {
    const socket = await this.connection();
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      let settled = false;
      const sendCancel = () => {
        const cancelId = this.nextId++;
        socket.write(`${JSON.stringify({ id: cancelId, cancelId: id, sessionId })}\n`);
      };
      const finishWithError = (error: Error, cancel: boolean) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        signal?.removeEventListener("abort", abort);
        this.pending.delete(id);
        if (cancel) sendCancel();
        reject(error);
      };
      const abort = () => finishWithError(new Error("Browser command cancelled"), true);
      const timer = setTimeout(
        () => finishWithError(new Error(`Browser command timed out: ${command}`), true),
        90_000,
      );
      const removeAbortListener = () => signal?.removeEventListener("abort", abort);
      this.pending.set(id, { resolve, reject, timer, removeAbortListener });
      signal?.addEventListener("abort", abort, { once: true });

      if (signal?.aborted) {
        abort();
        return;
      }
      socket.write(`${JSON.stringify({ id, command, sessionId })}\n`, (error) => {
        if (error) finishWithError(error, false);
      });
    });
  }

  disconnect() {
    const socket = this.socket;
    this.socket = undefined;
    socket?.destroy();
  }
}

export default function (pi: ExtensionAPI) {
  const worker = new NodriverWorker();
  let queue = Promise.resolve<unknown>(undefined);

  pi.registerTool({
    name: "browser",
    label: "Browser (Nodriver + Xvfb)",
    description: DESCRIPTION,
    promptSnippet: "Interact with web pages using a persistent Nodriver-controlled Chrome browser",
    promptGuidelines: [
      "Use browser for interactive web tasks that require clicking, typing, selecting, scrolling, or screenshots.",
      "With browser, run snapshot -i before referencing page elements and re-run it after navigation or major DOM changes; normal snapshots include only the current viewport.",
      "Use snapshot -i --full only for a visual overview: inspect the image first, then scroll up/down and run snapshot -i in each relevant viewport; do not claim an object is missing before checking likely sections and the relevant page boundary.",
      "A missing or stale @ref does not perform the action and automatically returns both a fresh DOM snapshot and viewport image for joint inspection; run snapshot -i next to unlock ref-based commands, and never retry the old ref.",
      "Send exactly one browser command per tool call; never combine commands with &&, ||, ;, or pipes.",
      "For downloads, inspect with download-info and prefer download <@ref> over clicking and guessing; use downloads to check progress.",
      "Browser close affects only the current Pi session tab; browser shutdown stops the shared daemon for every session.",
    ],
    parameters: Type.Object({
      command: Type.String({ description: "Nodriver browser command, without a prefix" }),
    }),

    async execute(_toolCallId, params, signal, _onUpdate, ctx) {
      const sessionId = ctx.sessionManager.getSessionId();
      const run = async () => worker.request(params.command.trim(), sessionId, signal);
      const responsePromise = queue.then(run, run);
      queue = responsePromise.catch(() => undefined);
      const response = await responsePromise;
      const text = response.text || "(no output)";

      if (response.screenshotPath) {
        const image = readFileSync(response.screenshotPath);
        const extension = extname(response.screenshotPath).toLowerCase();
        const mimeType = extension === ".jpg" || extension === ".jpeg" ? "image/jpeg" : "image/png";
        return {
          content: [
            { type: "text" as const, text },
            { type: "image" as const, data: image.toString("base64"), mimeType },
          ],
          details: response,
        };
      }

      const truncation = truncateHead(text, { maxLines: DEFAULT_MAX_LINES, maxBytes: DEFAULT_MAX_BYTES });
      let output = truncation.content;
      if (truncation.truncated) {
        output += `\n\n[Output truncated: ${truncation.outputLines}/${truncation.totalLines} lines, ${formatSize(truncation.outputBytes)}/${formatSize(truncation.totalBytes)}.]`;
      }
      return {
        content: [{ type: "text" as const, text: output }],
        details: { ...response, truncated: truncation.truncated },
      };
    },
  });

  pi.on("session_shutdown", async () => {
    worker.disconnect();
  });
}
