# Progressive research prefill implementation plan

**Goal:** Overlap browser crawling with same-model prompt prefill, without modifying Pi core or deploying an unverified model-server change.

**Architecture:** Pi's research tool remains owner of browser/search authority and final evidence. The daemon emits only committed, immutable, bounded evidence prefixes over its existing owned IPC channel. The extension optionally warms a dedicated llama.cpp slot with `n_predict:0` and `cache_prompt:true`; a final-provider hook verifies that the final request starts with the exact warmed prefix and pins its slot before decoding. On mismatch, failure, cancellation, unsupported model or concurrent use, skip warming and use the ordinary path. Preserve the existing C2 and artifact mode by default until tests and an isolated benchmark demonstrate actual prompt-cache reuse.

**Constraints:** Do not send speculative page text that might later be dropped or reordered; do not send an answer token during crawling. Never use a shared busy slot without reservation. Bound queued bytes, warm requests, GPU load and context budget. Warm-up errors never affect result collection. Do not restart or deploy shared services without explicit authorization.

## Task 1: Immutable incremental passage stream
- Add `research/passages.py` append-only per-page selection, fixed word/token budget and source-labelled output; preserve captured full evidence separately.
- Test first in `tests/test_research_passages.py`: append cannot change earlier emitted prefix; budget stays bounded; duplicate/failed pages emit nothing; final packet begins with exact emitted text.

## Task 2: Owned progress transport
- Wire committed crawl results in `research/controller.py` -> `research/jobs.py` to a bounded, optional progress frame. Transport only extracted text from successfully authorized registered URLs, never arbitrary model URLs. Do not delay crawl owner on a slow consumer.
- Test first in `tests/test_research_jobs.py`, `tests/test_research_daemon.py`: successful in-order emission, cancellation, late results ignored, no progress-frame replay.

## Task 3: No-decode prefill adapter
- Add a small extension-side adapter for local llama.cpp `/v1/chat/completions` `n_predict:0` with `cache_prompt:true`, pinned dedicated slot and an abortable queue.
- Test against an isolated local model server: request boundaries, slot isolation, cancellation and fallback. llama.cpp's chat endpoint may report one synthetic template token for `n_predict:0`; verify no substantive decode and inspect actual `cache_n`.

## Task 4: Exact-prefix verification and provider seam
- Capture the *actual* Pi provider payload (not an approximated chat template) and compare its serialized model input to warmed prefix before pinning the final decode request. Fail closed on mismatches; never silently claim cache hits. Test prefix match/mismatch and concurrent sessions.

## Task 5: Isolated A/B
- Run 3s C2 baseline and prefill variant against the same question set and isolated browser; read server prompt-eval tokens, cache reuse and wall-clock time. Quality audit for any changed passage delivery. Keep old default if fewer facts or no demonstrated reuse.

## Current state (opt-in integration, isolated KV reuse validated)
- The controller emits successful journal-committed pages. An append-only selector sends bounded `progress` frames through the existing owned daemon socket; its final evidence packet begins with the exact progress prefix. The original frozen full-page evidence remains on disk. The `progressive` evidence mode is internal/opt-in; ordinary C2 remains unchanged.
- `research-prefill.ts` forms a speculative Chat Completions payload from the preceding Pi provider request, a *single* research tool call, and the progressively growing tool-result text. It supports the local model's signed `reasoning_content` block; unsupported thinking formats fail closed. Each warm request sends `n_predict:0`, `cache_prompt:true` and an explicitly configured slot. The `before_provider_request` hook pins the final decode to that slot **only if** preceding provider messages match exactly and the final tool-result text starts with the last warmed prefix. Mismatch, network failure, unsupported model, cancellation or multiple tools falls back to ordinary inference.
- Off by default: requires `RESEARCH_PREFILL_RESERVED_SLOT=<dedicated exclusively reserved llama.cpp slot id>` and `local-llama` at the configured `http://localhost:8001/v1`. Merely setting that variable does not reserve a slot; the operator must provision exclusive slot ownership. **Do not enable on the shared two-slot service without a real reservation.** The earlier Python raw `/completion` prototype was removed; only the integrated Chat Completions path remains.
- 687 Python tests (90 skipped) and 43 Node tests pass. Scoped TS checking has only the pre-existing `intent/tool.ts` error. An isolated CPU llama.cpp/Pi test verified `usage.cacheRead` and genuine overlap; see `docs/research-progressive-prefill-validation.md`. Production quality neutrality and latency benefit are not established. No shared-service deployment or restart.

No commit, deployment or service restart is part of this implementation without further approval.
