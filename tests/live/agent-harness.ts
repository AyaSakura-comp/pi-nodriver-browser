// Runs browser-agent.ts tasks directly against a worker socket (no Pi), for live checks.
// usage: node tests/live/agent-harness.ts <socket> <tasks.json> <out.json> [--only id,id]
import { createConnection } from "node:net";
import { readFileSync, writeFileSync } from "node:fs";
import { runBrowserTask, type WorkerLike } from "../../browser-agent.ts";

const MARKER = "__PI_NODRIVER__";

function client(socketPath: string): WorkerLike {
  let next = 1;
  return {
    request(command, sessionId, signal) {
      return new Promise((resolve, reject) => {
        const id = next++;
        const socket = createConnection(socketPath);
        let buffer = "";
        const timer = setTimeout(() => { socket.destroy(); reject(new Error(`timeout: ${command}`)); }, 95_000);
        signal?.addEventListener("abort", () => { socket.destroy(); reject(new Error("aborted")); }, { once: true });
        socket.on("connect", () => socket.write(JSON.stringify({ id, command, sessionId }) + "\n"));
        socket.on("data", (chunk) => {
          buffer += chunk.toString();
          let index;
          while ((index = buffer.indexOf("\n")) >= 0) {
            const line = buffer.slice(0, index);
            buffer = buffer.slice(index + 1);
            if (!line.startsWith(MARKER)) continue;
            const response = JSON.parse(line.slice(MARKER.length));
            if (response.id !== id) continue;
            clearTimeout(timer);
            socket.end();
            if (response.ok) resolve(response);
            else reject(new Error(response.error || "command failed"));
          }
        });
        socket.on("error", (error) => { clearTimeout(timer); reject(error); });
      });
    },
  };
}

const [socketPath, tasksPath, outPath, ...rest] = process.argv.slice(2);
const only = rest[0] === "--only" ? new Set(rest[1].split(",")) : undefined;
const tasks = JSON.parse(readFileSync(tasksPath, "utf8")).filter((t: any) => !only || only.has(t.id));
const worker = client(socketPath);
const llm = {
  endpoint: process.env.AGENT_LLM_URL || "http://127.0.0.1:8090/v1/chat/completions",
  model: process.env.AGENT_LLM_MODEL || "qwen3.8-flash",
  local: true,
};
const results: any[] = [];
for (const task of tasks) {
  const t0 = Date.now();
  const lines: string[] = [];
  let result: any;
  try {
    result = await runBrowserTask({
      goal: task.goal, url: task.url, session: `harness-${task.id}`, worker, llm,
      onProgress: (line) => { lines.push(`${((Date.now() - t0) / 1000).toFixed(1)}s ${line}`); },
    });
  } catch (error) {
    result = { status: "HARNESS_ERROR", reason: String((error as Error).stack || error) };
  }
  try { await worker.request("session-cleanup", `harness-${task.id}`); } catch {}
  const row = { id: task.id, site: task.site, goal: task.goal, ...result, progress: lines };
  results.push(row);
  console.log(`\n### ${task.id} ${task.site}: ${row.status} in ${((Date.now() - t0) / 1000).toFixed(1)}s (llm ${(row.llm_ms / 1000 || 0).toFixed(1)}s, browser ${(row.browser_ms / 1000 || 0).toFixed(1)}s)`);
  for (const line of lines) console.log("   " + line);
  console.log("   answer: " + String(row.answer || row.reason || "").slice(0, 400).replace(/\n/g, " | "));
  writeFileSync(outPath, JSON.stringify(results, null, 2));
}
