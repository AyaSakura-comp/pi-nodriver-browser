#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PI_AGENT_DIR="${PI_AGENT_DIR:-$HOME/.pi/agent}"
TARGET="$PI_AGENT_DIR/extensions/nodriver-browser"
SETTINGS="$PI_AGENT_DIR/settings.json"
PI_NODRIVER_SOCKET="${PI_NODRIVER_SOCKET:-$PI_AGENT_DIR/nodriver-browser.sock}"

copy_runtime() {
  local target="$1"
  mkdir -p "$target/research" "$target/intent"
  install -m 0644 "$ROOT/index.ts" "$ROOT/browser-config.ts" "$ROOT/worker-launch.ts" "$target/"
  install -m 0644 "$ROOT/intent/"*.py "$ROOT/intent/"*.ts "$target/intent/"
  install -m 0755 "$ROOT/worker.py" "$target/worker.py"
  install -m 0644 "$ROOT/browser_logic.py" "$ROOT/macos_window.py" "$ROOT/requirements.txt" "$target/"
  install -m 0644 "$ROOT/research/"*.py "$target/research/"
  install -m 0644 "$ROOT/research-model.ts" "$target/"
  if [[ -d "$ROOT/stealth-extension" ]]; then
    mkdir -p "$target/stealth-extension"
    cp -rf "$ROOT/stealth-extension/"* "$target/stealth-extension/"
  fi
}

