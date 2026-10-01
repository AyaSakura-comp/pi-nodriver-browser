"""Event-driven research orchestration; only run() commits global job state.

Providers implement the existing SearchProvider protocol. The planner is a short
injected model call. Production uses binary crawl gating and a post-crawl round
review (neither probability certifies coverage). The crawl callback is an IPC seam,
NOT a crawler: its consumer must enforce job ownership and exact URL provenance.

Workers return proposals through task results and cannot append evidence. A
callback that ignores cancellation can outlive the job, but its result is
observed/discarded, never committed after the bounded drain/freeze barrier.
Provider/model/IPC client lifetimes belong to the caller. One controller runs
once. Cancellation returns a frozen cancelled result, rather than re-raising.
"""
import asyncio
from collections import deque
from dataclasses import dataclass, replace
import math
from pathlib import Path
from typing import Any

from .budget import QueryBudget
from .contracts import Limits, SearchResult, SearchTask, identifier, natural
from .controller_contracts import (
    Assessment, ControllerResult, Crawl, CrawlRequest, CrawlResult, Evidence, FocusRequest,
    Judge, JudgeRequest, Judgment, Plan, Planner, RequirementView, ResearchView,
    SearchIntent, SourceAction, SourceView, Review, ReviewDecision, Ranker, RankRequest, RankedSources,
)
from .evidence import EvidenceWriter
from .frontier import Frontier
from .providers import SearchProvider
from .registry import SourceRegistry
from .rough_budget import rough_words


@dataclass(frozen=True)
class _Work:
    kind: str
    request: Any
    deadline: float


def _observe(future):
    if not future.cancelled():
        future.exception()


