# Literal Browser Ref Guidance and Qwen Verification Implementation Plan

> **For Hermes:** Execute this plan task-by-task with strict RED-GREEN verification while preserving the repository's existing uncommitted Google-search work.

**Goal:** Prevent small/local models from sending angle-wrapped browser refs such as `click <@e16>`, and verify the Lady Flavor registration flow with Pi using `local-llama/qwen3.6-35b-q4`.

**Architecture:** Clarify every model-visible command example to use literal snapshot refs (`@e1`, `@e16`) without angle brackets. Add a narrow parser compatibility shim that unwraps only the ref argument of ref-based commands, so older model output remains executable without changing text arguments. Deploy the changed extension files without stopping the shared daemon, then run Qwen against an isolated socket/profile so the new worker code is exercised safely.

**Tech Stack:** TypeScript Pi extension prompt metadata, Python command parser/worker, `unittest`, Pi CLI print/JSON mode, Nodriver/Chrome.

---

### Task 1: Add regression tests for literal and legacy ref syntax

**Objective:** Prove the current parser and model-facing guide do not yet satisfy the desired behavior.

**Files:**
- Modify: `tests/test_browser_logic.py`
- Modify: `tests/test_install.py`

**Steps:**
1. Add parser tests requiring `click <@e16>` and `fill <@e6> ...` to normalize to literal `@e16`/`@e6`, while preserving angle-wrapped text passed to `click-text`.
2. Add a source-contract test requiring an explicit `click @e16` example, banning ambiguous `click <@ref>`/`fill <@ref>` examples, and requiring worker usage errors to say “no angle brackets”.
3. Run the targeted tests and confirm they fail for the expected missing behavior.

### Task 2: Implement the minimal parser and guidance fix

**Objective:** Make legacy angle-wrapped refs safe and make the intended literal syntax unambiguous to Qwen.

**Files:**
- Modify: `browser_logic.py`
- Modify: `worker.py`
- Modify: `index.ts`
- Modify: `README.md`

**Steps:**
1. Add command-aware normalization for only the ref position of ref-based commands.
2. Replace model-facing placeholder examples with concrete literal refs such as `click @e16` and `fill @e6 "text"`.
3. Add a prominent instruction: never type `<` or `>` around snapshot refs.
4. Change relevant usage errors so they teach literal syntax rather than repeating ambiguous metavariable notation.
5. Run targeted tests and confirm they pass.

### Task 3: Run regression and deployment checks

**Objective:** Ensure the change does not regress existing browser behavior and deploy only the required runtime files.

**Files:**
- Deploy to: `~/.pi/agent/extensions/nodriver-browser/{index.ts,browser_logic.py,worker.py,README.md}`

**Steps:**
1. Run Python compilation checks.
2. Run the complete fast unittest suite.
3. Run `git diff --check` and inspect the focused diff without discarding pre-existing uncommitted work.
4. Copy the four updated runtime/documentation files to the installed extension without invoking `install.sh` or stopping the shared browser daemon.
5. Verify source/deployed file hashes match.

### Task 4: Verify the registration flow with Pi + Qwen

**Objective:** Confirm the local Qwen model follows the corrected literal-ref workflow on the same registration page.

**Steps:**
1. Confirm `local-llama/qwen3.6-35b-q4` is available to Pi.
2. Launch Pi non-interactively with only the updated Nodriver extension and browser tool, using an isolated Unix socket and isolated Chrome profile.
3. Ask Qwen to open Lady Flavor sign-in, switch to registration, fill `hkhs7821@gmail.com`, select only the required terms checkbox, and proceed until the next form/state is visible; do not place an order or enter payment data.
4. Capture the Pi JSON/session trace and assert that it uses literal refs, avoids usage/stale-ref loops, and reaches a verifiable post-submit state.
5. Report exact commands, outcome, and any remaining site-specific blocker.
