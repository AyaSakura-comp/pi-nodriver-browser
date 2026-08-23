#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PI_AGENT_DIR="${PI_AGENT_DIR:-$HOME/.pi/agent}"
TARGET="$PI_AGENT_DIR/extensions/nodriver-browser"
SETTINGS="$PI_AGENT_DIR/settings.json"
PI_NODRIVER_SOCKET="${PI_NODRIVER_SOCKET:-$PI_AGENT_DIR/nodriver-browser.sock}"
PI_NODRIVER_PROFILE="${PI_NODRIVER_PROFILE:-$PI_AGENT_DIR/nodriver-profile}"
BUSTER_ARCHIVE="$ROOT/third_party/buster/buster-3.4.0-chrome.zip"
BUSTER_SHA256="26749705f1bb57ef3e4cda9aa73aa66cc71a8d9df2906c9600eaed98f0d54129"

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
install -m 0755 "$ROOT/install_chrome_extensions.py" "$TARGET/install_chrome_extensions.py"
install -m 0644 "$ROOT/browser_logic.py" "$TARGET/browser_logic.py"
install -m 0644 "$ROOT/requirements.txt" "$TARGET/requirements.txt"
if [[ -d "$ROOT/stealth-extension" ]]; then
  mkdir -p "$TARGET/stealth-extension"
  cp -rf "$ROOT/stealth-extension/"* "$TARGET/stealth-extension/"
fi
BUSTER_TARGET="$TARGET/chrome-extensions/buster"
BUSTER_STAGE=""
BUSTER_BACKUP=""
BUSTER_PENDING=0
cleanup_buster_deployment() {
  if [[ -n "$BUSTER_STAGE" && -d "$BUSTER_STAGE" ]]; then
    rm -rf "$BUSTER_STAGE"
  fi
  if [[ "$BUSTER_PENDING" == "1" ]]; then
    rm -rf "$BUSTER_TARGET"
    if [[ -n "$BUSTER_BACKUP" && -d "$BUSTER_BACKUP" ]]; then
      mv "$BUSTER_BACKUP" "$BUSTER_TARGET"
    fi
  elif [[ -n "$BUSTER_BACKUP" && -d "$BUSTER_BACKUP" ]]; then
    rm -rf "$BUSTER_BACKUP"
  fi
}
trap cleanup_buster_deployment EXIT

if [[ "${INSTALL_BUSTER:-0}" == "1" ]]; then
  if [[ ! -f "$BUSTER_ARCHIVE" ]]; then
    echo "Pinned Buster archive was not found: $BUSTER_ARCHIVE" >&2
    exit 1
  fi
  mkdir -p "$TARGET/chrome-extensions"
  BUSTER_STAGE="$(mktemp -d "$TARGET/chrome-extensions/.buster-stage.XXXXXX")"
  python3 - "$BUSTER_ARCHIVE" "$BUSTER_STAGE" "$BUSTER_SHA256" <<'PY'
import hashlib
import json
import stat
import sys
import zipfile
from pathlib import Path

archive_path = Path(sys.argv[1])
destination = Path(sys.argv[2]).resolve()
expected_sha256 = sys.argv[3]
actual_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
if actual_sha256 != expected_sha256:
    raise SystemExit(
        f'Buster archive checksum mismatch: expected {expected_sha256}, got {actual_sha256}'
    )
with zipfile.ZipFile(archive_path) as archive:
    for member in archive.infolist():
        target = (destination / member.filename).resolve()
        if target != destination and destination not in target.parents:
            raise SystemExit(f'Unsafe path in Buster archive: {member.filename}')
        if stat.S_ISLNK(member.external_attr >> 16):
            raise SystemExit(f'Symlink in Buster archive is not allowed: {member.filename}')
    archive.extractall(destination)
manifest = json.loads((destination / 'manifest.json').read_text())
if manifest.get('version') != '3.4.0' or manifest.get('homepage_url') != 'https://github.com/dessant/buster':
    raise SystemExit('Buster archive manifest does not match pinned v3.4.0 upstream')
PY
  BUSTER_BACKUP="$TARGET/chrome-extensions/.buster-backup.$$"
  rm -rf "$BUSTER_BACKUP"
  if [[ -d "$BUSTER_TARGET" ]]; then
    mv "$BUSTER_TARGET" "$BUSTER_BACKUP"
  fi
  BUSTER_PENDING=1
  mv "$BUSTER_STAGE" "$BUSTER_TARGET"
  BUSTER_STAGE=""
  cat >&2 <<'EOF'
Buster opt-in enabled. It has broad extension permissions including <all_urls>,
webRequest, nativeMessaging, and remote speech-recognition access. Pi will not
activate its reCAPTCHA audio solver automatically.
EOF
fi

if [[ "${SKIP_CHROME_EXTENSION_INSTALL:-0}" != "1" ]]; then
  CHROME_EXECUTABLE="${PI_NODRIVER_CHROME:-}"
  if [[ -z "$CHROME_EXECUTABLE" ]]; then
    for command in google-chrome google-chrome-stable chromium chromium-browser; do
      if command -v "$command" >/dev/null 2>&1; then
        CHROME_EXECUTABLE="$(command -v "$command")"
        break
      fi
    done
  fi
  if [[ -z "$CHROME_EXECUTABLE" ]]; then
    echo "Chrome/Chromium was not found. Set PI_NODRIVER_CHROME." >&2
    exit 1
  fi
  CHROME_EXTENSIONS=()
  if [[ -f "$TARGET/stealth-extension/manifest.json" ]]; then
    CHROME_EXTENSIONS+=("$TARGET/stealth-extension")
  fi
  for extension in "$TARGET/chrome-extensions/"*; do
    if [[ -f "$extension/manifest.json" ]]; then
      CHROME_EXTENSIONS+=("$extension")
    fi
  done
  if (( ${#CHROME_EXTENSIONS[@]} > 0 )); then
    python3 "$TARGET/install_chrome_extensions.py" \
      --chrome "$CHROME_EXECUTABLE" \
      --profile "$PI_NODRIVER_PROFILE" \
      "${CHROME_EXTENSIONS[@]}"
  fi
fi

if [[ "$BUSTER_PENDING" == "1" ]]; then
  rm -rf "$BUSTER_BACKUP"
  BUSTER_BACKUP=""
  BUSTER_PENDING=0
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
