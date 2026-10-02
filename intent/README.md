# Integrated intent runtime

Imported from `AyaSakura-comp/laya-browser-intent` branch `laya`, commit
`28a9b73b541b31c2beaaf61a42bd4d59fa7c50e6`, including the local URL-guard fix.
The original repository is retained as historical source; this directory owns
runtime changes now. The Laya **model server** remains a separate dependency.

## Select one tool

`~/.pi/agent/browser-config.json`:

```json
{"browserMode":"intent"}
```

Use `direct` for `browser`, or `intent` for `browser_intent`. Missing config
chooses direct; invalid config fails extension loading. `PI_BROWSER_CONFIG`
can select a different config file. Reload Pi after switching. Search/research,
image and PDF tools remain registered. Only the chosen browser tool's description
is injected. The main extension owns the shared URL guard.

`install.sh` copies both runtime modules, preserves the config and moves the old
standalone extension entry to `~/.pi/agent/extension-backups/`. `--stage DIRECTORY`
only copies files and intentionally does not change configuration or services.
Do not explicitly load the old standalone extension from Pi settings/CLI.

## Backend

The HTTP router remains a separate process on localhost:8011. Provision its Python
environment with torch, transformers, fastapi, uvicorn and pydantic; it uses the
Laya server and multilingual-e5-small weights. Start:

```sh
/path/to/intent-python /path/to/pi-nodriver-browser/intent/intent_service.py --port 8011
```

The integrated extension ensures the Nodriver socket daemon is started before
intent calls. The router's `/health` must be ready separately; direct mode does
not require the router or its Python dependencies.

For the existing user systemd unit, set WorkingDirectory to the installed
`~/.pi/agent/extensions/nodriver-browser/intent`, preserving the existing Python
interpreter and model environment. Restart only laya-intent through the service
restart procedure. No Chrome/Qwen/streaming restart is required for this migration.

## Candidate link picking & search engine navigation

In `browser_intent`, when a page is opened or read, snapshot extraction collects visible `<a>` candidate links and presents them numbered (`[1]..[N]`) under `🔗 [頁面候選跳轉連結]`:
- **Navigation via `pick`**: Models can navigate directly using `action: "open", pick: <number>` without having to copy or reconstruct URLs.
- **Dynamic Provenance Authorization**: Candidate links returned in tool output details are automatically registered into the session history and authorized for subsequent exact URL `open` actions.
- **Search Queries**: Direct search engine queries (e.g. `https://www.google.com/search?q=...`) are pre-authorized by `URL_PROVENANCE_GUARD`, allowing autonomous search workflows.

## Verification

Both modes were exercised with Pi + local Qwen: open exact user-provided
`https://example.com/`, click Learn more, read destination `Example Domains`.
Direct mode used only browser; intent used only browser_intent. Direct mode's
first obsolete `click @e1` was rejected and Qwen recovered with `activate @e1`.
Logs: `/tmp/merged-browser-{direct,intent}-e2e.jsonl` (local, not versioned).
Unit tests cover exact user/search URLs, rejected assistant URLs, slash changes,
failed searches, config validation, staging, search engine query allowlist, and
candidate link `pick` navigation. This is a runtime integration,
not a merge of unrelated Research Controller work or a Git-history rewrite.
