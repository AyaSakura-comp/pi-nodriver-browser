import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { extname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createInterface } from "node:readline";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { DEFAULT_MAX_BYTES, DEFAULT_MAX_LINES, formatSize, truncateHead } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const ROOT = fileURLToPath(new URL(".", import.meta.url));
const PYTHON = join(ROOT, ".venv", "bin", "python");
const WORKER = join(ROOT, "worker.py");
const MARKER = "__PI_NODRIVER__";

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
  screenshot [--full] - Capture screenshot and return it inline
  close - Close Chrome
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
  private process?: ChildProcessWithoutNullStreams;
  private nextId = 1;
  private pending = new Map<number, { resolve: (value: WorkerResponse) => void; reject: (error: Error) => void; timer: NodeJS.Timeout }>();
  private stderr = "";

  private start() {
    if (this.process && this.process.exitCode === null) return;
    if (!existsSync(PYTHON)) {
      throw new Error(`Nodriver browser environment is missing. Run: python3 -m venv ${join(ROOT, ".venv")} && ${PYTHON} -m pip install nodriver`);
    }

    this.stderr = "";
    this.process = spawn("xvfb-run", ["-a", "-s", "-screen 0 1440x1000x24", PYTHON, WORKER], {
      cwd: ROOT,
      stdio: ["pipe", "pipe", "pipe"],
      detached: true,
    });

    this.process.stderr.on("data", (chunk) => {
      this.stderr = (this.stderr + chunk.toString()).slice(-12000);
    });

    const lines = createInterface({ input: this.process.stdout });
    lines.on("line", (line) => {
      if (!line.startsWith(MARKER)) return;
      let response: WorkerResponse;
      try {
        response = JSON.parse(line.slice(MARKER.length));
      } catch (error) {
        return;
      }
      const request = this.pending.get(response.id);
      if (!request) return;
      clearTimeout(request.timer);
      this.pending.delete(response.id);
      if (response.ok) request.resolve(response);
      else request.reject(new Error(response.error || "Nodriver command failed"));
    });

    this.process.on("exit", (code, signal) => {
      const detail = this.stderr.trim();
      const error = new Error(`Nodriver worker exited (${code ?? signal ?? "unknown"})${detail ? `: ${detail}` : ""}`);
      for (const request of this.pending.values()) {
        clearTimeout(request.timer);
        request.reject(error);
      }
      this.pending.clear();
      this.process = undefined;
    });
  }

  request(command: string, signal?: AbortSignal): Promise<WorkerResponse> {
    this.start();
    const process = this.process!;
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
        this.stop();
        reject(new Error("Browser command cancelled"));
      };
      signal?.addEventListener("abort", abort, { once: true });

      process.stdin.write(`${JSON.stringify({ id, command })}\n`, (error) => {
        if (error) {
          clearTimeout(timer);
          this.pending.delete(id);
          reject(error);
        }
      });
    });
  }

  stop() {
    const child = this.process;
    this.process = undefined;
    if (child && child.exitCode === null && child.pid) {
      try {
        process.kill(-child.pid, "SIGTERM");
      } catch {
        child.kill("SIGTERM");
      }
    }
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
    worker.stop();
  });
}
