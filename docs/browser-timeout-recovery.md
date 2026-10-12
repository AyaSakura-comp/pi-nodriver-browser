# Browser timeout: cyclic DOM walks and connection cleanup

## Incident and evidence (2026-10-12, Asia/Taipei)

The shared Chrome process and its browser-level CDP endpoint were responsive,
while several Google Cloud Console page targets stopped responding. Initially,
ordinary `browser_intent` screenshots/observations timed out. Closing only the
operator-authorized hung targets was insufficient: the old worker also retained
stale session/connection state.

A fresh, attach-only diagnostic worker reproduced the failure on the read-only
**APIs & Services dashboard → Library → snapshot** path. Instrumenting the
snapshot walker showed repeated visits to document branches (including recurring
four-node iframe documents and the same larger branch). The old traversal
tracked interactive results but did not prevent revisiting roots or traversing
already-seen nodes. A deterministic cyclic-frame fixture reproduces that defect
without Chrome or private account data.

This establishes a **DOM traversal loop**, not a demonstrated race between two
conversations. The precise website/proxy mechanism exposing those repeated
branches was not established. This diagnosis does not exclude unrelated races
elsewhere, including shared Xvfb focus handling.

| Verification | Observed result |
|---|---|
| Before traversal deduplication | Library snapshot did not return within a 35-second diagnostic deadline; production requests also hit the 75-second command deadline. |
| After deduplication | The corresponding diagnostic snapshot evaluation returned in approximately 20 ms. |
| Deployed intent tool | Dashboard open → Library click → read → screenshot completed in 6.870 s, with the actual Library title/URL and screenshot verified. |
| Repeated read-only verification | Observe → read → screenshot completed in 0.657 s. |

These are incident smoke-test measurements, not a repeated performance benchmark
or a guarantee that arbitrary pages cannot hang. The shared Chrome process was
not restarted; its profile/logins and healthy existing pages were preserved.
An additional API search plan encountered HTTP 403 and was not replayed or
bypassed; that path was not verified.

## Changes

### 1. Cycle-safe DOM traversal

`worker.py` now tracks visited document/shadow roots and DOM nodes in snapshots,
select-option inspection, general CSS/text click resolution, and ref actions.
Snapshot traversal additionally stops beyond depth 12, at 256 roots (including
the document), or at 20,000 visited nodes. Other general walkers cap roots at
256, nodes at 20,000, and frame depth
(or combined frame/shadow-host depth for ref actions) at 12.

Snapshots expose `snapshotTruncated` on returned rows when a depth/root/node budget
is reached. `browser_logic.format_snapshot` adds an explicit incomplete-listing
notice when those marked rows exist. No returned refs means there is no row on
which to carry that flag; do not interpret an empty listing as proof of complete
coverage. Narrow the view or use visual inspection for omitted controls.

Literal click refs use a targeted `[data-pi-ref]` lookup before descending into
visible shadow/frame hosts. This avoids measuring every unrelated node on a
large dashboard. Ref lookup retains visibility, disabled-control checks,
same-origin frame coordinate offsets, and full-page ref handling; it also bounds
visited roots/frame nesting. Snapshot geometry/text reads are collected before
ref attribute writes to reduce layout read/write interleaving.

Pre-commit review additionally caught old ref attributes in skipped roots,
mismatched snapshot/action root budgets, and `--port 0` selecting the saved port.
Snapshots now replace a document-local node/ref WeakMap and root set; ref clicks,
ref actions, and option inspection reject nodes/roots outside that snapshot.
This prevents old attributes from aliasing newly numbered controls without an
unbounded cleanup traversal. Explicit port zero is rejected before profile or
network access. These final hardening changes were verified offline; the live
Library timings above describe the earlier deployed traversal repair, not a
fresh browser smoke test of this final staged version.

### 2. Disconnected clients cancel their requests

The server closes the disconnected client's writer, cancels that connection's
active command tasks, then awaits cleanup. Responses are not written to a
closing writer. The regression test confirms that a disconnected long-running
command is cancelled and releases its per-session lock for a subsequent client.

Cancellation is not a universal hard deadline: tasks that suppress cancellation,
research cleanup, or underlying CDP shutdown can still delay draining. A timeout
may also occur after a website action already happened. **Observe state rather
than replaying writes, purchases, uploads, or consent actions.**

### 3. Attach-only worker replacement

`PI_NODRIVER_ATTACH_PORT` connects a worker to an already-running Chrome on
`127.0.0.1` instead of launching another browser. Normal attached-worker shutdown
uses CDP connection cleanup, not `Browser.close` or browser-process termination.
`PI_NODRIVER_DETACH_TIMEOUT` defaults to two seconds for that cleanup await; it is
best-effort and does not bound the entire service shutdown or cancellation-
resistant cleanup. Worker-local session/ref mappings are not transferred to the
replacement worker: reopen an authorized URL when a session reports no page.
This is not a login reset and does not authorize automatic navigation replay.

## Operator recovery through the existing service hub

