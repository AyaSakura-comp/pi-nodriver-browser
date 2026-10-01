# Binary research loop — verification record

## Implemented

- Planner defines topic/direction guides and multiple keyword queries; host retains the entire job-local admitted query/direction history and sends it each round.
- Laya `noul` predicate 1: `should_crawl`, from original question, topic/guide, query, exact observed URL, title and descriptions.
- Each source gets a separate bounded judgment callback in production. A failed source cannot discard already successful siblings; a search settles only after every source's judgment settles. Parallelism uses existing Laya semaphore/controller limits.
- Authorized crawls preserve complete captured text through the existing separate owned scheduler and single writer. Raw search snippets are saved automatically but carry no answer-coverage addresses.
- All current searches/judgments/crawls settle before Laya predicate 2: `needs_more_search`, using accumulated applicable complete captures and query/direction history.
- True/unknown feedback reopens gaps and is sent to Planner for a different search direction. False still requires applicable cited evidence and no unresolved contradictions. Budgets, no-progress, cancellation and existing delivery limits remain enforced.
- Native return details include `reviewHistory`. Laya scores and feedback are not source evidence or navigation authority.

## RED → GREEN and review

Initial new tests failed on the missing binary protocol, workflow binding and review contract. The production connection fixture separately failed because the old multi-choice path produced no post-crawl review. Two Node tests failed because feedback was absent and finish was not vetoed. All pass after implementation.

Independent read-only review found P2: one late source failure or one whole-batch timeout discarded accumulated positive crawl classifications. Two added regressions reproduced both failures. Production now schedules singleton per-source judgments with independent deadlines and aggregates completion via `_judges_remaining`. A focused independent follow-up reviewed that fix, ran 20 offline controller tests, and returned **OK**, with no new P1/P2 scheduling finding.

## Final checks

- `.venv/bin/python -m unittest discover -s tests -v`: **642 run, 90 skipped, 0 failures/errors** (552 passed).
- `node --experimental-strip-types --test tests/research-model.test.ts tests/research-planner-contract.test.ts`: **27 passed**.
- Strict TypeScript for `research-model.ts` plus both planner test files: **passed**, using installed Pi type declarations.
- Full extension entry type check: **blocked** by unmodified/operator-owned `intent/tool.ts:319`, TS2339 (`slice` on `{}`). This file was not changed to hide the blocker.
- `git diff --check`: **passed**.
- Offline HTTP fixture exercises the real binary Laya wire protocol and real owned crawl-consumer IPC, including a two-round needs-more→different-search→finish sequence.
- Diagram HTML/SVG rendered with headless Chromium and PNG visually inspected.

## Limits, not acceptance claims

No live model/browser quality run, 20-question rerun, production service change, installation, commit or push was done. A classifier returning no is not proof that all facts are correct. Laya's own finite model context may truncate internally; the adapter separately rejects states above 12,000 characters without shortening captured artifacts. Large/failed review inputs are unknown and cannot authorize sufficient completion. The registry still uses the broad `answer` requirement, so full semantic subclaim decomposition and live completeness remain future work.

Legacy `review=None` controller fixtures and the standalone `choose()` helper remain compatible. Production `ResearchConnection` always wires the two binary predicates; those compatibility seams do not replace its workflow.
