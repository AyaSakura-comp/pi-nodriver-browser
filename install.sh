#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PI_AGENT_DIR="${PI_AGENT_DIR:-$HOME/.pi/agent}"
TARGET="$PI_AGENT_DIR/extensions/nodriver-browser"
SETTINGS="$PI_AGENT_DIR/settings.json"
PI_NODRIVER_SOCKET="${PI_NODRIVER_SOCKET:-$PI_AGENT_DIR/nodriver-browser.sock}"

if [[ "${SKIP_SYSTEM_CHECKS:-0}" != "1" ]]; then
  for command in python3 xvfb-run; do
    if ! command -v "$command" >/dev/null 2>&1; then
      echo "Missing required command: $command" >&2
      exit 1
    fi
  done
  if [[ -z "${PI_NODRIVER_CHROME:-}" ]] && \
     ! command -v google-chrome >/dev/null 2>&1 && \
     ! command -v google-chrome-stable >/dev/null 2>&1 && \
     ! command -v chromium >/dev/null 2>&1 && \
     ! command -v chromium-browser >/dev/null 2>&1; then
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

mkdir -p "$TARGET"
install -m 0644 "$ROOT/index.ts" "$TARGET/index.ts"
install -m 0755 "$ROOT/worker.py" "$TARGET/worker.py"
install -m 0644 "$ROOT/browser_logic.py" "$TARGET/browser_logic.py"
install -m 0644 "$ROOT/requirements.txt" "$TARGET/requirements.txt"
if [[ -d "$ROOT/stealth-extension" ]]; then
  mkdir -p "$TARGET/stealth-extension"
  cp -rf "$ROOT/stealth-extension/"* "$TARGET/stealth-extension/"
fi

if [[ "${SKIP_PIP_INSTALL:-0}" != "1" ]]; then
  if [[ ! -x "$TARGET/.venv/bin/python" ]]; then
    python3 -m venv "$TARGET/.venv"
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
