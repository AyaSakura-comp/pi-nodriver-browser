import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export function parseBrowserConfig(value: unknown): { browserMode: "direct" | "intent" } {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Browser config must be an object");
  const mode = (value as Record<string, unknown>).browserMode ?? "direct";
  if (mode !== "direct" && mode !== "intent") throw new Error('browserMode must be "direct" or "intent"');
  return { browserMode: mode };
}

export function readBrowserConfig() {
  const path = process.env.PI_BROWSER_CONFIG || join(process.env.PI_AGENT_DIR || join(homedir(), ".pi", "agent"), "browser-config.json");
  if (!existsSync(path)) return parseBrowserConfig({});
  return parseBrowserConfig(JSON.parse(readFileSync(path, "utf8")));
}
