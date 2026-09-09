# Android-to-Linux Block Fallback Implementation Plan

> **For Hermes:** Use test-driven development to implement this plan task-by-task.

**Goal:** Keep Android Chrome mobile rendering by default, but retry one blocked interactive navigation in a fresh native Linux Chrome target.

**Architecture:** Add a pure strong-signal block classifier, inspect the first rendered URL/title/body after Android navigation, and replace only the blocked target with a fresh target that receives mobile device metrics but no UA override. Return the selected identity and fallback reason; never retry more than once.

**Tech Stack:** Python 3, Nodriver/CDP, unittest, local HTTP integration fixtures.

---

### Task 1: Add strong-signal block classification

**Files:**
- Modify: `browser_logic.py`
- Test: `tests/test_browser_logic.py`

1. Write failing tests for CAPTCHA URL, localized robot challenge, 429, Cloudflare, and benign pages.
2. Run the focused tests and confirm RED.
3. Implement `detect_access_block(url, title, text) -> str | None` using only strong markers.
4. Run focused tests and confirm GREEN.

### Task 2: Add one-shot fresh-target fallback

**Files:**
- Modify: `worker.py`
- Test: `tests/test_worker_integration.py`

1. Add a failing real-browser integration test whose local server blocks Android UA and accepts native Linux UA.
2. Confirm the first request is Android, the second is Linux, and the final response reports `identityUsed=linux-fallback`.
3. Factor Android and native-Linux page configuration helpers.
4. After Android navigation, classify the rendered page; on block, close it and navigate once in a fresh native target.
5. Confirm no-fallback Android identity, normal clicks, rollback, and tab accounting tests remain green.

### Task 3: Document, install, and live-verify

**Files:**
- Modify: `README.md`, `index.ts`
- Deploy to: `~/.pi/agent/extensions/nodriver-browser/`

1. Document Android-first/Linux-fallback behavior and the one-retry limit.
2. Run `py_compile`, all fast tests, and focused real-browser tests.
3. Run `git diff --check`.
4. Reinstall with `SKIP_PIP_INSTALL=1 ./install.sh`.
5. Verify Inline and OpenTable load behavior, then verify a PChome product page remains in Android mobile layout.
