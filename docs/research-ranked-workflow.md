# Ranked mixed-search research (current source workflow; not deployed)

## Default flow

1. The invoking Pi model is a **search-only Planner**. It receives the original question, complete job-local query/direction history, available provider slots and remaining query budget. No article text, SERP descriptions or evidence assessments enter the Planner.
2. Each full default wave contains **three 4get queries and one Google query**, dispatched concurrently. The host assigns engines; the model only supplies query, direction and allowed routing addresses. Each adapter returns at most ten candidates per query. An empty plan is allowed if nothing justified can be searched. Duplicate-query suppression can make a wave smaller; it never invents filler queries.
3. Wait for the wave's searches to settle, merge URL identities, and exclude queued/fetching/completed/failed URLs before ranking. An exact URL's first successful-provider spelling remains its navigation authority; normalized keys never grant new URL authority.
4. **Laya ranks page candidates, not actions.** A bounded ternary tournament compares at most three title/description previews at a time. Local winners advance; selected winners are removed and only their paths are replayed. The remaining tree is cached for the next batch. Separately normalized probabilities are never merged into one global distribution.
5. Take the next **four pages** by default and crawl them (concurrency two). This is a batch size, not a job-wide crawl count. The old six-crawl cap is not used in ranked mode.
6. Accumulate approximately **20,000 text units** from captured page text. Once reached, stop starting new crawls and drop queued ones; already-running pages are preserved in full. This is a soft threshold and can overshoot. If below threshold, take the next ranked batch; after candidates are exhausted the Planner may produce another new wave within the search budget.
7. Hand the preserved, source-labelled evidence packet back to the invoking Pi, which writes the answer. No full-article Laya post-review and no final Planner evidence review. `collected` means collection stopped, **not verified answer correctness**.

[Diagram](research-workflow.html)

## Pure ranking, not another hidden rejection gate

The production page ranker passes `include_none=False`. Only page IDs are options; a singleton advances without inference. This is intentionally a relative top-N selector. It does not claim every selected page is relevant or correct, and it does not discard an entire pool because an assistant-added `none_match` option wins. Live diagnostic trials with that extra option were retained: even NASA results were rejected. API/malformed-label failures remain explicit `failed_ids`, not fabricated rankings or irrelevance. The legacy generic choice helper still supports none for its old isolated callers.

Laya sees only bounded title/description previews. The full original text is not altered. Its small model context and approximate, order-dependent tournament mean this is not a global calibrated relevance distribution or a quality guarantee. Different URLs with the same article are not content-deduplicated.

## Parameters

```json
{
  "question": "...",
  "provider": "auto",
  "searchBudget": 8,
  "searchConcurrency": 4,
  "layaConcurrency": 2,
  "crawlWordBudget": 20000,
  "rankBatchSize": 4
}
```

- `auto`: host 3:1 cycle of 4get/Google slots. Explicit `4get` or `google` remains available.
- `searchBudget`: actual provider-query attempts, default8, max20; no hidden fallback/replay.
- `crawlWordBudget`: approximate page-text units, default20,000, range1–100,000. English words and CJK characters count cheaply; long runs have an eight-character floor so minified/punctuation text cannot evade the threshold. **No tokenizer/model call calculates the budget.** It is not an exact model-token limit.
- `rankBatchSize`: default4, max16; not a total crawl limit.
- Search snippets and source/capture headers add handoff overhead beyond the page-text threshold.

## Safety, failures and delivery

Existing owner-only registry/writer, exact provider URL authority, private crawl scheduler, same-model Planner identity checks and cancellation/cleanup remain. Ranking gets a separately bounded aggregate deadline based on the original pool depth, batch size and concurrency rather than one ordinary HTTP-call timeout. Ranking failure does not mark unprocessed pages irrelevant. Individual classification failures are distinct from successfully ranked pages.

Full page captures remain untrimmed in the journal. PDF temp-wiki/partial captures are not treated as complete page evidence. At most the existing in-flight crawl slots can continue after the word threshold. Research-only transport safety limits are now **2MiB / 20,000 lines**. Other browser/tool50KiB limits are unchanged.

The invoking extension estimates context cost as UTF-8 bytes/2 and reserves half the remaining context plus16k for prompt/output/sibling tools. This is deliberately a heuristic, not precise tokenization or a universal context guarantee. Unknown/insufficient context or a hard transport overflow remains explicit incomplete with preserved artifact. Normal packets reach Pi as one tool-result text; no manual file reread is required.

## Verification (2026-09-28)

- 659 Python tests run: **569 passed,90 skipped**; 36 Node tests passed; scoped strict TypeScript passed; diff/static checks passed.
- Independent broad review found an aggregate-ranking deadline issue; RED→GREEN regression and focused review cleared it. Selected/failed/rejected disjointness and the final pure top-N change also received focused review.
- Full entry type checking remains blocked by unrelated unmodified `intent/tool.ts:319` TS2339. No deployment, commit or push performed.
- Existing Laya CPU service was inactive during initial live attempt. That failure was retained; the existing unit was explicitly started through the service hub. Qwen/shared browser services were not restarted.
- Final frozen c05 live run: **3×4get +1×Google**, 37 results →34 URL identities, 69 bounded page comparisons, 20 crawls attempted /15 nonempty captures, **20,706 rough units** collected against20,000 threshold. Five crawl failures were not hidden.
- Full packet **142,241 bytes** successfully reached Pi. Pi produced an answer; actual final model input was43,544 tokens including1,404 cache-read tokens (usage observation, not the budget algorithm). Research work29.81s; end-to-end74.99s. No general answer-quality improvement is inferred from one run.

Durable evidence: `~/.pi/agent/generated/research-ranked-20k-20260928/`, including all earlier failed/abstaining trials, final source hashes, full raw trace, reviews and answer. Older [integration notes](research-controller.md) and binary/search-only reports are historical or describe shared low-level invariants, not the current default selection/delivery policy.
