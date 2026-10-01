# Search-only Planner correction

Goal: Planner only produces new search queries, with original question and job-local query/direction history. It never receives articles/snippets/source descriptions or evaluates evidence/completion.

Architecture: compact the Python planner IPC frame with an explicit metadata allowlist before serialization/size checks; independently allowlist the TypeScript model view. Model output is exactly `{searches}`; host adds revision/parent IDs. Laya owns the no-more-search signal, and the controller owns bounded stopping. A normal Laya stop is `collected/laya_no_more_search`, not a factual-sufficiency claim. Full evidence remains in the journal for the final answering agent. Existing legacy standalone controller tests may retain assessment contracts, but production ignores/rejects them.

1. RED: new Node tests for article-proof compact payload, search-only output, rejected completion/evidence authority, duplicate keyword filtering; Python tests for compact wire serialization despite large evidence, strict parser, Laya stop without final planner call, and no Planner at exhausted budget.
2. GREEN: modify research-model.ts, research/jobs.py, research/controller.py, research/contracts.py. Keep URL ownership, source preservation, same-model routing, history and 20-second timeout unchanged. Update old wire fixtures to the new schema; retain independent legacy controller tests.
3. Check focused/full Python suite, Node suites, scoped TypeScript and diff whitespace; independent read-only review if authorized. No deployment or unrelated source changes.
4. Re-run c05 (NASA seasons), longest prior case at 152.49 seconds, with the same provider/model/budgets. Trace actual planner payload/token usage and compare. No in-run prompt/timeout tuning. Report remaining Laya input/capture-delivery limits honestly; do not claim this fixes all workflow failures.
