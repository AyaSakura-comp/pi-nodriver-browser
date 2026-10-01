# Research core and integration checkpoint

The primitives below now compose an offline-tested controller and source-registered
`research` tool. This is **not a deployment or live-verification claim**. See
[research-controller.md](research-controller.md) for the integration, supported
model controls, capture/output limits and remaining review/release gates.

## What exists

| Module | Implemented behavior |
|---|---|
| `research/contracts.py` | Validated search tasks, result URLs, ownership identifiers, operational limits |
| `research/budget.py` | Atomic query-attempt reservations and immutable attempt ledger view; no refunds after dispatch |
| `research/frontier.py` | Same-depth FIFO dispatch, level barriers, query dedup and late shallower-topic handling |
| `research/registry.py` | Cross-provider source dedup, exact source URLs, topic associations, guarded crawl lifecycle |
| `research/providers.py` | Async 4get HTTP client and provider-neutral streaming parallel `SearchBatch` |
| `research/decisions.py` | Async Laya `/v1/systemone` protocol client; bounded concurrent rankings, no automatic completeness claim |
| `research/evidence.py` | One queue-consumer writer, cancellation-safe shutdown, private per-job JSONL, capture metadata, full text renderer, snapshot hash/manifest, explicit packet overflow |
| `research/controller.py`, `controller_contracts.py` | Immutable callbacks; single-owner BFS, gap/coverage/citation validation, drain/freeze and stale-result rejection |
| `research/jobs.py`, `crawl_jobs.py` | Daemon-owned job/planner routing and private separately owned crawl scheduler |
| `research/capture.py`, `delivery.py` | Explicit research HTML acquisition ceiling and all-or-incomplete terminal packet |
| `research-model.ts` | Pinned initiating-model direct registry calls and trusted host-clock snapshot |

`SearchBatch` accepts injected adapters implementing `async search(SearchTask) -> list[SearchResult]`. The reusable core includes the 4get adapter; the daemon integration binds Google to `BrowserWorker.search_one` without legacy URL rewriting. Passing a Google task without a Google adapter fails before consuming budget; there is no silent provider substitution.

The Laya client uses the existing state/questions protocol and defaults to loopback port 8000. It does not call browser-intent port 8011, spawn a model process, select new queries, or certify that an answer is complete. The service must already be running for live use. Offline tests use a small local HTTP fixture instead.

## Installation and tests

Use the repository environment; system Python may not have nodriver installed:

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -p 'test_research_*.py' -v
.venv/bin/python -m unittest discover -s tests -v
```

`aiohttp>=3.9,<4` is the new dependency for cancellable, bounded asynchronous HTTP. No service restart or extension installation is required for unit tests. The new tests bind ephemeral loopback ports and never access Google, 4get's upstream scraper, or a real Laya model.

## Composition / ownership rules

1. Standalone core batching uses `QueryBudget` plus `SearchBatch`. The single-owner controller instead reserves `QueryBudget` directly before each dispatch (never nests another scheduler). Use provider-specific normalized query keys; explicit retries require another attempt/charge.
2. A frontier task remains running until its search **and downstream decision work** settle. The controller calls `Frontier.complete`; a search response alone does not release a BFS barrier.
3. Search attempts are reserved directly before launch. Parallel three-query work consumes three units, not one. A dispatched provider error still consumes its unit.
4. `SearchBatch.run()` is an async iterator. Results are yielded as they complete. Use `contextlib.aclosing` when stopping early; closing cancels and joins owned outstanding searches. Do not concurrently call `run` on the same batch.
5. Register exact provider-returned URLs with `SourceRegistry.discover`. Its normalized key is only for dedup, never navigation authority. Fragment variants dedup; paths, trailing slashes and semantic query strings are not rewritten. Terminal `authorizations` retain all exact provider-discovered variants independently of the representative crawl/source URL.
6. `SourceRegistry` and `Frontier` are single-owner components, not thread-safe shared mutable stores. Parallel classifiers return immutable proposals to the controller.
7. `EvidenceWriter` reserves an exclusive job directory and is the only writer. It copies admitted records, rejects unexpected fields, treats identical event IDs idempotently, and rejects conflicting event ID reuse.
8. Every snippet and extract remains separately labelled. Long description text is retained, not clipped to the legacy tool formatter's 1,000-byte description cap. The 4get response still has a 5 MiB decompressed-body safety cap, 100-candidate sanitation pass and Top-10 result limit.
9. `freeze` is an admission barrier: queued records are written first, subsequent appends are rejected, and a manifest with SHA-256 and terminal metadata is flushed. Closing without freeze retains a partial journal; crash recovery/resume is not implemented yet.
10. `Snapshot.render(max_bytes=...)` preserves all text, identifies it as untrusted source data, checks the hash, and raises `PacketTooLarge` rather than silently trimming. The integration additionally checks 50 KiB/2,000-line terminal and estimated context limits; overflow is explicitly incomplete, not a successful full delivery.
11. Close provider/Laya clients after their owned tasks settle. `ResearchController.pending_search_callbacks` exposes outstanding search lifecycle handles after cancellation/expiry/freeze; integration bounds their cleanup, cancels stalled cooperative closes and joins/observes them before client/owner cleanup. Truly non-cooperative work remains tracked and causes explicit cleanup failure. Laya max-state limits reject oversized inputs explicitly; they do not truncate the final evidence journal.

## Minimal offline example

This example exercises accounting and journal creation only. It makes no search or model request:

```python
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from research.budget import QueryBudget
from research.evidence import EvidenceWriter

async def main():
    budget = QueryBudget(2)
    granted = await asyncio.gather(*(budget.reserve(f'q{i}') for i in range(3)))
    assert sum(granted) == 2
    with TemporaryDirectory() as directory:
        async with EvidenceWriter(Path(directory), 'demo') as writer:
            await writer.append({
                'eventId': 'e1', 'sourceId': 's1', 'topicIds': ['t1'],
                'kind': 'search_snippet', 'url': 'https://example.com/',
                'title': 'Fixture', 'text': 'Complete fixture text.\nSecond line.',
                'status': 'completed', 'truncated': False,
            })
            snapshot = await writer.freeze(
                status='incomplete', reason='fixture_only', gaps=['Verify the answer'])
        print(snapshot.render(max_bytes=10000))

asyncio.run(main())
```

For a parallel end-to-end **core fixture**, see `tests/test_research_pipeline.py`. Its two search requests must reach the fixture simultaneously, share one URL/source, retain both full descriptions, and create one verified snapshot. Controller, daemon socket, crawl-process and extension binding behaviors have separate `test_research_controller*`, `test_research_jobs`, `test_research_daemon`, `test_research_crawl_jobs`, `test_research_extension` and TypeScript model fixtures.

## Remaining verification and scope

- Independent acceptance review and operator-approved live browser/provider/model verification.
- Semantic citation quality, richer named requirement decomposition and contradiction resolution.
- Full telemetry/benchmark tooling and measured end-to-end/replay evaluation.
- Broader supported thinking controls and larger one-turn evidence delivery. The conservative context check is an estimate, not a guarantee against all simultaneous sibling tool outputs.
- No 1,500-TPS scenario or speedup has been measured by these fixture tests. No services were restarted and no extension was installed.

Detailed checklist: [BFS research implementation plan](plans/2026-09-28-bfs-research-controller.md).
