# Binary Laya research loop implementation plan

Goal: Planner owns topic guides and multiple parallel keyword searches; Laya gates crawl yes/no and, after the round's crawls settle, requests another search direction yes/no. Host preserves job-local search history and full evidence across iterations.

Architecture: retain the single-owner controller, exact provider URL authority, private crawl consumer and direct current-Pi-model planner. Add a binary HTTP client and a round-review callback. Post-crawl feedback is advisory evidence routing, never a source citation or a calibrated correctness certificate. A `false` review still needs planner-cited support; true/unknown must not be overridden by planner finish. Budgets, no-progress, cancellation and size limits stay bounded.

Tech: Python asyncio/aiohttp, TypeScript planner, offline unittest/Node fixtures, standalone white-background SVG/HTML diagram.

## Steps (RED → GREEN)
1. Add `tests/test_research_binary_loop.py`: binary protocol (`noul`), invalid probabilities, per-topic crawl input, post-crawl full-text feedback, no multi-action choice. Run focused tests and observe missing-feature failures.
2. Add `LayaClient.decide` in `research/decisions.py`, with existing owned HTTP/session limits. Add `research/workflow.py` to bind only crawl/ignore and needs-more predicates. No service restart or different model.
3. Add review contract/view feedback in `research/controller_contracts.py` and `research/controller.py`. Test round barrier, true→different-query loop, false+cited-support finish, failure/oversize unknown, rejected premature finish, cancellation and finite budget. Save raw snippets automatically as observations without coverage in new production mode; only full applicable evidence supports completion.
4. Wire reviewer into `research/jobs.py`; injected callbacks remain offline-test seams. Add a full ResearchConnection test with loopback Laya and owned crawl fixture. Old isolated controller fixtures may omit review for backward-compatible seam testing; production always supplies it.
5. Update `research-model.ts`: pass host-owned review feedback and full job-local semantic query/direction history; prompt for distinct topic guides and parallel searches; reject finish when more/unknown feedback exists. Keep strict authority checks and original snapshot revisions. Node regression test first.
6. Run `.venv/bin/python -m unittest discover -s tests -p 'test_research*.py' -v`, Node planner tests, full suite, strict TS check, `git diff --check`. Preserve unrelated operator edits. No commit/push/deploy.
7. Update workflow docs and render/inspect a self-contained Japanese-white SVG/HTML/PNG showing real implemented loop and bounded exits.

## Limits / acceptance
- No query text or navigation authority may be invented by Laya. No old crawl/use_snippet/search_more multi-choice in production.
- All scheduled crawls settle (or fail/drain) before round review. No unfinished/failed review may imply sufficient.
- Repeated normalized queries remain deduplicated, history is never silently dropped; input overflow is explicit, not silent truncation.
- Source text remains untrusted; full captures stay in the one evidence artifact. Existing Laya is a bounded-context classifier, not full-document correctness proof. Oversized review inputs/errors produce unknown feedback and bounded incomplete delivery, never fabricated completion.
- Regression tests prove mechanics, not a new quality benchmark. Report that distinction.
