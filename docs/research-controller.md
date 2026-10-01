# Earlier research integration notes (historical; not deployed)

**The default source workflow has been superseded by [ranked mixed-search research](research-ranked-workflow.md): 3×4get +1×Google, page-choice ranking and a rough20k crawl budget.** Binary/source-review selection and50KiB delivery policies described below are historical; shared ownership, exact-URL and cancellation details still explain the underlying components.

The `research` tool is registered in the source extension. Isolated live
experiments exposed planner-contract and evidence-quality failures; they are
not a production deployment or a passed live acceptance gate. Existing
standalone tools remain available.

```json
{"question":"Compare the documented behavior and limitations of feature X","provider":"4get","searchBudget":6,"searchConcurrency":3,"layaConcurrency":2}
```

## Ownership and scheduling

- One controller in the existing browser daemon owns each journal, frontier,
  query budget, source registry and completion decision. Searches and source
  judgments overlap within their limits. Production uses one binary judgment per
  source, each with its own callback deadline and failure isolation; a search task
  settles only after all its source judgments settle. The next planning round waits
  for current searches, judgments and queued/in-flight crawls, then runs the binary
  post-crawl review. Crawling still overlaps searches inside a round.
- `4get` is the default. `google` uses one real browser query per charged attempt.
  `auto` requires 4get roots and permits planner-selected Google child queries
  for unresolved gaps. There is no hidden retry or automatic transport replay.
  Cross-provider queries consume separate budget units.
- The daemon permits two running research jobs globally and one per client
  connection. Research bypasses the interactive session lock and TypeScript
  queue, but **does not remove** either lock for ordinary interactive commands.
  Standalone and research search/crawl operations share a daemon-wide background
  semaphore (at most `max_tabs - 1`, minimum one), managed-tab admission, active
  target accounting, transactional eviction and the global tab cap.
- Each job starts a separate, owned Python crawl scheduler, not Chrome or another
  agent. An inherited socketpair is the job's private capability. Only opaque
  request IDs cross it: exact URLs and extraction text remain in the daemon.
  The child cannot choose a URL or append a journal. Its environment contains no
  inherited API keys, auth headers or browser profile settings. EOF stops it.
- Successful typed provider results register exact navigation authority **before**
  crawl admission. The original Google observed href is used, not legacy URL
  canonicalization/redirect normalization. Terminal `sources` retain one crawl
  identity per deduplicated page; separate typed `authorizations` export every
  exact successful-provider URL, including observed fragment and hostname-case
  variants. Only those exact spellings permit subsequent opens; packet/page/model
  prose is never scanned to grant new authority. Existing exact-user/search URL guard wording is retained.

## Short initiating-model planner

`research-model.ts` defensively copies the invoking model descriptor and calls
that invocation's `ctx.modelRegistry.complete` for every plan. The installed `pi`
executable was resolved to its bundle, where this method delegates to the runtime
provider/auth path. Credentials stay in that host runtime; none are serialized
into daemon frames, subprocess arguments or evidence artifacts. No nested Pi
session, harness, tools, skills, conversation history or model fallback is used.

Only the original question, full job-local query/direction history, allowed routing
IDs, remaining budget and compact Laya feedback are sent. The model drafts exactly
`{searches}`; a search contains query, direction, provider and routing addresses.
**No articles, snippets, source descriptions, source URLs or evidence IDs enter the
planner input.** `research/jobs.py::planner_view()` projects an explicit metadata
allowlist before IPC serialization/size checks; `research-model.ts::modelView()`
independently allowlists the model payload. Old `assessments`/`finish` output fields
are rejected: this model does not read evidence, answer questions or decide stop.

Job IDs, revisions and task-parent IDs stay in the host. Search history retains all
admitted queries/directions/providers/routing addresses. Compact `review_feedback`
contains only needs-more boolean/unknown and a safe failure reason. History is
host-owned, not a persistent model chat. The host also filters case/whitespace-
equivalent queries against previous queries and this batch, including a provider
change. This is exact normalized de-duplication, not guaranteed semantic novelty. The host captures the original request before awaiting the model, attaches
that original revision, and assigns each follow-up to the newest deepest prior
task sharing a requested gap (or the newest deepest round predecessor when that
gap has no prior search). Initial searches have a null parent. The controller's
existing current-revision guard still rejects genuinely stale proposals; the
host never re-stamps a response with a newer live revision.

Input is capped at 48 KiB (and conservatively reduced for the pinned context
window), output at 2,048 tokens/32 KiB per attempt. Syntax/shape mistakes may
receive **one** same-model format correction, within the **same total 20-second
abort deadline** and input cap. The original state and malformed output are
included as data; neither is silently clipped or turned into instructions.
A second bad format terminates the plan. Fabricated references, disallowed
providers/navigation fields, budget violations, transport/auth errors and model
identity mismatches are not eligible for this repair. SDK transport retries
remain zero. Only a valid normal text-only completion reaches the controller.

