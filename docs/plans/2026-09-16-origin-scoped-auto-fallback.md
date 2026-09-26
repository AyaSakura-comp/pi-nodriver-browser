# Origin-Scoped Android-to-Linux Gate Fallback Implementation Plan

> **For Hermes:** Implement task-by-task with strict test-first development.

**Goal:** Keep `auto` mode Android-mobile by default, detect strong CAPTCHA/login gates, and route only affected origins through Linux desktop for the remainder of the Pi session.

**Architecture:** Add a pure gate classifier and origin normalizer in `browser_logic.py`, a single-round-trip DOM gate probe in `worker.py`, and a bounded per-session set of Linux-required origins. Explicit `android` and `linux` modes override automatic routing. Post-click unexpected gates reopen the pre-action URL in a fresh Linux target without replaying the click; other origins remain Android.

**Tech Stack:** Python 3, asyncio, Nodriver/CDP, unittest, local HTTP integration fixtures.

---

### Task 1: Pure gate and origin classification

**Files:**
- Modify: `tests/test_browser_logic.py`
- Modify: `browser_logic.py`

1. Add failing tests for strict CAPTCHA widget, strong login-form/dialog signals, incidental login text, auth URL handling, and normalized HTTP(S)/file origins.
2. Run the focused tests and confirm expected failures.
3. Implement minimal pure helpers.
4. Rerun focused tests.

### Task 2: Origin-scoped routing on open

**Files:**
- Modify: `tests/test_worker_integration.py`
- Modify: `worker.py`

1. Add an integration test where Android receives a strong gate and Linux receives content.
2. Assert first fallback is fresh Linux, same-origin subsequent opens remain Linux, and a different origin returns to Android.
3. Verify explicit Android mode disables fallback.
4. Implement the per-session origin routing set and metadata.
5. Rerun focused tests.

### Task 3: Post-click login-gate fallback

**Files:**
- Modify: `tests/test_worker_integration.py`
- Modify: `worker.py`

1. Add a simulated MOMO modal and PChome redirect test.
2. Assert detection after click, fresh Linux reopen of the pre-click URL, no click replay, same-origin Linux routing, and cross-origin Android routing.
3. Add an intentional-login-click test that must not fallback.
4. Implement one-round-trip post-click probing and fallback response.
5. Rerun focused tests.

### Task 4: Documentation and complete verification

**Files:**
- Modify: `README.md`
- Modify: `index.ts`
- Modify: `tests/test_install.py`

1. Document origin-scoped auto behavior and login false-positive safeguards.
2. Update tool guidance without increasing ambiguity.
3. Run focused logic/integration/install tests.
4. Run the full fast suite with ResourceWarnings promoted to errors.
5. Run real-browser tests for Android gate, Linux route, and cross-origin Android reset.

### Task 5: Deploy and smoke-test

1. Run `./install.sh` from the repository.
2. Verify installed `worker.py` and `browser_logic.py` match source.
3. Use a fresh browser daemon to test the UA-only garbage-site fixture.
4. Confirm request identity sequence is Android → Linux for the blocked origin and Android on a different origin.
5. Do not commit while the broader independent security review remains blocked.
