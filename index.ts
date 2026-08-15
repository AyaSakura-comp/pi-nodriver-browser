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
Workflow: open URL → snapshot -i (get @refs like @e1) → interact → re-snapshot after page changes.
Commands:
  open <url> - Navigate to URL
  snapshot -i - List visible interactive elements with @refs
  click <@ref> - Click element
  fill <@ref> <text> - Clear and type
  type <@ref> <text> - Type without clearing
  select <@ref> <value> - Select dropdown option
  press <key> - Press Enter, Tab, Space, Backspace, or text
  scroll <up|down|left|right> [px] - Scroll page
  get text|url|title [@ref] - Get information
  wait <@ref|ms> - Wait for an element or milliseconds
  dismiss overlays [--cookies=accept|reject-optional|ignore] - Safely dismiss cookie and modal overlays
  screenshot [--full] - Capture screenshot and return it inline
  close - Close Chrome while leaving the daemon available
  shutdown - Close Chrome and stop the persistent browser daemon
Use quoted text when an argument contains spaces. Re-run snapshot -i after navigation or major DOM changes.`;

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
  private pending = new Map<number, { resolve: (value: WorkerResponse) => void; reject: (error: Error) => void; timer: NodeJS.Timeout }>();

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
        request.reject(error);
      }
      this.pending.clear();
    });
  }

  private async connectOrStart(): Promise<Socket> {
    if (!existsSync(PYTHON)) {
      throw new Error(`Nodriver browser environment is missing. Run: python3 -m venv ${join(ROOT, ".venv")} && ${PYTHON} -m pip install nodriver`);
    }
    try {
      const socket = await this.openSocket();
      this.attach(socket);
      return socket;
    } catch {
      const daemon = spawn(
        "xvfb-run",
        ["-a", "-s", "-screen 0 1440x1000x24", PYTHON, WORKER, "--server", SOCKET],
        { cwd: ROOT, stdio: "ignore", detached: true },
      );
      daemon.unref();
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

  async request(command: string, signal?: AbortSignal): Promise<WorkerResponse> {
    const socket = await this.connection();
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`Browser command timed out: ${command}`));
      }, 90_000);
      this.pending.set(id, { resolve, reject, timer });

      const abort = () => {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(new Error("Browser command cancelled"));
      };
      signal?.addEventListener("abort", abort, { once: true });

      socket.write(`${JSON.stringify({ id, command })}\n`, (error) => {
        if (error) {
          clearTimeout(timer);
          this.pending.delete(id);
          reject(error);
        }
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
      "With browser, run snapshot -i before referencing page elements and re-run it after navigation or major DOM changes.",
    ],
    parameters: Type.Object({
      command: Type.String({ description: "Nodriver browser command, without a prefix" }),
    }),

    async execute(_toolCallId, params, signal) {
      const run = async () => worker.request(params.command.trim(), signal);
      const responsePromise = queue.then(run, run);
      queue = responsePromise.catch(() => undefined);
      const response = await responsePromise;
      const text = response.text || "(no output)";

      if (response.action === "screenshot" && response.screenshotPath) {
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