All matching-identity attempts with valid reported usage contribute to tool
usage, including a length-terminated repair. The
terminal tool details include `plannerDiagnostics` with `modelCalls`,
`formatRepairs` and `formatFailures`, so the additional call is not hidden.
The controller independently revalidates every assembled proposal.
Descriptor-level server-side fallback is not disabled by SDK retry limits:
research rejects `compat.allowedFallbackModels` (including an empty field),
Vercel gateway routing, and OpenRouter routing without explicit
`allow_fallbacks:false` before transport. The invoking descriptor is not mutated.
Returned provider, model ID and API must exactly equal the captured identity;
substituted/missing identities fail explicitly. No unverified aliases or prefix
matching are accepted.

Thinking controls are API-specific: Anthropic messages uses
`thinkingEnabled:false`; Google generative AI uses `thinking.enabled:false`
(reasoning models limited to the disable-capable Gemini 2.5 Flash family);
OpenAI responses/completions use the installed adapter's verified off mapping.
Reasoning configurations without an explicit supported off path, a null off
mapping, unknown APIs or unsafe arbitrary sampling overrides are rejected.
Non-reasoning supported models are accepted. Fixture tests verify pinning,
options and errors, **not live provider/model semantic quality**.

A host-generated `Date`/`Intl` ISO/timezone snapshot is captured on invocation,
using an injectable clock helper. This is the approved trusted host-clock bridge,
not a planner-supplied timestamp or an invented cross-extension gettime call.

## Binary Laya workflow

See the [workflow diagram](research-workflow.html).

Production uses two separate `type: "noul"` predicates, not a multi-action choice:

1. `should_crawl`: original question + planner's topic/direction guide + query +
   observed URL/title/descriptions → yes/no. Yes queues an authorized crawl; no
   skips it. Laya does not choose `use_snippet`, invent queries or request tools.
2. After the round's crawls settle, `needs_more_search`: original question,
   job-local search history and accumulated complete, applicable page captures →
   yes/no about another different search direction. No complete crawl produces a
   host-owned `no_complete_crawl` request for more, not invented model feedback.
3. True or unknown requests another search-only plan when budgets permit. False
   stops the collection loop without another planner call, provided at least one
   complete applicable page was captured. The terminal status is
   `collected/laya_no_more_search`, **not** `sufficient` or a factual-completeness
   certificate. The final answering agent must read and assess the evidence.
   Budget exhaustion also stops without a final evidence-assessment planner call.

Both predicates use P(true) >= 0.5 as a binary classification boundary, **not**
a calibrated probability of factual correctness. Malformed/failed/oversized review
becomes unknown and cannot produce sufficient. The adapter's 12,000-character
state cap rejects rather than trims. The existing Laya service also has a finite
model context and may internally truncate long inputs; this remains a live-quality
limitation, not a full-document verification promise. Full captures remain intact
in the evidence artifact regardless of classifier limits.

Snippets are saved automatically as observations **without coverage addresses**.
Complete captures remain with the controller/writer and Laya, not the Planner.
Terminal details export `reviewHistory`; feedback is not source evidence. A
`collected` packet explicitly says answer correctness/completeness is unverified.
Optional `review=None` and legacy action handling remain as isolated controller
callback seams; `ResearchConnection` always supplies the binary workflow review.

The requirement registry still uses the single whole-question `answer` ID. Topic
guides are planner-generated descriptions, not independently verified subclaims.
This change does not claim to fix factual completeness, query drift or calibrated
stopping. The search-only c05 live rerun is recorded in
[search-only validation](research-search-only-validation.md); remaining Laya and
packet-size limits are explicitly reported.

## Full captured evidence, with explicit limits

The journal preserves snippets and page captures separately, including text,
line breaks and Unicode, without semantic trimming or LLM summaries. Google
retains its upstream 20-result/600-character snippet cap and four-line fallback;
actual description truncation is propagated. 4get retains its existing bounded
HTTP sanitation and Top-10 policy.

Research HTML acquisition is capped at **1,000,000 JavaScript UTF-16 units** per
page, separately checked against a **4,000,000 UTF-8-byte** transport ceiling.
The acquisition boundary never splits a surrogate pair. `sourceChars`,
`capturedChars`, `captureLimit`, `captureUnits` and `truncated` are journaled.
This is a safety ceiling, not a relevance filter. Every captured character is
retained. Standalone crawl's capture policy is unchanged. Capped extracts cannot provide coverage;
other complete evidence must independently support the answer for sufficiency.
PDFs keep existing extraction limits. `temp-wiki` notices are retained and labelled
incomplete, never treated as unlimited full-document text or supporting evidence.
Job-private PDF assets are cleaned at job end; this tool does not promise later
`pdf_query` access to its private wiki.

After a bounded drain, one immutable snapshot is rendered with source IDs,
URLs, evidence kinds, capture metadata and unresolved gaps. Terminal text must
fit **50 KiB and 2,000 lines**, plus a conservative context reserve. The host uses
one token per UTF-8 byte as an upper-bound heuristic, leaves 16k for prompt/output,
and uses at most half the remaining estimated context for this packet. Unknown
context usage fails closed. This estimate cannot guarantee space against all
simultaneous sibling tool outputs.