class ResearchController:
    def __init__(self, *, job_id: str, question: str, requirements: tuple[str, ...],
                 artifact_root: Path, providers: dict[str, SearchProvider],
                 planner: Planner, judge: Judge, crawl: Crawl, limits: Limits | None = None,
                 review: Review | None = None, rank: Ranker | None = None, focus=None,
                 on_page=None, on_results=None, callback_timeout: float = 30.0, max_planner_calls: int = 16,
                 max_no_progress_rounds: int = 2, max_results_per_search: int = 100,
                 rank_while_searching: bool = False, streaming: bool = False):
        identifier(job_id)
        if not isinstance(question, str) or not question.strip():
            raise ValueError('question required')
        if (not isinstance(requirements, tuple) or not requirements
                or any(not isinstance(r, str) or not r.strip() for r in requirements)
                or len(set(requirements)) != len(requirements)):
            raise ValueError('unique nonempty requirements required')
        if (type(callback_timeout) not in (int, float)
                or not math.isfinite(callback_timeout) or callback_timeout <= 0):
            raise ValueError('callback_timeout must be finite and positive')
        for name, value in [('max_planner_calls', max_planner_calls),
                            ('max_no_progress_rounds', max_no_progress_rounds),
                            ('max_results_per_search', max_results_per_search)]:
            natural(value, name, 1)
        self.job_id, self.question = job_id, question
        self.root, self.providers = Path(artifact_root), dict(providers)
        self.planner, self.judge, self.crawl = planner, judge, crawl
        if review is not None and rank is not None:
            raise ValueError('rank and legacy review are mutually exclusive')
        self.review = review
        self.rank = rank
        self.focus = focus  # separate stage after the crawl slot is released
        self.on_page = on_page  # synchronous, nonblocking callback after journal admission
        self.on_results = on_results  # synchronous: [(source, title, url, description)] once a search settles
        # A rank that needs every result (Laya tournament) waits for the whole
        # search wave. Search-order ranking does not: each search's results are
        # crawled as soon as that search returns (search/crawl pipeline).
        self.rank_while_searching = rank_while_searching
        # Streaming queries: the research call is still being written; more
        # searches arrive through add_search() until close_stream().
        self._stream_open = streaming
        self._stream_wake = None
        self._focus_queue = deque()
        self._focus_items = {}  # source_id -> (crawl request, crawl result, addresses)
        self._rank_rejected = set()
        self._rank_failed = set()
        self._ranking_history = []
        self._rank_max_candidates = 0
        self._crawl_words = 0
        self._collection_done = False
        self._review_feedback = None
        self._review_stamp = None
        self._review_history = []
        self.limits = limits or Limits()
        self.callback_timeout = callback_timeout
        self.max_planner_calls = max_planner_calls
        self.max_no_progress_rounds = max_no_progress_rounds
        self.max_results_per_search = max_results_per_search
        self._requirements = {r: RequirementView(r) for r in requirements}
        self._budget = QueryBudget(self.limits.search_budget)
        self._frontier = Frontier()
        self._registry = SourceRegistry(job_id)
        self._tasks = {}
        self._evidence = []
        self._evidence_keys = set()
        self._revision = 0
        self._progress = 0
        self._errors = []
        self._active = {}
        self._search_callbacks = set()  # Resource lifetime may extend past freeze.
        self._judges = deque()
        self._judges_remaining = {}
        self._crawls = deque()
        self._pending = {}  # source -> (originating task, coverage), queued or fetching
        self._crawl_count = 0
        self._planner_calls = 0
        self._last_plan = None
        self._round_tasks = 0
        self._settled = 0
        self._round_progress = 0
        self._no_progress = 0
        self._reason = None
        self._finish = False
        self._drain_deadline = None
        self._started = False
        self._abandoned = 0
        self._cancel_signal = None

    @property
    def pending_search_callbacks(self) -> tuple[asyncio.Task, ...]:
        """Caller-owned cleanup handles, never admitted as post-freeze evidence."""
        return tuple(task for task in self._search_callbacks if not task.done())

    def _source(self, source) -> SourceView:
        return SourceView(source.source_id, source.url, source.title, source.topic_ids,
                          source.status, tuple(d['description'] for d in source.discoveries))

    def _review_signature(self):
        return self._settled, len(self._evidence)

    def _review_ok(self):
        return self.review is None or (self._review_feedback is not None
            and self._review_feedback.needs_more_search is False
            and self._review_stamp == self._review_signature()
            and any(e.kind == 'page_extract' and e.addresses and not e.truncated
                    and e.text.strip() for e in self._evidence))

    def _view(self) -> ResearchView:
        if self.rank is not None:
            gaps = () if self._collection_done else tuple(self._requirements)
        elif self.review is not None:
            # Search-loop gaps, not a certificate of factual completeness.
            gaps = () if self._review_ok() else tuple(self._requirements)
        else:
            gaps = tuple(r.requirement_id for r in self._requirements.values() if not r.supported)
        pending = tuple(r for r in self._requirements
                        if any(r in addresses for _, addresses in self._pending.values()))
        return ResearchView(self.job_id, self.question, self._revision,
            tuple(self._requirements.values()), gaps, pending,
            tuple(r for r in gaps if r not in pending), tuple(self._evidence),
            tuple(self._source(s) for s in self._registry.sources),
            tuple(self._tasks.values()), self._budget.remaining, self._review_feedback)

    def _launch(self, kind, request, callback):
        # Invoking even a synchronous/malformed callback happens inside the worker.
        async def invoke():
            return await callback(request)
        future = asyncio.create_task(invoke(), name=f'research:{self.job_id}:{kind}')
        if kind == 'search':
            self._search_callbacks.add(future)
            future.add_done_callback(self._search_callbacks.discard)
        timeout = self.callback_timeout
        if kind == 'focus':
            # Includes waiting at the daemon-wide gate behind other sessions.
            timeout = max(timeout, 60.0)
        if kind == 'rank':
            # A tournament comprises many bounded choices, not one HTTP call.
            # Cached trees retain their original depth after winners are removed.
            self._rank_max_candidates=max(self._rank_max_candidates,len(request.sources))
            count=self._rank_max_candidates;levels=0;waves=0
            while count>1:
                count=math.ceil(count/3);levels+=1
                waves+=math.ceil(count/self.limits.laya_concurrency)
            steps=max(1,waves)+max(0,request.limit-1)*max(1,levels)
            timeout=max(timeout,min(240.0,(steps+1)*min(5.0,self.callback_timeout)))
        self._active[future] = _Work(kind, request,
            asyncio.get_running_loop().time() + timeout)

    def _count(self, kind):
        return sum(w.kind == kind for w in self._active.values())

    def _signature(self):
        return self._revision, self._settled, tuple(sorted(self._pending))

    def _plan(self):
        self._last_plan = self._signature()
        self._planner_calls += 1
        self._launch('plan', self._view(), self.planner)

    def add_search(self, query, provider):
        """Add a root search while the run is live (streamed tool call)."""
        if not self._stream_open or self._reason or provider not in self.providers:
            return False
        if self._frontier.pending_count >= self._budget.remaining:
            return False
        n = len(self._tasks) + 1
        try:
            task = SearchTask(f'q{n}', f't{n}', query, query, provider, 0, None,
                              tuple(self._view().searchable_gaps))
        except (ValueError, TypeError):
            return False
        added = self._frontier.add(task)
        if added:
            self._tasks[task.task_id] = task
        self._wake_stream()
        return added

    def close_stream(self):
        self._stream_open = False
        self._wake_stream()

    def _wake_stream(self):
        if self._stream_wake is not None and not self._stream_wake.done():
            self._stream_wake.set_result(None)

    def _addresses(self, addresses, allowed):
        return (isinstance(addresses, tuple) and bool(addresses)
                and all(isinstance(a, str) and a in allowed for a in addresses))

    def _assess(self, assessments):
        if not isinstance(assessments, tuple):
            self._errors.append('invalid_assessments')
            return
        evidence = {e.event_id: e for e in self._evidence}
        for assessment in assessments:
            if (not isinstance(assessment, Assessment)
                    or not isinstance(assessment.requirement_id, str)
                    or assessment.requirement_id not in self._requirements
                    or not isinstance(assessment.evidence_ids, tuple)
                    or not isinstance(assessment.contradiction_ids, tuple)):
                self._errors.append('invalid_assessment')
                continue
            refs = assessment.evidence_ids + assessment.contradiction_ids
            if any(not isinstance(e, str) or e not in evidence
                   or not evidence[e].text.strip()
                   or evidence[e].kind == 'crawl_failure'
                   or assessment.requirement_id not in evidence[e].addresses for e in refs):
                self._errors.append('unsupported_evidence_reference')
                continue
            old = self._requirements[assessment.requirement_id]
            # Contradictions are deliberately sticky in v1. A score or later
            # omission cannot erase them; semantic resolution is not implemented.
            contradictions = tuple(dict.fromkeys(old.contradiction_ids + assessment.contradiction_ids))
            updated = RequirementView(assessment.requirement_id,
                                      tuple(dict.fromkeys(assessment.evidence_ids)), contradictions)
            if updated != old:
                self._requirements[assessment.requirement_id] = updated
                self._revision += 1
                self._progress += int(updated.supported and not old.supported)
                self._finish = False

    def _apply_plan(self, request, plan):
        if (not isinstance(plan, Plan) or type(plan.revision) is not int
                or plan.revision != request.revision or plan.revision != self._revision
                or not isinstance(plan.searches, tuple) or type(plan.finish) is not bool):
            self._errors.append('invalid_or_stale_plan')
            return
        if self.review is None and self.rank is None:
            self._assess(plan.assessments)
            self._finish = plan.finish and not self._view().gaps
        elif plan.assessments or plan.finish:
            # Legacy callback fields cannot grant completion in production.
            self._errors.append('planner_completion_authority_ignored')
        if self._finish or self._reason:
            return
        allowed = self._view().searchable_gaps
        # Bound admitted queries by the actual remaining dispatch budget, even
        # if a model generates far more candidates. No filler queries are made.
        for proposal in plan.searches:
            if self._frontier.pending_count >= self._budget.remaining:
                break
            if (not isinstance(proposal, SearchIntent)
                    or not self._addresses(proposal.addresses, allowed)
                    or not isinstance(proposal.provider, str)
                    or proposal.provider not in self.providers
                    or (proposal.parent_task_id is not None and not isinstance(proposal.parent_task_id, str))):
                self._errors.append('invalid_search_intent')
                continue
            parent = self._tasks.get(proposal.parent_task_id)
            if (request.tasks and parent is None) or (not request.tasks and proposal.parent_task_id is not None):
                self._errors.append('invalid_search_parent')
                continue
            n = len(self._tasks) + 1
            try:
                task = SearchTask(f'q{n}', f't{n}', proposal.query, proposal.direction,
                    proposal.provider, parent.depth + 1 if parent else 0,
                    parent.topic_id if parent else None, proposal.addresses)
                if self._frontier.add(task):
                    self._tasks[task.task_id] = task
            except (ValueError, TypeError):
                self._errors.append('invalid_search_intent')
        # Assessment revisions are part of this plan, not a reason to poll it.
        self._last_plan = self._signature()

    async def _append(self, writer, source, *, kind, text, addresses, truncated=False,
                      status='completed', title=None, url=None, topic_ids=None,
                      content_mode='full', source_chars=None, captured_chars=None,
                      capture_limit=None, capture_units='codepoints'):
        key = source.source_id, kind, text, truncated, status, tuple(sorted(set(addresses))), content_mode, source_chars, captured_chars, capture_limit, capture_units
        if key in self._evidence_keys:
            return
        eid = f'e{len(self._evidence) + 1}'
        await writer.append(dict(eventId=eid, sourceId=source.source_id,
            topicIds=list(topic_ids or source.topic_ids), kind=kind,
            url=url or source.url, title=title or source.title, text=text,
            status=status, truncated=truncated, contentMode=content_mode, sourceChars=source_chars,
            capturedChars=captured_chars, captureLimit=capture_limit, captureUnits=capture_units))
        self._evidence_keys.add(key)
        self._evidence.append(Evidence(eid, source.source_id, kind, text, addresses, truncated))
        self._revision += 1
        if kind != 'crawl_failure' and text.strip() and addresses:
            self._progress += 1
        self._finish = False

    async def _apply_judgment(self, writer, request, judgment):
        if (not isinstance(judgment, Judgment) or type(judgment.revision) is not int
                or judgment.revision != request.view.revision or not isinstance(judgment.actions, tuple)):
            self._errors.append('invalid_judgment')
            return
        # Assessments see pre-action evidence only. Stale source actions can be
        # safely revalidated against source authority, coverage and crawl state.
        if self.review is not None:
            if judgment.assessments:
                self._errors.append('judge_assessments_not_allowed')
        elif judgment.revision == self._revision:
            self._assess(judgment.assessments)
        elif judgment.assessments:
            self._errors.append('stale_judgment_assessments')
        sources = {s.source_id for s in request.sources}
        for action in judgment.actions:
            if (not isinstance(action, SourceAction) or not isinstance(action.source_id, str)
                    or action.source_id not in sources
                    or action.action not in (('crawl', 'ignore') if self.review is not None
                                              else ('crawl', 'use_snippet', 'ignore', 'search_more'))):
                self._errors.append('invalid_source_action')
                continue
            if action.action in ('ignore', 'search_more'):
                continue  # Only the planner can create gap-addressing queries.
            if not self._addresses(action.addresses, request.task.addresses):
                self._errors.append('invalid_source_addresses')
                continue
            source = self._registry.get(action.source_id)
            if action.action == 'use_snippet':
                for discovery in source.discoveries:
                    if discovery['taskId'] == request.task.task_id:
                        await self._append(writer, source, kind='search_snippet',
                            text=discovery['description'], addresses=action.addresses,
                            title=discovery['title'], url=discovery['url'],
                            truncated=discovery['truncated'])
            elif source.source_id in self._pending:
                task, addresses = self._pending[source.source_id]
                self._pending[source.source_id] = task, tuple(dict.fromkeys(addresses + action.addresses))
            elif self._crawl_count < self.limits.max_crawls and self._registry.queue_crawl(source.source_id):
                self._crawl_count += 1
                self._pending[source.source_id] = request.task, action.addresses
                self._crawls.append(source.source_id)

    async def _crawl_done(self, writer, request, result=None, error=None):
        source = self._registry.get(request.source_id)
        _, addresses = self._pending.pop(source.source_id)
        valid = (isinstance(result, CrawlResult) and isinstance(result.text, str)
                 and type(result.truncated) is bool and type(result.success) is bool
                 and result.content_mode in ('full', 'temp-wiki')
                 and all(n is None or (type(n) is int and n >= 0) for n in
                         (result.source_chars, result.captured_chars, result.capture_limit))
                 and result.capture_units in ('codepoints','utf16'))
        success = valid and result.success and result.content_mode != 'temp-wiki' and bool(result.text.strip()) and error is None
        self._registry.finish_crawl(source.source_id, success=success)
        if (self.focus is not None and success and not result.truncated
                and result.focus_text is None and result.text.strip()):
            # The crawl slot is already free; the page waits in the focus queue.
            self._focus_items[source.source_id] = (request, result, addresses)
            self._focus_queue.append(source.source_id)
            return
        await self._record_crawl(writer, source, result, addresses, valid, success, error)

    async def _focus_done(self, writer, request, text=None, error=None):
        crawl_request, result, addresses = self._focus_items.pop(request.source_id)
        source = self._registry.get(request.source_id)
        if error is None and isinstance(text, str):
            result = replace(result, focus_text=text)
        else:
            self._errors.append(f'focus:{error or "no_filter_result"}')
        await self._record_crawl(writer, source, result, addresses, True, True, None)

    async def _record_crawl(self, writer, source, result, addresses, valid, success, error):
        if valid and result.text:
            if self.rank is not None:
                # The budget measures what is delivered: filtered text when a
                # focus filter ran, otherwise the full page.
                delivered = (result.focus_text if success and isinstance(result.focus_text, str) else result.text)
                self._crawl_words += rough_words(delivered)
                if self._crawl_words >= self.limits.crawl_word_budget:
                    self._reason = self._reason or 'crawl_word_budget_reached'
            # Preserve captured text even for a partial/failed extraction.
            focused = (success and not result.truncated and isinstance(result.focus_text, str)
                       and bool(result.focus_text.strip()))
            await self._append(writer, source, kind='page_extract',
                text=result.focus_text if focused else result.text,
                addresses=addresses if success and not result.truncated else (), truncated=result.truncated,
                status='completed' if success else 'failed',
                content_mode=result.content_mode, source_chars=result.source_chars,
                captured_chars=result.captured_chars, capture_limit=result.capture_limit,
                capture_units=result.capture_units)
            if self.on_page is not None:
                # Unusable pages report empty text so progressive evidence can
                # release the budget it reserved for them.
                usable = success and not result.truncated and result.content_mode == 'full'
                try:
                    self.on_page(source, result.text if usable else '')
                except Exception:
                    self._errors.append('page_progress_failed')
        if not success:
            await self._append(writer, source, kind='crawl_failure', text='',
                addresses=addresses, status='failed')
            self._errors.append(error or 'crawl_empty_or_failed')
            if self.on_page is not None:
                try:
                    self.on_page(source, '')  # idempotent release of a reservation
                except Exception:
                    self._errors.append('page_progress_failed')

    async def _settle(self, writer, work, result=None, error=None):
        if error:
            self._errors.append(f'{work.kind}:{error}')
        if work.kind == 'plan':
            if error:
                self._reason = self._reason or 'planner_failed'
            else:
                self._apply_plan(work.request, result)
        elif work.kind == 'search':
            task = work.request
            valid = (isinstance(result, list) and len(result) <= self.max_results_per_search
                     and all(isinstance(r, SearchResult) for r in result))
            if not error and not valid:
                self._errors.append('invalid_or_oversized_search_result')
            if not error and valid and result:
                sources = {}
                result_rows = []
                for row in result:
                    source, _ = self._registry.discover(row, task.topic_id, task.task_id, task.provider)
                    sources[source.source_id] = self._source(source)
                    result_rows.append((source.source_id, row.title, row.url, row.description))
                    if self.review is not None or self.rank is not None:
                        # Preserve observations automatically, but do not confuse
                        # an unjudged SERP snippet with applicable answer evidence.
                        await self._append(writer, source, kind='search_snippet',
                            text=row.description, addresses=(), title=row.title,
                            url=row.url, truncated=row.truncated)
                if self.on_results is not None:
                    try:
                        self.on_results(result_rows)
                    except Exception:
                        self._errors.append('results_progress_failed')
                if self.rank is not None:
                    self._frontier.complete(task.task_id)
                    self._settled += 1
                else:
                    groups = [(sid,) for sid in sources] if self.review is not None else [tuple(sources)]
                    self._judges_remaining[task.task_id] = len(groups)
                    self._judges.extend((task, ids) for ids in groups)
            else:
                self._frontier.complete(task.task_id)
                self._settled += 1
        elif work.kind == 'judge':
            if not error:
                await self._apply_judgment(writer, work.request, result)
            task_id = work.request.task.task_id
            if error and self.review is not None:
                self._errors.append('judge_source:' + ','.join(s.source_id for s in work.request.sources) + ':' + error)
            self._judges_remaining[task_id] -= 1
            if not self._judges_remaining[task_id]:
                del self._judges_remaining[task_id]
                self._frontier.complete(task_id)
                self._settled += 1
        elif work.kind == 'rank':
            self._apply_ranking(work.request, result, error)
        elif work.kind == 'review':
            valid = (isinstance(result, ReviewDecision)
                     and type(result.revision) is int
                     and result.revision == work.request.revision == self._revision
                     and (result.needs_more_search is None or type(result.needs_more_search) is bool)
                     and isinstance(result.reason, str) and len(result.reason) <= 256
                     and (result.probability_true is None or
                          (type(result.probability_true) in (int, float)
                           and 0 <= result.probability_true <= 1
                           and math.isfinite(result.probability_true))))
            if error or not valid:
                if not error:
                    self._errors.append('invalid_or_stale_review')
                result = ReviewDecision(self._revision, None, reason=error or 'invalid_or_stale_review')
            self._review_feedback = result
            self._review_history.append(result)
            self._review_stamp = self._review_signature()
            self._revision += 1
            self._finish = False
        elif work.kind == 'focus':
            await self._focus_done(writer, work.request, result, error)
        else:
            await self._crawl_done(writer, work.request, result, error)

    def _apply_ranking(self, request, result, error=None):
        allowed = {s.source_id for s in request.sources}
        valid = (isinstance(result, RankedSources) and type(result.revision) is int
                 and result.revision == request.view.revision
                 and isinstance(result.source_ids, tuple) and isinstance(result.rejected_ids, tuple)
                 and isinstance(result.failed_ids,tuple)
                 and len(result.source_ids) <= request.limit
                 and all(isinstance(s,str) and s in allowed for s in result.source_ids + result.rejected_ids + result.failed_ids)
                 and len(set(result.source_ids + result.rejected_ids + result.failed_ids)) == len(result.source_ids + result.rejected_ids + result.failed_ids)
                 and type(result.comparisons) is int and result.comparisons >= 0
                 and isinstance(result.errors,tuple) and len(result.errors)<=1000
                 and all(isinstance(e,str) and len(e)<=256 for e in result.errors))
        if error or not valid:
            self._errors.append('invalid_or_failed_ranking')
            self._rank_failed.update(allowed)
            self._reason = self._reason or 'ranking_failed'
        else:
            self._ranking_history.append(result)
            self._errors.extend(result.errors)
            self._rank_rejected.update(result.rejected_ids)
            self._rank_failed.update(result.failed_ids)
            if not result.source_ids:
                if result.errors or result.failed_ids:
                    self._reason = self._reason or 'ranking_failed'
                else:
                    self._rank_rejected.update(allowed)
            for sid in result.source_ids:
                source = self._registry.get(sid)
                if source.status != 'seen' or sid in self._rank_rejected:
                    continue
                tasks = [t for t in self._tasks.values() if t.topic_id in source.topic_ids]
                if not tasks:
                    self._errors.append('rank_source_without_origin');continue
                addresses = tuple(dict.fromkeys(a for t in tasks for a in t.addresses))
                if self._registry.queue_crawl(sid):
                    self._crawl_count += 1
                    self._pending[sid] = tasks[0], addresses
                    self._crawls.append(sid)
        self._revision += 1

    def _ranked_terminal(self, reason):
        if reason in ('planner_failed','ranking_failed'):
            return 'failed', reason
        self._collection_done = any(e.kind == 'page_extract' and e.addresses
            and not e.truncated and e.text.strip() for e in self._evidence)
        return ('collected' if self._collection_done else 'incomplete'), reason

    def _rank_candidates(self):
        return tuple(self._source(s) for s in self._registry.sources
            if s.status == 'seen' and s.source_id not in self._rank_rejected
            and s.source_id not in self._rank_failed)

    def _background_rank(self):
        """Keep ranking off the crawl critical path.

        Ranking runs one winner at a time while crawls are in flight, and stops
        once rank_batch_size winners are queued ahead of the crawl workers. The
        PageRanker caches its tournament tree, so each next winner replays only
        one path. Crawlers therefore never wait on a batch-sized ranking round.
        """
        active = [f for f, w in self._active.items() if w.kind == 'rank']
        if self._reason:
            for future in active:
                del self._active[future]
                self._discard(future)
            return
        wave_pending = (self._count('plan') or self._count('search')
                        or (self._frontier.pending_count and self._budget.remaining))
        if (active or (wave_pending and not self.rank_while_searching)
                or len(self._crawls) >= self.limits.rank_batch_size):
            # Whole search wave must settle first: merge/dedup before ranking.
            return
        candidates = self._rank_candidates()
        if candidates:
            self._launch('rank', RankRequest(self._view(), candidates, 1), self.rank)

    async def _ranked_loop(self, writer):
        self._plan()
        while True:
            if self._cancel_signal.done():
                raise asyncio.CancelledError
            await self._dispatch()  # start queued crawls before ranking ahead
            self._background_rank()
            if self._active:
                await self._wait(writer)
                continue
            if self._stream_open and not self._reason:
                # Nothing to do yet, but the agent is still writing queries.
                if self._stream_wake is None or self._stream_wake.done():
                    self._stream_wake = asyncio.get_running_loop().create_future()
                await asyncio.wait({self._stream_wake, self._cancel_signal}, return_when=asyncio.FIRST_COMPLETED)
                if self._cancel_signal.done():
                    raise asyncio.CancelledError
                continue
            if self._reason:
                return self._ranked_terminal(self._reason)
            if self._frontier.pending_count and self._budget.remaining:
                continue
            if len(self._tasks) != self._round_tasks:
                self._no_progress = 0 if self._progress > self._round_progress else self._no_progress + 1
                self._round_tasks, self._round_progress = len(self._tasks), self._progress
            if not self._budget.remaining:
                return self._ranked_terminal('search_budget_exhausted')
            if self._no_progress >= self.max_no_progress_rounds:
                return self._ranked_terminal('no_progress')
            if self._last_plan != self._signature() and self._planner_calls < self.max_planner_calls:
                self._plan()
                continue
            return self._ranked_terminal('planner_limit' if self._planner_calls >= self.max_planner_calls else 'no_new_searches')

    async def _dispatch(self):
        if not self._reason and not self._finish:
            slots = self.limits.search_concurrency - self._count('search')
            for task in self._frontier.take(min(slots, self._budget.remaining)):
                # All global reservations happen in this owner, immediately
                # before task creation, not inside a worker's SearchBatch.run().
                if await self._budget.reserve(task.task_id):
                    self._launch('search', task, self.providers[task.provider].search)
        while self._judges and self._count('judge') < self.limits.laya_concurrency:
            task, ids = self._judges.popleft()
            request = JudgeRequest(self._view(), task,
                tuple(self._source(self._registry.get(sid)) for sid in ids))
            self._launch('judge', request, self.judge)
        if self.rank is not None and self._crawl_words >= self.limits.crawl_word_budget:
            while self._crawls:
                sid = self._crawls.popleft()
                self._registry.drop_queued(sid)
                self._pending.pop(sid)
        while self._focus_queue and self._count('focus') < self.limits.focus_concurrency:
            sid = self._focus_queue.popleft()
            crawl_request, result, _ = self._focus_items[sid]
            source = self._registry.get(sid)
            queries = tuple(dict.fromkeys(t.query for t in self._tasks.values() if t.topic_id in source.topic_ids))
            self._launch('focus', FocusRequest(self.job_id, sid, crawl_request.title, result.text, queries), self.focus)
        while self._crawls and self._count('crawl') < self.limits.crawl_concurrency:
            sid = self._crawls.popleft()
            source = self._registry.get(sid)
            task, addresses = self._pending[sid]
            self._registry.start_crawl(sid)
            request = CrawlRequest(self.job_id, sid, source.url, source.title,
                                   task, source.topic_ids, addresses)
            self._launch('crawl', request, self.crawl)

    def _discard(self, future):
        future.cancel()
        future.add_done_callback(_observe)
        self._abandoned += int(not future.done())

    async def _expire_crawls(self, writer):
        for future, work in list(self._active.items()):
            if work.kind == 'crawl':
                del self._active[future]
                self._discard(future)
                await self._settle(writer, work, error='drain_timeout')
        while self._crawls:
            sid = self._crawls.popleft()
            source = self._registry.get(sid)
            task, addresses = self._pending[sid]
            self._registry.start_crawl(sid)
            await self._crawl_done(writer, CrawlRequest(self.job_id, sid, source.url,
                source.title, task, source.topic_ids, addresses), error='drain_timeout')

    async def _wait(self, writer, *, cancelling=False):
        now = asyncio.get_running_loop().time()
        deadline = min(w.deadline for w in self._active.values())
        if self._drain_deadline is not None:
            deadline = min(deadline, self._drain_deadline)
        waiting = set(self._active)
        if not cancelling:
            waiting.add(self._cancel_signal)
            if self._stream_open:
                if self._stream_wake is None or self._stream_wake.done():
                    self._stream_wake = asyncio.get_running_loop().create_future()
                waiting.add(self._stream_wake)
        done, _ = await asyncio.wait(waiting, timeout=max(0, deadline - now),
                                    return_when=asyncio.FIRST_COMPLETED)
        if not cancelling and self._cancel_signal.done():
            raise asyncio.CancelledError
        # Pop/apply each completed proposal only in this owner. Order is stable
        # for simultaneous events, while genuinely earlier results stream in.
        for future in list(self._active):
            if future not in done:
                continue
            work = self._active.pop(future)
            try:
                result = future.result()
            except asyncio.CancelledError:
                await self._settle(writer, work, error='callback_cancelled')
            except Exception as exc:
                await self._settle(writer, work, error=type(exc).__name__)
            else:
                await self._settle(writer, work, result)
        now = asyncio.get_running_loop().time()
        for future, work in list(self._active.items()):
            if work.deadline <= now:
                del self._active[future]
                self._discard(future)
                await self._settle(writer, work, error='callback_timeout')
        if self._drain_deadline is not None and now >= self._drain_deadline:
            await self._expire_crawls(writer)
            self._drain_deadline = None

    async def _loop(self, writer):
        if self.rank is not None:
            return await self._ranked_loop(writer)
        self._plan()
        while True:
            if self._cancel_signal.done():
                raise asyncio.CancelledError
            await self._dispatch()
            busy = (self._count('search') or self._count('judge') or self._judges
                    or self._count('plan') or self._count('review'))
            if not busy:
                if (self.review is not None and self._settled and not self._pending
                        and self._review_stamp != self._review_signature()):
                    # Round barrier: no review of unfinished crawls and no new
                    # planner round until this full evidence snapshot is judged.
                    self._launch('review', self._view(), self.review)
                    await self._wait(writer)
                    continue
                if len(self._tasks) != self._round_tasks:
                    self._no_progress = 0 if self._progress > self._round_progress else self._no_progress + 1
                    self._round_tasks, self._round_progress = len(self._tasks), self._progress
                if not self._budget.remaining:
                    self._reason = self._reason or 'budget_exhausted'
                elif self._no_progress >= self.max_no_progress_rounds:
                    self._reason = self._reason or 'no_progress'
                if self._pending:
                    if (self._reason or not self._view().searchable_gaps or self._finish):
                        if self._drain_deadline is None:
                            self._drain_deadline = asyncio.get_running_loop().time() + self.limits.drain_timeout
                    elif (self.review is None and self._last_plan != self._signature()
                          and self._planner_calls < self.max_planner_calls):
                        self._plan()
                elif self.review is not None and self._review_ok():
                    return 'collected', 'laya_no_more_search'
                elif self.review is not None and self._reason:
                    return 'failed' if self._reason == 'planner_failed' else 'incomplete', self._reason
                elif self._finish:
                    return 'sufficient', 'requirements_supported'
                elif self._last_plan != self._signature() and self._planner_calls < self.max_planner_calls:
                    self._plan()  # Final semantic evaluation is allowed after budget exhaustion.
                else:
                    return 'failed' if self._reason == 'planner_failed' else 'incomplete', (
                        self._reason or ('planner_limit' if self._planner_calls >= self.max_planner_calls else 'no_progress'))
            if not self._active:
                # A plan may have populated the frontier in the preceding turn.
                if self._frontier.pending_count and not self._reason and self._budget.remaining:
                    continue
                return 'incomplete', self._reason or 'no_progress'
            await self._wait(writer)

    async def _cancel(self, writer):
        self._reason = 'cancelled'
        self._judges.clear()
        for future, work in list(self._active.items()):
            if work.kind != 'crawl':
                del self._active[future]
                self._discard(future)
        # Do not start queued IPC requests after cancellation. Existing requests
        # may deliver useful partial evidence within the one drain deadline.
        self._drain_deadline = asyncio.get_running_loop().time() + self.limits.drain_timeout
        while self._active:
            await self._wait(writer, cancelling=True)
        await self._expire_crawls(writer)

    async def run(self) -> ControllerResult:
        if self._started:
            raise RuntimeError('controller runs once')
        self._started = True
        self._cancel_signal = asyncio.get_running_loop().create_future()
        # Cancellation is an input event, not an interrupt inside journal commit
        # or freeze. Repeated caller cancellation cannot tear owner transactions.
        owner = asyncio.create_task(self._run_owned(), name=f'research-owner:{self.job_id}')
        while True:
            try:
                return await asyncio.shield(owner)
            except asyncio.CancelledError:
                if owner.done():
                    return owner.result()
                if not self._cancel_signal.done():
                    self._cancel_signal.set_result(None)

    async def _run_owned(self) -> ControllerResult:
        async with EvidenceWriter(self.root, self.job_id) as writer:
            try:
                try:
                    status, reason = await self._loop(writer)
                    if self._cancel_signal.done():
                        raise asyncio.CancelledError
                except asyncio.CancelledError:
                    await self._cancel(writer)
                    status, reason = 'cancelled', 'cancelled'
                view = self._view()
                snapshot = await writer.freeze(status=status, reason=reason, gaps=list(view.gaps))
                return ControllerResult(snapshot, self._budget.used, self._budget.attempts,
                    view.sources, view.requirements, view.evidence, tuple(self._errors), self._abandoned,
                    tuple(self._review_history), self._crawl_words, tuple(self._ranking_history))
            finally:
                for future in self._active:
                    self._discard(future)
                self._active.clear()
