#!/usr/bin/env bash
# Browser repair hub: start only the missing intent router, never shared Chrome/models.
set -euo pipefail
# Explicit operator repair only: default resolve never interrupts page execution.
if [[ "${1:-}" == '--recover-targets' ]]; then
  shift
  exec "$HOME/.pi/agent/extensions/nodriver-browser/.venv/bin/python" \
    "$(dirname "$0")/browser-target-recovery.py" "$@"
fi
if [[ "${1:-}" == '--reconnect-worker' ]]; then
  shift
  exec python3 "$(dirname "$0")/browser-worker-reconnect.py" "$@"
fi
if (( $# )); then
  echo 'Unknown browser recovery option' >&2
  exit 2
fi
healthy() {
  curl --silent --show-error --fail --max-time 3 http://127.0.0.1:8011/health 2>/dev/null |
    python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("ok") is True else 1)' 2>/dev/null
}
if healthy; then
  echo 'intent router healthy; no restart needed'
  exit 0
fi
# start is idempotent and does not interrupt an already-running router.
systemctl --user start laya-intent.service
for ((i=0; i<${BROWSER_RESOLVE_ATTEMPTS:-45}; i++)); do
  if healthy; then
    echo 'intent router recovered and health verified'
    exit 0
  fi
  sleep 1
done
echo 'intent router unavailable; inspect journalctl --user -u laya-intent.service. No shared service was restarted.' >&2
exit 1
