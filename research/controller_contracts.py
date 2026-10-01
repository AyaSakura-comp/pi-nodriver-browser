"""Immutable, transport-neutral proposals for the single-owner controller.

Callbacks receive copies, never a registry, frontier, budget or writer. Model
bindings must parse their wire output into these contracts. Source text is
untrusted data. Assessments cite journal event IDs, not scores or invented URLs.
A judgment may reuse its source actions after revision revalidation; semantic
assessments and planner output require the exact current evidence revision.
"""
from dataclasses import dataclass
from typing import Awaitable, Callable

from .contracts import SearchTask
from .evidence import Snapshot


@dataclass(frozen=True)
class SearchIntent:
    query: str
    direction: str
    addresses: tuple[str, ...]
    parent_task_id: str | None = None
    provider: str = '4get'


@dataclass(frozen=True)
class Assessment:
    requirement_id: str
    evidence_ids: tuple[str, ...] = ()
    contradiction_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceAction:
    source_id: str
    action: str  # crawl | use_snippet | search_more | ignore
    addresses: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plan:
    revision: int
    searches: tuple[SearchIntent, ...] = ()
    assessments: tuple[Assessment, ...] = ()
    finish: bool = False


@dataclass(frozen=True)
class Judgment:
    revision: int
    actions: tuple[SourceAction, ...] = ()
    assessments: tuple[Assessment, ...] = ()


@dataclass(frozen=True)
class Evidence:
    event_id: str
    source_id: str
    kind: str
    text: str
    addresses: tuple[str, ...]
    truncated: bool = False


@dataclass(frozen=True)
class SourceView:
    source_id: str
    url: str
    title: str
    topic_ids: tuple[str, ...]
    status: str
    descriptions: tuple[str, ...]


@dataclass(frozen=True)
class RequirementView:
    requirement_id: str
    evidence_ids: tuple[str, ...] = ()
    contradiction_ids: tuple[str, ...] = ()

    @property
    def supported(self) -> bool:
        return bool(self.evidence_ids) and not self.contradiction_ids


@dataclass(frozen=True)
class ReviewDecision:
    revision: int
    needs_more_search: bool | None  # None = review failed/unknown; never permission to finish.
    probability_true: float | None = None
    reason: str = ''


@dataclass(frozen=True)
class ResearchView:
    job_id: str
    question: str
    revision: int
    requirements: tuple[RequirementView, ...]
    gaps: tuple[str, ...]
    pending_addresses: tuple[str, ...]
    searchable_gaps: tuple[str, ...]
    evidence: tuple[Evidence, ...]
    sources: tuple[SourceView, ...]
    tasks: tuple[SearchTask, ...]
    budget_remaining: int
    review_feedback: ReviewDecision | None = None


@dataclass(frozen=True)
class JudgeRequest:
    view: ResearchView
    task: SearchTask
    sources: tuple[SourceView, ...]


@dataclass(frozen=True)
class RankRequest:
    view: ResearchView
    sources: tuple[SourceView, ...]
    limit: int


@dataclass(frozen=True)
class RankedSources:
    revision: int
    source_ids: tuple[str, ...] = ()
    rejected_ids: tuple[str, ...] = ()
    comparisons: int = 0
    errors: tuple[str, ...] = ()
    failed_ids: tuple[str, ...] = ()  # classification unknown, not irrelevant


@dataclass(frozen=True)
class CrawlRequest:
    job_id: str
    source_id: str
    url: str  # Exact provider-returned URL; no callback-supplied navigation URLs.
    title: str
    task: SearchTask  # Origin/lineage retained even if the crawl arrives late.
    topic_ids: tuple[str, ...]
    addresses: tuple[str, ...]


@dataclass(frozen=True)
class CrawlResult:
    text: str
    truncated: bool = False
    success: bool = True
    content_mode: str = 'full'
    source_chars: int | None = None
    captured_chars: int | None = None
    capture_limit: int | None = None
    capture_units: str = 'codepoints'
    # Verbatim sentence subset chosen right after the crawl; `text` stays the
    # full page (preserved), while the delivered focus text drives the budget.
    focus_text: str | None = None


@dataclass(frozen=True)
class FocusRequest:
    """One crawled page for the separate focus stage; bound to its job/source."""
    job_id: str
    source_id: str
    title: str
    text: str
    queries: tuple[str, ...] = ()  # the search queries that found this page


@dataclass(frozen=True)
class ControllerResult:
    snapshot: Snapshot
    budget_used: int
    attempts: tuple[str, ...]
    sources: tuple[SourceView, ...]
    requirements: tuple[RequirementView, ...]
    evidence: tuple[Evidence, ...]
    errors: tuple[str, ...]
    abandoned_callbacks: int
    review_history: tuple[ReviewDecision, ...] = ()
    crawl_words: int = 0
    ranking_history: tuple[RankedSources, ...] = ()


Planner = Callable[[ResearchView], Awaitable[Plan]]
Judge = Callable[[JudgeRequest], Awaitable[Judgment]]
Review = Callable[[ResearchView], Awaitable[ReviewDecision]]
Ranker = Callable[[RankRequest], Awaitable[RankedSources]]
Crawl = Callable[[CrawlRequest], Awaitable[CrawlResult]]
Focus = Callable[[FocusRequest], Awaitable[str | None]]
