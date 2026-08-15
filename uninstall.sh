#!/usr/bin/env bash
set -euo pipefail

PI_AGENT_DIR="${PI_AGENT_DIR:-$HOME/.pi/agent}"
TARGET="$PI_AGENT_DIR/extensions/nodriver-browser"

if [[ -d "$TARGET" ]]; then
  rm -rf "$TARGET"
  echo "Removed $TARGET"
else
  echo "Not installed: $TARGET"
fi

echo "Start a new Pi session or run /reload."
