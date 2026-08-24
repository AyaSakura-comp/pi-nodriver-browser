# Vision Correctness Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Add a mandatory screenshot → marked preview → visual confirmation → click workflow for last-resort viewport-coordinate interactions, then prove a Qwen 3.6 Pi agent receives and uses the screenshots.

**Architecture:** Add a session-scoped `VisionCorrectnessGuard` that records a fresh current-viewport screenshot, issues one-time marker tokens tied to the active page/URL/viewport, and consumes a matching token before clicking. `vision-mark <x> <y>` temporarily draws a non-interactive crosshair in the page and returns the annotated screenshot without clicking; `vision-click <token>` clicks the stored point only after the model has had a separate turn to inspect that image. Raw `click <x> <y>` is blocked so coordinate interaction cannot bypass visual verification; semantic `click @ref` remains unchanged.

**Tech Stack:** Python 3.13, Nodriver/CDP, in-page DOM overlay, TypeScript Pi extension responses, unittest, real Chrome/Xvfb, Pi CLI with local Qwen 3.6 vision input.

---

### Task 1: Specify parser and state-machine behavior with failing unit tests

**Objective:** Define strict syntax and session/page/TTL/token invariants before implementation.

**Files:**
- Modify: `tests/test_browser_logic.py`
- Modify: `browser_logic.py`

**Steps:**
1. Add tests for `parse_vision_mark`, `parse_vision_click`, `VisionPageState`, and `VisionCorrectnessGuard`.
2. Cover finite/non-negative coordinates, exact token syntax, screenshot prerequisite, page/viewport mismatch, replacement tokens, expiry, one-time consumption, and invalidation.
3. Run only the new tests and verify they fail because the API does not exist.
4. Implement the minimum parser and guard code.
5. Re-run the new tests and verify they pass.

### Task 2: Specify the browser command workflow with failing real-Chrome tests

**Objective:** Prove that previews contain a marker, never click early, allow correction, and only the current token can click.

**Files:**
- Create: `tests/fixture_vision_canvas.html`
- Modify: `tests/test_worker_integration.py`
- Modify: `worker.py`

**Steps:**
1. Add a canvas-only fixture with a visually rendered target and no semantic `@ref`.
2. Add opt-in tests requiring `screenshot` before `vision-mark`, comparing clean and marked screenshot bytes, checking that preview does not click, rejecting the superseded token, confirming the replacement token, rejecting out-of-viewport points, and blocking raw coordinate clicks.
3. Run the focused tests with `RUN_BROWSER_INTEGRATION=1` and verify expected failures.
4. Add session state, viewport capture, temporary marker overlay/cleanup, preview screenshot output, token confirmation, and click execution.
5. Invalidate pending visual state on mutating/navigation actions and consume tokens before native click dispatch.
6. Re-run focused tests and verify pass.

### Task 3: Make the Pi tool teach and transport the visual workflow

**Objective:** Ensure every clean and marked preview is returned as an actual image content block to the model.

**Files:**
- Modify: `index.ts`
- Modify: `README.md`
- Modify: `tests/test_install.py`

**Steps:**
1. Add failing installation assertions for the new commands and mandatory workflow guidance.
2. Document `screenshot`, `vision-mark <x> <y>`, correction by re-marking, and `vision-click <token>`; remove raw coordinate click as an executable fallback.
3. Keep using `screenshotPath` so `index.ts` attaches the PNG as `type: image` rather than text-only output.
4. Run installation tests and verify pass.

### Task 4: Regression, security, and independent review

**Objective:** Verify no regressions or unsafe reusable/stale click capabilities.

**Files:**
- Review all changed files.

**Steps:**
1. Run `python3 -m py_compile worker.py browser_logic.py`.
2. Run the default unittest suite.
3. Run focused real-Chrome tests and `git diff --check`.
4. Scan added lines for secrets, command injection, unsafe `eval`/`exec`, and debug output.
5. Dispatch an independent reviewer with the staged diff; fix blockers and re-run verification.

### Task 5: Deploy and verify with Qwen 3.6 vision

**Objective:** Demonstrate that a fresh Pi/Qwen run uses image-bearing screenshot and marked-preview results before clicking the canvas target.

**Files:**
- Deploy to: `/home/chihmin/.pi/agent/extensions/nodriver-browser`
- Evidence: `/tmp/qwen36-vision-correctness/`

**Steps:**
1. Install the verified source with `SKIP_PIP_INSTALL=1 ./install.sh` and compare deployed source hashes.
2. Start a fresh Pi CLI run using `local-llama/qwen3.6-35b-q4`, browser-only tools, and the visual canvas fixture.
3. Require Qwen to locate the target without supplied coordinates and finish the task without semantic refs.
4. Inspect JSONL evidence: a normal `screenshot` response contains image content, at least one `vision-mark` response contains image content, and `vision-click` occurs only after the marked-image response.
5. Confirm the page reports success and save the final evidence paths.
