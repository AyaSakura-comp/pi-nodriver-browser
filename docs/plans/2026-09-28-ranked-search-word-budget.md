# Ranked mixed-search + approximate 20k crawl budget

Goal: one wave plans four new queries, dispatches 3×4get + 1×Google concurrently, deduplicates URLs/excludes already queued/fetched sources, ranks page candidates with Laya choice, crawls in ranked batches until an approximate 20,000-word threshold, and hands preserved evidence to the invoking Pi for the answer.

Architecture: retain existing single-owner controller, exact successful-provider URL registry, private crawl consumer and evidence writer. Add a ranked production mode, keeping old injected test seams separate. Planner remains metadata-only; host assigns provider slots. No full-article Laya post-review. Candidate choice is a bounded 3-way tournament with an explicit none option, not a misleading merge of separately normalized probabilities. Reuse the tournament between batches of the same candidate pool; source text remains intact in the journal.

## RED→GREEN tasks
1. New rough-word counter and tests (Latin words/long runs plus CJK characters, no tokenizer/model). Add Limits fields for word threshold/rank batch size; no six-crawl cap in ranked mode. Finish in-flight pages; soft threshold may overshoot, never silently crop a document.
2. New Laya page-ranker tests: known-ID-only choices, bounded candidate groups, top-N tournament selection without cross-group probability comparison, none/error handling, reusable remaining pool.
3. Controller tests: search barrier before ranking, pre-rank URL dedup and completed/failed exclusion, ranked batches, word-budget stopping including queued/in-flight behavior, no model review of articles, cancellation/failures.
4. Planner/connection tests: compact metadata + host provider slots; search-only model output without provider authority in ranked mode; 3+1 mix and exact query count per wave; duplicate suppression; real fixture-backed handoff.
5. Public tool defaults: provider auto, search budget8, concurrency4, crawlWordBudget20000, rankBatchSize4. Raise research-only packet safety ceiling to2MiB/20,000 lines; retain a heuristic context reserve without exact tokenization. Other tools'50KiB limits remain untouched.
6. Full Python/Node tests, scoped TS, static/diff review; preserve operator changes. Independent read-only review. Run one isolated c05 end-to-end Pi trial with new parameters; report actual answer, budget/latency and any failures rather than claiming quality from crawl quantity.

## Acceptance / limits
- No articles/snippets/evidence IDs in Planner IPC or model payload.
- No Laya binary crawl-action selection or full-text post-review in production ranked mode; choices are page IDs.
- No repeated crawl of identical registered URL; exact navigation authority is unchanged. Different URLs with identical content are not semantic-deduplicated in this task.
- The approximate threshold applies to page text, not exact model tokens. Source metadata and captured SERP observations add handoff overhead. At most already-running captures may overshoot.
- `collected` is collection completion, not factual correctness. Unresolved API/provenance/transport/context failures remain explicit. A single comparison's probability is not a global calibrated relevance probability.
- No service restart, global deployment, commit or push without authorization. Existing unrelated intent/tool.ts type error may remain a full-entry check blocker.

## Final implementation decisions

Live diagnostics showed that the assistant-added none option rejected even NASA candidates, contrary to a pure top-N ordering request. Final PageRanker therefore compares only page candidates (`include_none=False`), treating invalid/API outcomes as unknown/errors, not an absolute relevance gate. The generic legacy choice helper retains none support. The initial none-based trials are preserved, not substituted with successful scores. The existing Laya CPU unit was found inactive and explicitly started for validation; no Qwen/shared browser restart occurred.

Independent review's aggregate-ranking timeout finding was fixed with a separately bounded tournament deadline; source failures remain distinct from irrelevance. Final c05 reached the rough threshold, delivered the full packet to Pi, and produced an answer. See docs/research-ranked-workflow.md for exact results and limits.
