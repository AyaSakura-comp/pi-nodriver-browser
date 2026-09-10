# Vision Safety Hardening Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Close the remaining fail-closed gaps in cross-session Xvfb interaction, preview lifecycle management, `xdotool` dispatch, and Omni candidate freshness/geometry validation.

**Architecture:** Introduce one process-wide lock around every operation that reads from or writes to the shared Xvfb display. Centralize preview invalidation and checked `xdotool` execution, then bind Omni previews to a lightweight DOM mutation/geometry state without restoring whole-image equality that would break native transient menus.

**Tech Stack:** Python 3.13, asyncio, nodriver/CDP, Xvfb, xdotool, Pillow, unittest.

---

## Current status

The current branch already has backend-aware CDP/Xvfb coordinate mapping, screenshot/detection state checks, finite/in-bounds Omni geometry filtering, strict manual preview hash checks, and broader Omni invalidation. The following tasks remain intentionally deferred and must be completed before the visual path can be considered fully fail-closed.

### Task 1: Serialize every shared Xvfb focus/capture/input operation

**Objective:** Prevent one Pi process or session from switching the foreground tab while another session is capturing or dispatching Xvfb input.

**Files:**
- Modify: `worker.py` (`BrowserWorker.__init__`, `save_viewport_screenshot`, `native_click`, `native_drag`, `native_long_press`, Xvfb helpers, and server request orchestration)
- Test: `tests/test_worker_integration.py`
- Test: `tests/test_viewport_screenshot.py`

**Step 1: Write failing tests**

Add two-session tests that pause session A after `ensure_page_front`, start session B, and prove B cannot focus/capture/click until A releases the shared lock. Add a test proving focus failure aborts root-window capture rather than continuing.

**Step 2: Verify RED**

```bash
python -m unittest \
  tests.test_worker_integration.XvfbSerializationUnitTests \
  tests.test_viewport_screenshot.ViewportScreenshotTests -v
```

Expected: FAIL because the current locks are per session or limited to selected browser-structure actions.

**Step 3: Implement the minimal lock**

Create one `asyncio.Lock` owned by `BrowserWorker`, for example `self.xvfb_interaction_lock`. Hold it across the complete critical section:

```python
async with self.xvfb_interaction_lock:
    await self.ensure_page_front(page)
    # perform root capture or xdotool input before releasing the lock
```

Do not acquire the same non-reentrant lock recursively. Split lock-owning public methods from private `*_locked` helpers where midway screenshots are required during a held long press.

**Step 4: Verify GREEN**

Run the targeted tests, then the full suite.

**Step 5: Commit**

```bash
git add worker.py tests/test_worker_integration.py tests/test_viewport_screenshot.py
git commit -m "fix: serialize shared Xvfb interactions"
```

### Task 2: Centralize preview invalidation for every terminal page transition

**Objective:** Ensure manual and Omni previews cannot survive failed commands, loop-guard rejection, close/quarantine/eviction, document replacement, or a different state-changing interaction.

**Files:**
- Modify: `worker.py` (`execute`, `_execute`, close/quarantine/eviction helpers)
- Test: `tests/test_worker_integration.py`

**Step 1: Write failing table-driven tests**

Cover at least:

```python
cases = [
    'touch-drift @e1 100ms 2 3',
    'close',
    'vision-mark omni',  # repeated until LOOP_GUARD
]
```

Also directly exercise quarantine and tab eviction. Seed both preview stores before each action and assert both are cleared when the action changes or abandons the page.

**Step 2: Verify RED**

```bash
python -m unittest tests.test_worker_integration.PreviewLifecycleUnitTests -v
```

**Step 3: Implement one invalidation API**

Add a helper such as:

```python
def invalidate_visual_previews(self, session_id):
    self.vision_guard.invalidate(session_id)
    self.omni_previews.pop(session_id, None)
```

Call it before repeat/loop rejection can exit for replacement preview commands, and from all terminal page lifecycle helpers. Preserve the active manual token only for the short interval in which `vision-click`, `vision-drag`, or `vision-long-press` is consuming that exact token.

**Step 4: Run targeted and full tests.**

**Step 5: Commit**

```bash
git add worker.py tests/test_worker_integration.py
git commit -m "fix: centralize visual preview invalidation"
```

### Task 3: Treat nonzero xdotool exit codes as dispatch failures

**Objective:** Fall back to CDP only when Xvfb dispatch genuinely fails, rather than treating any completed subprocess as success.

**Files:**
- Modify: `worker.py` (`xvfb_mouse_click`, `xvfb_mouse_drag`, `xvfb_mouse_long_press`)
- Test: `tests/test_worker_integration.py`

**Step 1: Write failing tests**

