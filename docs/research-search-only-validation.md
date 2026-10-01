# Search-only Planner — correction and c05 live rerun

## Contract

The production Planner only returns `{searches}`. It receives original question, routing IDs, all admitted query/direction/provider history, remaining budget and compact Laya more-search feedback. It receives no article, snippet, source title/URL/description, evidence ID or requirement assessment. Python projects metadata before IPC serialization and the 64-KiB frame check; TypeScript separately allowlists the model payload.

Host adds original revision and deterministic parents. Case/whitespace-equivalent query duplicates are removed across history and the new batch, including provider changes. This is exact normalization, not guaranteed semantic novelty. Output assessments/finish/evidence authority is rejected.

Laya false + an applicable complete capture ends collection without another Planner call. Status is `collected/laya_no_more_search`, with an explicit unverified-answer disclaimer; it is not factual sufficiency. The final answering agent assesses actual evidence. Exhausted budgets also do not trigger an evidence-assessment Planner call. Historical isolated `review=None` callbacks retain their old contract; production never uses that seam.

## Tests / review

New article-proof transport/model tests and controller-stopping tests were RED before implementation and GREEN afterward. Old wire fixtures were migrated to searches-only. Independent read-only review returned OK (5 Node and 45 focused Python tests independently run).

- Full Python: 647 run, 90 skipped, 0 failures/errors (557 passed).
- Node planner suites: 32 passed.
- Scoped strict TypeScript (planner + all three test files): passed.
- Full entry TypeScript: still blocked by unchanged `intent/tool.ts:319` TS2339 (`slice` on `{}`).
- `git diff --check`: passed.

## c05 (previously slowest case) — one live retry

Same NASA seasons question, Qwen3.6-35B-Q4, thinking off, 4get, search budget6/concurrency3, Laya concurrency2, crawl cap6, and **unchanged 20-second Planner deadline**. Isolated benchmark socket/profile; no service restart, deployment, commit or push. Source hashes unchanged during the run.

- End-to-end: **152.49 → 30.64 seconds**. Different-time single trials, not isolated/interleaved performance A/B.
- Planner actual input tokens: **333 → 487 → 573** across three successful calls, no format repairs. Prior follow-up was approximately 11,184 text tokens (retokenized, excluding chat wrappers).
- Planner latencies: 4.404 / 2.316 / 1.424 seconds.
- Search history counts: 0 / 3 / 5. Six distinct query strings issued in batches 3 + 2 + 1.
- Planner IPC frames: 580 / 1566 / 2094 bytes; no forbidden article/evidence/source fields.
- Crawls: 6 attempted, 5 nonempty successful captures.
- **Not an end-to-end answer success**: three post-crawl Laya states (39,721 / 40,105 / 40,291 characters) still exceeded its unchanged12,000-character cap. Unknown feedback caused more search; controller eventually stopped `incomplete/budget_exhausted`. Final handoff hit `packet_too_large`; Pi declined to answer.
- No Planner timeout or planner_failed occurred in this retry. Model-service contention, Laya long-context handling and large packet delivery remain separate unresolved issues.

Artifacts: `~/.pi/agent/generated/research-search-only-20260928/` contains `case-c05-report.html`, `case-c05-result.json`, raw runs, input/output traces, code freeze, review and updated workflow diagram. No new general answer-quality score is claimed.
