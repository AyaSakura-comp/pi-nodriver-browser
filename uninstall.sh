#!/usr/bin/env bash
set -euo pipefail

PI_AGENT_DIR="${PI_AGENT_DIR:-$HOME/.pi/agent}"
TARGET="$PI_AGENT_DIR/extensions/nodriver-browser"
PI_NODRIVER_SOCKET="${PI_NODRIVER_SOCKET:-$PI_AGENT_DIR/nodriver-browser.sock}"

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
fi

if [[ -d "$TARGET" ]]; then
  rm -rf "$TARGET"
  echo "Removed $TARGET"
else
  echo "Not installed: $TARGET"
fi

echo "Start a new Pi session or run /reload."
