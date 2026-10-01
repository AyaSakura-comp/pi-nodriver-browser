# Progressive research prefill — validation on the real Pi agent + qwen-mtp 35B (2026-09-30)

> **Superseded (2026-10-02).** Prefill now lives in the Pi harness (`ctx.prefill`), not in an
> extension-side `research-prefill.ts` with a reserved slot; `RESEARCH_PREFILL_RESERVED_SLOT` is gone.
> See [research-streaming-pipeline.md](research-streaming-pipeline.md) for the current design.

## What it does
While research is still crawling, the extension prefills the model with the evidence that is already final,
so the final answer request only has to process the rest.

1. `research/passages.py` `ProgressivePassages`: search snippets are committed as soon as each search settles;
   each page ranked in the top 12 commits its best passage (scored with corpus statistics) as it arrives,
   up to half the C2 budget; `finalize()` adds, in search-rank order, a passage for top pages that were not
   yet covered, then the globally best passages up to the same total budget as C2. Committed text is never
   changed or reordered.
2. `research-prefill.ts` `ProgressiveWarm`: renders the exact chat template once via `/apply-template`,
   then sends RAW prompt text (template head + evidence so far) to `/completion` with `n_predict:0`.
   A raw prefix is required: chat-API warms append closing/generation tokens, and llama.cpp cannot roll
   Qwen3.6's recurrent state back to an arbitrary position, so they reused only ~25% of the warmed tokens.
3. `index.ts`: the final Pi request is pinned to the same slot only if every earlier message matches and
   the tool result starts with the warmed text; otherwise it falls back to ordinary inference.

Enable with `RESEARCH_PREFILL_RESERVED_SLOT=0` (qwen-mtp runs `-np 2`). Unset = original C2 path.

## Results (real `pi` binary + extension, live 4get/Google search, Chrome crawl, qwen-mtp 35B)
Direct server check (same c17 evidence, 3 reps each): final prompt tokens 8,819 → 4,671 with raw-prefix
warms (chat-API warms: 6,713); final prompt eval 4.78 s → 2.73 s.

End-to-end A/B, 10 questions, A (C2) and B (C2 + prefill) interleaved; medians:

| run | final prefill A → B | final new tokens A → B | tokens reused (B) | B faster in |
|---|---|---|---|---|
| ab2 | 5.65 s → 3.77 s | 10,237 → 5,747 | 9,681 | 9/10 |
| ab3 | 6.72 s → 4.84 s | 9,303 → 6,465 | 8,794 | 10/10 |

Direct saving ≈ 1.9 s per question. End-to-end medians also fell (21.8 → 18.4 s, 21.9 → 20.1 s), but
those include crawl and answer-length noise.

## Open issues
- **ab3 answer quality is invalid**: in 13/20 runs — in BOTH arms, including A which never prefills — the
  model called `research` with a previous case's question. The prompts were correct, so stale state leaked
  inside qwen-mtp (logs show LCP slot selection, host prompt-cache loads and context-checkpoint restores on
  the hybrid model; build `gfx1151-gdnfused`, `-np 2`, q4_0 KV). Not reproduced in isolated probes
  (the server re-processed fully, cache_n=0). ab/ab2 (40 runs) had no such case. Until understood, keep
  prefill off in production and treat this as a server correctness bug that can affect any Pi session.
- ab2 quality: B lost c18 because Apple's page had no passage; fixed (top-12 rank gating, corpus-stat
  scoring, rank-ordered finalize) and verified offline on the same evidence, not yet re-graded end to end.
- Tests: 688 Python (90 skipped), 45 Node pass.
