# Iframe Semantic Actions Implementation Plan

> **Status:** Completed in commit [`099de1b`](https://github.com/AyaSakura-comp/pi-nodriver-browser/commit/099de1bce801c3810c19eeb22ace55835e54b2b8). See the final [technical design, workflow, and architecture](../semantic-actions-technical-design.md) for the implemented protocol and release evidence.

> **For Hermes:** Implement this plan task-by-task with strict test-first verification.

**Goal:** Make Pi prefer semantic iframe interactions over raw viewport coordinates and prove Qwen can configure a CoolPC estimate containing a Ryzen 7 9800X3D.

**Architecture:** Snapshot traversal will retain same-origin iframe context in each `@ref`. A recursive in-page semantic action resolver will perform fill, type, select, submit, and deferred DOM click directly on referenced iframe elements. Native coordinate clicking remains available only as a documented fallback for canvas or inaccessible cross-origin content.

**Tech Stack:** Python 3.13, Nodriver/CDP, browser-injected JavaScript, unittest, Pi Agent/Qwen 3.6.

---

### Task 1: Add iframe fixtures and failing browser tests

**Files:**
- Create: `tests/fixture_iframe.html`
- Modify: `tests/test_worker_integration.py`

1. Add a same-origin iframe containing an input, select, and clickable button.
2. Test that `snapshot -i` labels iframe refs.
3. Test semantic `fill`, `select`, and `click-js` inside the iframe.
4. Run the focused integration tests and confirm failure before implementation.

### Task 2: Add frame-aware snapshots and semantic ref actions

**Files:**
- Modify: `worker.py`
- Modify: `browser_logic.py`
- Test: `tests/test_browser_logic.py`

1. Add frame metadata to snapshot entries and formatted refs.
2. Add recursive same-origin iframe/shadow-root ref resolution.
3. Route fill/type/select/fill-submit/click-js through the semantic resolver.
4. Run focused tests until green.

### Task 3: Make coordinate clicking an explicit fallback

**Files:**
- Modify: `index.ts`
- Modify: `worker.py`
- Modify: `README.md`
- Test: `tests/test_worker_integration.py`

1. Strengthen full-overview output so the agent must run `snapshot -i` before coordinates.
2. Document semantic priority: ref, text/CSS, direct iframe action, then coordinates.
3. Run unit and browser smoke suites.

### Task 4: Deploy and run Pi/Qwen E2E

**Files:**
- Sync deployed extension files under `~/.pi/agent/extensions/nodriver-browser/`.

1. Restart the browser daemon safely.
2. Ask Qwen 3.6 through Pi Agent to use CoolPC semantic refs and select a Ryzen 7 9800X3D without raw coordinate clicks.
3. Verify the resulting estimate state in the live browser and save the JSONL trace.
4. Run review gates, commit, and push.