Mock subprocesses whose `wait()` returns `1`. Assert click returns `False`, drag releases a pressed button and returns `False`, and long press attempts cleanup before returning failure.

**Step 2: Verify RED**

```bash
python -m unittest tests.test_worker_integration.XdotoolExitStatusUnitTests -v
```

**Step 3: Implement a checked helper**

```python
async def run_xdotool(self, *args):
    proc = await asyncio.create_subprocess_exec('xdotool', *args, ...)
    return await proc.wait() == 0
```

Every mouse stage must check the boolean. If failure occurs after `mousedown`, execute a best-effort `mouseup` in `finally` before CDP fallback.

**Step 4: Run targeted and full tests.**

**Step 5: Commit**

```bash
git add worker.py tests/test_worker_integration.py
git commit -m "fix: check xdotool dispatch status"
```

### Task 4: Require Omni centers to lie inside their boxes

**Objective:** Reject internally inconsistent detector geometry before a coordinate can be armed.

**Files:**
- Modify: `worker.py` (`filter_omni_page_candidates`)
- Test: `tests/test_worker_integration.py`

**Step 1: Add a failing candidate test**

Use an in-bounds box with an in-bounds center located outside that box. Assert it is removed while a center on the box boundary follows one explicitly documented inclusive/exclusive policy.

**Step 2: Verify RED.**

**Step 3: Add the minimal validation**

```python
if not (x1 <= center_x <= x2 and y1 <= center_y <= y2):
    continue
```

Keep the existing finite, positive-area, image-bound, backend-aware toolbar, confidence-sort, and result-limit checks.

**Step 4: Run targeted and full tests.**

**Step 5: Commit**

```bash
git add worker.py tests/test_worker_integration.py
git commit -m "fix: validate Omni center-box consistency"
```

### Task 5: Bind Omni previews to DOM-only movement without breaking native menus

**Objective:** Detect element movement/replacement that does not change URL, loader, viewport, or scroll state, while preserving native `<select>` popups and other transient browser UI.

**Files:**
- Modify: `worker.py` (Omni capture and immediate pre-dispatch validation)
- Modify if needed: `browser_logic.py` (`VisionPageState` or a dedicated immutable Omni DOM state)
- Test: `tests/test_worker_integration.py`
- Fixture: `tests/fixture_vision_canvas.html` or a new `tests/fixture_omni_motion.html`

**Step 1: Write failing tests**

Create three cases:

1. A button moves through a DOM/style mutation after detection; click must be rejected.
2. The node under the selected center is replaced with a different actionable node; click must be rejected.
3. A native `<select>` popup opens without a DOM mutation; its Omni option click remains valid.

**Step 2: Verify RED**

```bash
python -m unittest tests.test_worker_integration.OmniDomFreshnessUnitTests -v
```

**Step 3: Implement a lightweight DOM freshness contract**

Install a `MutationObserver` before screenshot capture and record a document-scoped epoch covering child-list, text, and relevant attributes (`style`, `class`, `hidden`, `disabled`, `aria-*`). For DOM-backed candidates, also store a center hit-test fingerprint and bounding rectangle. Immediately before dispatch, reject changed epoch/geometry/fingerprint. For browser-native transient UI that has no DOM candidate, retain the existing page-state/TTL contract rather than reintroducing whole-PNG equality.

**Step 4: Run the native dropdown regression**

Verify the seven-level dropdown fixture still exposes and selects the expanded native option through Xvfb.

**Step 5: Run the full suite.**

**Step 6: Commit**

```bash
git add worker.py browser_logic.py tests/test_worker_integration.py tests/fixture_omni_motion.html
git commit -m "fix: bind Omni previews to DOM geometry state"
```

### Task 6: Final review, deployment, and documentation closure

**Objective:** Prove the remaining hardening work is complete and remove these entries from the known-limitations list.

**Files:**
- Modify: `README.md`
- Modify: `docs/semantic-actions-technical-design.md`
- Modify: this plan, marking completed tasks

**Step 1: Run verification**

```bash
python -m unittest discover -s tests -q
git diff --check
```

**Step 2: Run an independent fail-closed review**

Review the full diff for cross-session races, stale previews, backend-coordinate errors, subprocess cleanup, and native dropdown regressions.

**Step 3: Deploy and smoke test**

```bash
SKIP_SYSTEM_CHECKS=1 SKIP_PIP_INSTALL=1 ./install.sh
```

Start a new Pi session or use `/reload`, then smoke-test semantic click, Omni click, native dropdown option selection, manual click, drag, and long press.

**Step 4: Update docs and commit**

```bash
git add README.md docs/semantic-actions-technical-design.md docs/plans/2026-09-11-vision-safety-hardening.md
git commit -m "docs: close visual safety hardening plan"
```