# Staging copies only runtime files; it never probes/stops services, installs
# dependencies, changes settings, or cleans sockets/display locks.
if [[ "${1:-}" == "--stage" ]]; then
  [[ $# == 2 && -n "$2" ]] || { echo 'usage: install.sh --stage DIRECTORY' >&2; exit 2; }
  copy_runtime "$2"
  exit 0
fi
[[ $# == 0 ]] || { echo 'unknown installer arguments' >&2; exit 2; }

OS="$(uname -s)"

chrome_found() {
  local command app
  for command in google-chrome google-chrome-stable chromium chromium-browser; do
    command -v "$command" >/dev/null 2>&1 && return 0
  done
  if [[ "$OS" == "Darwin" ]]; then
    for app in "Google Chrome.app/Contents/MacOS/Google Chrome" \
               "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing" \
               "Chromium.app/Contents/MacOS/Chromium"; do
      [[ -x "/Applications/$app" || -x "$HOME/Applications/$app" ]] && return 0
    done
  fi
  return 1
}

# Python 3.10+ for the worker venv (macOS ships /usr/bin/python3 3.9). PI_NODRIVER_PYTHON wins;
# otherwise the newest python3.x on PATH; otherwise uv provides one.
python_ok() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; }
PYTHON_BIN="${PI_NODRIVER_PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && python_ok "$(command -v "$candidate")"; then
      PYTHON_BIN="$(command -v "$candidate")"
      break
    fi
  done
fi
if [[ -z "$PYTHON_BIN" ]] && command -v uv >/dev/null 2>&1; then
  uv python install 3.12 >/dev/null
  PYTHON_BIN="$(uv python find 3.12)"
fi

if [[ "${SKIP_SYSTEM_CHECKS:-0}" != "1" ]]; then
  if [[ -z "$PYTHON_BIN" ]] || ! python_ok "$PYTHON_BIN"; then
    echo "Python 3.10+ is required (set PI_NODRIVER_PYTHON, or install uv)." >&2
    exit 1
  fi
  # macOS has no Xvfb: the worker hides its own headful Chrome instead (macos_window.py).
  if [[ "$OS" != "Darwin" ]] && ! command -v xvfb-run >/dev/null 2>&1; then
    echo "Missing required command: xvfb-run" >&2
    exit 1
  fi
  for command in pdftotext pdfimages; do
    if ! command -v "$command" >/dev/null 2>&1; then
      echo "Warning: $command was not found; PDF extraction requires Poppler (poppler-utils; macOS: brew install poppler)." >&2
    fi
  done
  if ! "$PYTHON_BIN" - <<'PY'
import sqlite3
connection = sqlite3.connect(':memory:')
connection.execute('CREATE VIRTUAL TABLE fts_check USING fts5(text)')
PY
  then
    echo "Warning: Python's SQLite lacks FTS5; temporary large-PDF wiki search will be unavailable." >&2
  fi
  if [[ -z "${PI_NODRIVER_CHROME:-}" ]] && ! chrome_found; then
    echo "Chrome/Chromium was not found. Install it or set PI_NODRIVER_CHROME." >&2
    exit 1
  fi
fi

if [[ -S "$PI_NODRIVER_SOCKET" ]]; then
  python3 - "$PI_NODRIVER_SOCKET" <<'PY' || true
import json
import socket
import sys

client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
client.settimeout(3)
client.connect(sys.argv[1])
client.sendall((json.dumps({'id': 0, 'command': 'shutdown'}) + '\n').encode())
client.recv(4096)
client.close()
PY
  for _ in {1..30}; do
    [[ ! -S "$PI_NODRIVER_SOCKET" ]] && break
    sleep 0.1
  done
fi

# Ensure any leftover nodriver workers and their chrome children are completely reset
stale_workers=$(pgrep -u "$USER" -f "^$TARGET/.venv/bin/python .*worker.py" || true)
if [[ -n "$stale_workers" ]]; then
  kill $stale_workers 2>/dev/null || true
  sleep 0.5
  kill -9 $stale_workers 2>/dev/null || true
fi

# Clean up stale nodriver sockets, env, and locks (display >= 100)
rm -f "$PI_NODRIVER_SOCKET" "$PI_AGENT_DIR/nodriver-browser.env" "$PI_AGENT_DIR/nodriver-browser.sock.lock"
find /tmp -maxdepth 1 -name ".X10*-lock" -mmin +5 -delete 2>/dev/null || true

copy_runtime "$TARGET"

# The integrated extension owns tool registration. Keep the old entry as a backup,
# outside Pi's extensions directory so it cannot register a duplicate tool.
if [[ -e "$PI_AGENT_DIR/extensions/browser-intent.ts" || -L "$PI_AGENT_DIR/extensions/browser-intent.ts" ]]; then
  mkdir -p "$PI_AGENT_DIR/extension-backups"
  backup="$PI_AGENT_DIR/extension-backups/browser-intent.ts"
  n=0
  while [[ -e "$backup" || -L "$backup" ]]; do n=$((n + 1)); backup="$PI_AGENT_DIR/extension-backups/browser-intent.ts.~$n~"; done
  mv "$PI_AGENT_DIR/extensions/browser-intent.ts" "$backup"
fi
if [[ ! -f "$PI_AGENT_DIR/browser-config.json" ]]; then
  printf '{"browserMode":"direct"}\n' > "$PI_AGENT_DIR/browser-config.json"
fi

if [[ "${SKIP_PIP_INSTALL:-0}" != "1" ]]; then
  if [[ ! -x "$TARGET/.venv/bin/python" ]]; then
    "${PYTHON_BIN:-python3}" -m venv "$TARGET/.venv"
  fi
  "$TARGET/.venv/bin/python" -m pip install --upgrade pip
  "$TARGET/.venv/bin/python" -m pip install -r "$TARGET/requirements.txt"
fi

if [[ -f "$SETTINGS" ]]; then
  BACKUP="$PI_AGENT_DIR/settings.json.pi-nodriver-browser.bak"
  cp "$SETTINGS" "$BACKUP"
  python3 - "$SETTINGS" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text())
packages = data.get('packages')
if isinstance(packages, list):
    data['packages'] = [item for item in packages if item != 'npm:pi-agent-browser']
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
PY
fi

cat <<EOF
Installed Pi Nodriver Browser to:
  $TARGET

The conflicting npm:pi-agent-browser package was disabled when present.
Run /reload in Pi, or start a new Pi session.
EOF

# Automatically reset/sync Xvfb streaming service if present
if [[ "${SKIP_STREAM_SYNC:-0}" != "1" ]] && [[ -x "$HOME/.hermes/skills/restart-service/scripts/restart-xvfb-streaming.sh" ]]; then
  echo "Checking and syncing Xvfb streaming service..."
  bash "$HOME/.hermes/skills/restart-service/scripts/restart-xvfb-streaming.sh" status >/dev/null 2>&1 || true
fi