The versioned operator helpers are under `ops/`. They are maintained copies of
scripts deployed to the existing `restart-service` hub, **not** a new background
service or a standalone replacement skill. They are not deployed by `install.sh`.
On a host that already uses that hub, review the diff before copying them:

```sh
HUB="$HOME/.hermes/skills/restart-service/scripts"
# Preserve any host-specific resolver changes before replacing its script.
cp ops/browser-target-recovery.py ops/browser-worker-reconnect.py "$HUB/"
cp ops/resolve-browser.sh "$HUB/resolve-browser.sh"
```

The no-argument resolver checks the local intent router and starts it only when
missing/unhealthy. It does not restart Chrome, models, or unrelated services.
Target recovery requires Python 3.11+ and the worker environment's `websockets`.
The worker-reconnect helper is Linux/user-systemd specific.

### Exact-target recovery (explicit operator authorization required)

```sh
bash "$HUB/resolve-browser.sh" --recover-targets --port "$CDP_PORT" "$TARGET_ID"
```

Use the **actual current** local CDP port and exact authorized 32-hex target IDs,
not values copied from another incident. If no port is supplied, the helper tries
the existing profile's `DevToolsActivePort` file; that file may be absent.

The helper validates all selected targets and local websocket endpoints before
acting. It is deliberately restricted to Google Cloud Console page targets:
probe → attempt `Runtime.terminateExecution` → probe again → close only the
still-unresponsive selected target with `Target.closeTarget`. A healthy target
is untouched. No navigation is replayed and shared Chrome is not restarted.
Termination/closure can lose unsaved work in that target, hence explicit scope
and operator approval are required. It is not a general-purpose tab killer.

### Worker reconnect / reload (explicit operator authorization required)

```sh
bash "$HUB/resolve-browser.sh" --reconnect-worker --port "$CDP_PORT" --dry-run
bash "$HUB/resolve-browser.sh" --reconnect-worker --port "$CDP_PORT"
# For the already-managed attach-only worker on the same Chrome port:
bash "$HUB/resolve-browser.sh" --reconnect-worker --port "$CDP_PORT" --reload
```

The helper verifies current-user ownership, exact worker argv, an existing
healthy Chrome CDP endpoint, and the saved display/Xauthority. For a legacy
`xvfb-run` worker it kills only the verified wrapper (without its cleanup trap)
and that Python worker, preserving Xvfb/authentication and Chrome; it then starts
an attach-only `nodriver-browser-worker.service` user unit. Reload refuses an
unmanaged/non-attach worker or a different attach port. These are disruptive to
worker-local sessions, and the legacy wrapper kill deliberately leaves Xvfb
running. Inspect failures rather than repeating process-control commands.

A user-unit reload during the incident twice reached systemd's stop timeout
before replacement completed. The Chrome process survived. The helper's socket
probe demonstrates transport availability, **not** successful navigation or
that all old commands have drained. Always run a separate read-only browser
verification after reconnecting.

## Reproducible offline checks

From the repository root, with production-compatible Python dependencies and
Node.js available (the JS fixtures execute trusted repository constants only):

```sh
PYTHON="$HOME/.pi/agent/extensions/nodriver-browser/.venv/bin/python"
for test in \
  test_snapshot_layout_batching.py \
  test_click_ref_lookup.py \
  test_disconnect_cancellation.py \
  test_shared_browser_attach.py \
  test_target_recovery.py; do
  PYTHONPATH=. RUN_BROWSER_INTEGRATION=0 \
    "$PYTHON" -m unittest discover -s tests -p "$test" -v || exit
done
"$PYTHON" -m py_compile worker.py browser_logic.py ops/*.py
git diff --check
```

The original 16 focused methods passed on the repaired deployment; publication
adds three review-driven regression methods, for **19 passing focused methods**.
Coverage includes stale refs in skipped roots, consistent root limits, explicit
invalid-port rejection, cyclic roots in all four general walkers, layout batching, truncated-list
notices, fast ref lookup, shadow/frame coordinates, visibility/disabled checks,
disconnect lock release, attached-browser shutdown, and exact-target recovery.
The target-recovery tests load the repository's `ops/` copy and need no personal
home-directory paths or live Chrome.

During incident diagnosis, the worker-integration module run alone on the
existing deployment ran 172 tests: 12 failures, three errors, and 65 skips.
Comparing named failures with its unmodified baseline showed the same 15
pre-existing semantic-progress/vision-long-press failures/errors; this fix did
not claim to resolve those module-alone failures.

For publication, clean baseline and staged-candidate source snapshots were
compared with matching Git index metadata and the same Python environment
(`.venv` available for the installer fixture), excluding unrelated working-tree
edits. The **full offline suite** passed in both: baseline **712 tests, 90
skipped**; final candidate **731 tests, 90 skipped** (641 executed, including the
19 new focused methods). This validates no observed full-suite regression, not an
explanation for the earlier module-alone result. Real-browser integration
fixtures remained disabled; the live Library smoke tests above are a separate
and narrower verification.
