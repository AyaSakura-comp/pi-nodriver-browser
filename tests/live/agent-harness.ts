// Runs browser-agent.ts tasks directly against a worker socket (no Pi), for live checks.
// usage: node tests/live/agent-harness.ts <socket> <tasks.json> <out.json> [--only id,id]
// AGENT_LLM_URL / AGENT_LLM_MODEL pick the planner (default: local qwen3.8-flash on :8090).
import { readFileSync, writeFileSync } from "node:fs";
import { runBrowserTask } from "../../browser-agent.ts";
import { WorkerClient } from "../../worker-launch.ts";

const [socketPath, tasksPath, outPath, ...rest] = process.argv.slice(2);
const only = rest[0] === "--only" ? new Set(rest[1].split(",")) : undefined;
const tasks = JSON.parse(readFileSync(tasksPath, "utf8")).filter((t: any) => !only || only.has(t.id));
// Connect only: a harness must never start a worker (it would take the production profile).
const worker = new WorkerClient(socketPath);
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
worker.disconnect();
