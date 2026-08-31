# Form Safety and Navigation Hardening Implementation Plan

> **For Hermes:** Implement task-by-task with strict RED → GREEN → REFACTOR tests.

**Goal:** Prevent wrong-field entry and unsafe text clicks, expose checkbox state, make stale-ref recovery immediately usable, and stop treating legitimate cross-site QA navigation as an open loop.

**Architecture:** Keep DOM behavior in the worker’s injected JavaScript, but make every text-entry path use the same validated `REF_ACTION_JS`. Enrich snapshots with form-control state derived from either the element itself or a visible label’s associated control. Move open-loop identity into `OpenActionGuard`, keyed by origin instead of counting unrelated destinations.

**Tech Stack:** Python 3.12+, unittest, Nodriver/CDP, injected browser JavaScript, HTML integration fixtures.

---

### Task 1: Add a deterministic form-safety fixture

**Files:**
- Create: `tests/fixture_form_safety.html`
- Modify: `tests/test_worker_integration.py`

**Steps:**
1. Create a fixture containing a focused text input, an interactive label that must not accept text, hidden native checkboxes with visible labels, and a `Next` button.
2. Add integration tests asserting `fill` on a label fails without changing the focused input.
3. Add integration tests asserting snapshot output exposes checkbox type, checked, required, and disabled states.
4. Add an integration test asserting `click-text "X"` does not match `Next`.
5. Run the tests and verify RED failures caused by the current behavior.

### Task 2: Fail closed for text entry and expose form state

**Files:**
- Modify: `worker.py` (`SNAPSHOT_JS`, `REF_ACTION_JS` callers, fill/fill-submit handlers)
- Modify: `browser_logic.py` (`format_snapshot`)

**Steps:**
1. Remove the top-document native fast path for `fill`, `type`, and `fill-submit`; route all through `perform_ref_action`.
2. Keep `setText` limited to text-editable input types, textarea, and contenteditable.
3. Treat visible labels as snapshot controls only when they proxy checkbox/radio/switch controls or are independently interactive.
4. Emit associated control type plus checked/required/disabled state.
5. Render those states in `format_snapshot`.
6. Run targeted integration and unit tests until GREEN.

### Task 3: Make text-click matching safe

**Files:**
- Modify: `worker.py` (`CLICK_TARGET_JS`)
- Test: `tests/test_worker_integration.py`

**Steps:**
1. Require exact matching for one- and two-character queries.
2. For longer non-exact queries, allow only normalized prefix matches rather than arbitrary substring matches.
3. Preserve exact descendant preference.
4. Verify `X` no longer matches `Next`, while exact visible text still clicks.

### Task 4: Make stale-ref recovery authoritative

**Files:**
- Modify: `worker.py` (`stale_ref_recovery`)
- Modify: `tests/test_daemon_integration.py`

**Steps:**
1. Change the stale recovery test to use a fresh ref returned in the recovery response without another snapshot.
2. Verify RED under the existing guard.
3. Clear `snapshot_required_sessions` after the recovery snapshot is generated.
4. Update recovery text to say the returned refs are authoritative and the stale ref must not be retried.
5. Run the daemon integration test until GREEN.

### Task 5: Distinguish cross-site traversal from same-site open loops

**Files:**
- Modify: `browser_logic.py` (`OpenActionGuard`)
- Modify: `worker.py` (`track_open_action`)
- Modify: `tests/test_browser_logic.py`
- Modify: `index.ts`, `README.md`

**Steps:**
1. Add RED unit tests allowing consecutive opens to different origins while still blocking the third same-origin open.
2. Pass the target URL into the guard and key counts by normalized origin.
3. Update tool guidance and README.
4. Run targeted tests until GREEN.

### Task 6: Verify, deploy, and re-run Qwen safety cases

**Files:**
- Deploy changed runtime files to `~/.pi/agent/extensions/nodriver-browser/`

**Steps:**
1. Run Python compilation, complete unit suite, and `git diff --check`.
2. Run relevant browser integration tests under an isolated profile/socket.
3. Compare source and deployed hashes.
4. Start a fresh Pi + Qwen isolated session with a short prompt against deterministic fixtures.
5. Verify no label fill, no `X → Next`, checkbox states visible, fresh stale refs reusable, and distinct-site opens accepted.
6. Do not interrupt the shared browser daemon; note that existing sessions need `/reload` and the worker update activates on next daemon start.
