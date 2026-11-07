#!/usr/bin/env bash
# Install into Pi: ./install.sh copies the runtime to ~/.pi/agent/extensions/nodriver-browser,
# makes its venv and stops the running worker (the next tool call starts the new code).
# ./install.sh --stage DIR only copies the runtime.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PI_AGENT_DIR="${PI_AGENT_DIR:-$HOME/.pi/agent}"
TARGET="$PI_AGENT_DIR/extensions/nodriver-browser"
PI_NODRIVER_SOCKET="${PI_NODRIVER_SOCKET:-$PI_AGENT_DIR/nodriver-browser.sock}"

copy_runtime() {
  local target="$1"
  mkdir -p "$target/research" "$target/pibrowser/js" "$target/ops"
  install -m 0644 "$ROOT"/*.ts "$ROOT/requirements.txt" "$target/"
  install -m 0755 "$ROOT/worker.py" "$target/worker.py"
  install -m 0644 "$ROOT/pibrowser/"*.py "$target/pibrowser/"
  install -m 0644 "$ROOT/pibrowser/js/"*.js "$target/pibrowser/js/"
  install -m 0644 "$ROOT/research/"*.py "$target/research/"
  install -m 0755 "$ROOT/ops/browser-agent-watchdog.py" "$target/ops/"
}

if [[ "${1:-}" == "--stage" ]]; then
  [[ $# == 2 && -n "$2" ]] || { echo 'usage: install.sh --stage DIRECTORY' >&2; exit 2; }
  copy_runtime "$2"
  exit 0
fi
[[ $# == 0 ]] || { echo 'usage: install.sh [--stage DIRECTORY]' >&2; exit 2; }

for command in python3 xvfb-run; do
  command -v "$command" >/dev/null || { echo "Missing required command: $command" >&2; exit 1; }
done
command -v pdftotext >/dev/null || echo 'Warning: pdftotext not found; PDF text needs Poppler (poppler-utils).' >&2
if [[ ! -x /opt/google/chrome/chrome ]] && ! command -v google-chrome >/dev/null && \
   ! command -v google-chrome-stable >/dev/null && ! command -v chromium >/dev/null && \
   ! command -v chromium-browser >/dev/null; then
  echo 'Chrome/Chromium was not found; install google-chrome or chromium.' >&2
  exit 1
fi

# The running worker keeps the old code until it exits: ask it to shut down, then kill
# one that does not answer.
if [[ -S "$PI_NODRIVER_SOCKET" ]]; then
  python3 -c 'import json, socket, sys
client = socket.socket(socket.AF_UNIX); client.settimeout(3); client.connect(sys.argv[1])
client.sendall(json.dumps({"id": 0, "command": "shutdown"}).encode() + b"\n"); client.recv(4096)' \
    "$PI_NODRIVER_SOCKET" || true
  for _ in {1..30}; do
    [[ -S "$PI_NODRIVER_SOCKET" ]] || break
    sleep 0.1
  done
fi
stale_workers=$(pgrep -u "$USER" -f "^$TARGET/.venv/bin/python .*worker.py" || true)
if [[ -n "$stale_workers" ]]; then
  kill $stale_workers 2>/dev/null || true
  sleep 0.5
  kill -9 $stale_workers 2>/dev/null || true
fi
rm -f "$PI_NODRIVER_SOCKET" "$PI_AGENT_DIR/nodriver-browser.env" "$PI_NODRIVER_SOCKET.lock"

copy_runtime "$TARGET"
[[ -x "$TARGET/.venv/bin/python" ]] || python3 -m venv "$TARGET/.venv"
"$TARGET/.venv/bin/python" -m pip install -r "$TARGET/requirements.txt"

echo "Installed to $TARGET. Run /reload in Pi, or start a new Pi session."