When it does not fit, the tool returns `incomplete/packet_too_large` with
`fullEvidenceDelivered:false` and the immutable artifact path/hash. **That is not
a successful one-turn evidence delivery.** No partial evidence, automatic
reread, compaction, hidden model turn or semantic summary is substituted. UI
progress contains status only; the final packet is not duplicated in details.

Cancellation/disconnect stops owned work, drains/freezes partial evidence and
reaps the owned consumer. Repeated cancellation cannot interrupt journal or job
cleanup. If an extraction ignores cancellation or target cleanup is still pending
after bounded waits, the tool fails explicitly with cleanup-incomplete, retains
tracked ownership for eventual cleanup, and does not claim success. It never
kills the shared daemon, Chrome, unrelated jobs or services to resolve this.

Cancelled/expired search callbacks remain available through the controller's
`pending_search_callbacks` lifecycle view even after evidence freezes. Job
finalization joins them, cancels stalled cleanup again, and observes completion
before owner cleanup/client shutdown. Genuinely non-cooperative callbacks retain
tracked ownership and prevent a successful terminal result. Shared search/crawl
tab closes (including later owner cleanup) have a two-second close deadline;
cooperative cancellation releases the tab-management lock and both admission
slots even if eviction raises. Unconfirmed tab closure leaves registry ownership
intact, and later cleanup revalidates the exact owner under the lock.

## Verification and remaining gates

Search-only correction (2026-09-28): **647 Python tests ran (90 skipped), 32 Node
tests passed**, and scoped TypeScript passed. Independent read-only review: OK.
The c05 live rerun made three successful planner calls with **333 / 487 / 573
input tokens**, no article fields in any IPC/model payload, six search queries,
and no planner failure. It still ended incomplete: Laya's post-crawl input cap
and final packet-size cap were unchanged. No deployment or timeout increase.

The following binary-loop and earlier checks are historical:

Binary-loop update (2026-09-28): offline fixtures cover a real loopback Laya HTTP
client, owned crawl consumer, two search rounds, parallel crawl settlement,
automatic raw-snippet preservation, true/unknown finish veto, source failure
isolation, per-source deadlines and cancellation. **642 Python tests ran (90
skipped), with no failures; 27 Node tests passed.** Independent review found one
source-batch failure-isolation issue; per-source scheduling and two RED→GREEN
regressions fixed it, and the focused follow-up review returned OK. These checks
prove mechanics, not live model quality. Scoped planner/contract strict TypeScript
passes; full entry
checking is blocked by an unrelated, unmodified `intent/tool.ts:319` (`slice` on
`{}`). No new 20-case benchmark or deployment was performed for this loop.

Historical planner-contract validation follows (not new loop acceptance):

After the host-owned planner contract fix: **624 Python tests, 90 skipped, no
failures**, and **25 Node planner/contract tests passed**, with strict TypeScript
validation including the new contract test. Two independently reviewed fixes
also cover forbidden navigation fields masked by malformed shape and usage from
length-terminated repairs.

A planner-only live replay of six previously recorded Qwen request snapshots
(five initial questions plus one follow-up) returned six contract-valid proposals,
one model call each. This validates real model output binding, not answer
correctness, search/crawl decisions or a new 20-case quality score. A separate
five-question end-to-end smoke was blocked by an inactive Laya service; no
service was restarted to hide that limitation. Offline tests and this replay
must not be presented as a completed end-to-end acceptance test.

- Python tests use `.venv/bin/python`; local HTTP fixtures, real temporary Unix
  sockets, fake managed tabs and a real owned crawl subprocess require no live
  website, model or Chrome instance.
- TypeScript fixtures run with the already-installed Node 22 strip-types runner:
  `node --experimental-strip-types --test tests/research-model.test.ts tests/research-planner-contract.test.ts`.
  Python extension fixtures also execute actual extracted TypeScript with Node.
- Type-check the extension/helper against the invoking Pi installation's type
  declarations using its existing `tsc`; do not install dependencies merely to
  run this check. The checked host exposes `ModelRegistry.complete`.
- `tests/test_install.py` now invokes only `install.sh --stage <temporary-dir>`.
  That branch copies runtime files and exits before all system checks, settings,
  process/socket/display cleanup, dependency installation and service operations.
  Lifecycle commands are additionally stubbed; staged imports are exercised.

Operator-approved live release verification remains required. The binary-loop
review and verification record is in [binary-loop validation](research-binary-loop-validation.md). No external search, model call, extension installation, service restart,
performance benchmark or throughput measurement was performed. The plan's
**1,500 new tokens/second** remains an evaluation assumption, not a measurement.
Full telemetry/benchmark tooling, contradiction resolution, broader thinking
compatibility and larger one-turn packets are not implemented.
