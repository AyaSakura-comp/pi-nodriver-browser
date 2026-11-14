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

IS_MACOS=0
[[ "$(uname -s)" == "Darwin" ]] && IS_MACOS=1
if [[ "$IS_MACOS" == "1" ]]; then
  # macOS: no Xvfb (the worker hides its own headful Chrome, pibrowser/macos.py), Chrome is an
  # app bundle, and /usr/bin/python3 is 3.9 while the worker needs 3.13 (as CI).
  python_ok() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 13))' >/dev/null 2>&1; }
  PYTHON_BIN="${PI_NODRIVER_PYTHON:-}"
  for candidate in python3.14 python3.13 python3; do
    [[ -n "$PYTHON_BIN" ]] && break
    command -v "$candidate" >/dev/null && python_ok "$(command -v "$candidate")" && PYTHON_BIN="$(command -v "$candidate")"
  done
  if [[ -z "$PYTHON_BIN" ]] && command -v uv >/dev/null; then
    uv python install 3.13 >/dev/null && PYTHON_BIN="$(uv python find 3.13)"
  fi
  [[ -n "$PYTHON_BIN" ]] && python_ok "$PYTHON_BIN" || {
    echo 'Python 3.13+ is required: brew install uv (or python@3.13), or set PI_NODRIVER_PYTHON.' >&2; exit 1; }
  command -v pdftotext >/dev/null || echo 'Warning: pdftotext not found; PDF text needs Poppler (brew install poppler).' >&2
  if [[ ! -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]] && \
     [[ ! -x "$HOME/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]] && \
     [[ ! -x "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing" ]] && \
     [[ ! -x "/Applications/Chromium.app/Contents/MacOS/Chromium" ]] && \
     ! command -v chromium >/dev/null && ! command -v google-chrome >/dev/null; then
    echo 'Chrome/Chromium was not found; install Google Chrome into /Applications.' >&2
    exit 1
  fi
else
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
if [[ "$IS_MACOS" == "1" ]]; then
  # macOS: a venv from an older install may be on a Python the worker no longer runs on.
  python_ok "$TARGET/.venv/bin/python" || { rm -rf "$TARGET/.venv"; "$PYTHON_BIN" -m venv "$TARGET/.venv"; }
else
  [[ -x "$TARGET/.venv/bin/python" ]] || python3 -m venv "$TARGET/.venv"
fi
"$TARGET/.venv/bin/python" -m pip install -r "$TARGET/requirements.txt"

echo "Installed to $TARGET. Run /reload in Pi, or start a new Pi session."
