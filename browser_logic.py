import math
import os
import re
import shlex
import shutil
import time
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable


class TabLimitError(ValueError):
    pass


@dataclass
class TabActivity:
    target_id: str
    page: object
    session_id: str
    kind: str
    created_at: float
    last_active_at: float


class TabActivityRegistry:
    def __init__(self, max_tabs: int = 20, clock: Callable[[], float] = time.monotonic):
        if max_tabs < 1:
            raise ValueError('max_tabs must be at least 1')
        self.max_tabs = max_tabs
        self.clock = clock
        self._records: dict[str, TabActivity] = {}

    @staticmethod
    def target_id(page: object) -> str:
        target = getattr(page, 'target', None)
        value = getattr(target, 'target_id', None)
        return str(value) if value else f'object-{id(page)}'

    def register(
        self,
        page: object,
        session_id: str,
        kind: str = 'page',
        *,
        last_active_at: float | None = None,
    ) -> TabActivity:
        target_id = self.target_id(page)
        now = self.clock() if last_active_at is None else last_active_at
        existing = self._records.get(target_id)
        record = TabActivity(
            target_id=target_id,
            page=page,
            session_id=session_id,
            kind=kind,
            created_at=existing.created_at if existing else now,
            last_active_at=now,
        )
        self._records[target_id] = record
        return record

    def touch(self, page: object) -> None:
        record = self._records.get(self.target_id(page))
        if record is not None:
            record.last_active_at = self.clock()

    def remove(self, page: object) -> TabActivity | None:
        return self._records.pop(self.target_id(page), None)

    def records(self) -> tuple[TabActivity, ...]:
        return tuple(self._records.values())

    def evictions_for_new_tabs(
        self,
        required: int = 1,
        *,
        protected_sessions: set[str] | None = None,
        protected_target_ids: set[str] | None = None,
    ) -> list[TabActivity]:
        overflow = len(self._records) + required - self.max_tabs
        if overflow <= 0:
            return []
        protected_sessions = protected_sessions or set()
        protected_target_ids = protected_target_ids or set()
        candidates = sorted(
            (
                record for record in self._records.values()
                if record.session_id not in protected_sessions
                and record.target_id not in protected_target_ids
            ),
            key=lambda record: (record.last_active_at, record.created_at, record.target_id),
        )
        if len(candidates) < overflow:
            raise TabLimitError(
                f'TAB_LIMIT: cannot open {required} new tab(s); all {self.max_tabs} tabs are active or protected'
            )
        return candidates[:overflow]


class OpenActionGuard:
    def __init__(self, limit: int = 2):
        self.limit = limit
        self._counts: dict[str, int] = {}

    def check(self, session_id: str, action: str) -> None:
        if action != 'open':
            self._counts.pop(session_id, None)
            return

        count = self._counts.get(session_id, 0) + 1
        self._counts[session_id] = count
        if count > self.limit:
            raise ValueError(
                f'OPEN_LOOP_GUARD: open has run {count} times consecutively in this session and is '
                f'blocked until a non-open browser action runs. Use the current page, crawl URLs in '
                f'one batch, or stop browsing instead of opening more pages.'
            )

    def clear(self, session_id: str) -> None:
        self._counts.pop(session_id, None)


@dataclass(frozen=True)
class VisionFallbackContext:
    target_id: str
    url: str
    loader_id: str


class VisionFallbackGuard:
    def __init__(self, threshold: int = 3):
        if threshold != 3 or isinstance(threshold, bool):
            raise ValueError('vision fallback threshold is fixed at 3')
        self.threshold = threshold
        self._failures: dict[str, tuple[VisionFallbackContext, int]] = {}

    def record_failure(
        self,
        session_id: str,
        context: VisionFallbackContext,
    ) -> tuple[int, bool]:
        previous = self._failures.get(session_id)
        count = previous[1] + 1 if previous and previous[0] == context else 1
        count = min(count, self.threshold)
        self._failures[session_id] = (context, count)
        return count, count >= self.threshold

    def require_unlocked(self, session_id: str, context: VisionFallbackContext) -> None:
        previous = self._failures.get(session_id)
        if previous and previous[0] != context:
            self._failures.pop(session_id, None)
            previous = None
        count = previous[1] if previous else 0
        if count < self.threshold:
            raise ValueError(
                f'VISION_FALLBACK_LOCKED: vision-mark is available only after '
                f'{self.threshold} consecutive semantic target-resolution failures on the current page/document '
                f'({count}/{self.threshold} recorded). Keep using @ref, click-text, click-css, or '
                f'click-js; do not fabricate failures just to unlock coordinate fallback.'
            )

    def observe_context(self, session_id: str, context: VisionFallbackContext) -> None:
        previous = self._failures.get(session_id)
        if previous and previous[0] != context:
            self._failures.pop(session_id, None)

    def reset(self, session_id: str) -> None:
        self._failures.pop(session_id, None)


@dataclass(frozen=True)
class VisionPageState:
    target_id: str
    url: str
    width: int
    height: int
    loader_id: str = ''
    scroll_x: float = 0
    scroll_y: float = 0
    visual_offset_x: float = 0
    visual_offset_y: float = 0
    visual_width: float = 0
    visual_height: float = 0
    visual_scale: float = 1


def map_screenshot_point_to_viewport(
    page: VisionPageState,
    image_width: int,
    image_height: int,
    x: float,
    y: float,
) -> tuple[float, float]:
    dimensions = (
        float(image_width), float(image_height),
        float(page.visual_width), float(page.visual_height),
    )
    if not all(math.isfinite(value) and value > 0 for value in dimensions):
        raise ValueError('vision screenshot and visual viewport dimensions must be finite and positive')
    if not all(math.isfinite(value) for value in (x, y)):
        raise ValueError('vision screenshot coordinates must be finite')
    return (
        x * page.visual_width / image_width,
        y * page.visual_height / image_height,
    )


@dataclass(frozen=True)
class VisionMarker:
    token: str
    x: float
    y: float
    click_x: float
    click_y: float
    image_width: int
    image_height: int
    page: VisionPageState
    image_hash: str
    created_at: float


class VisionCorrectnessGuard:
    def __init__(self, ttl_seconds: float = 30, clock: Callable[[], float] = time.monotonic):
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0 or ttl_seconds > 300:
            raise ValueError('vision preview ttl must be finite and between 0 and 300 seconds')
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._screenshots: dict[str, tuple[VisionPageState, float]] = {}
        self._markers: dict[str, VisionMarker] = {}

    def record_screenshot(self, session_id: str, page: VisionPageState) -> None:
        self._screenshots[session_id] = (page, self.clock())
        self._markers.pop(session_id, None)

    def issue_marker(
        self,
        session_id: str,
        page: VisionPageState,
        x: float,
        y: float,
        token: str,
        image_hash: str,
        image_width: int | None = None,
        image_height: int | None = None,
        click_x: float | None = None,
        click_y: float | None = None,
    ) -> VisionMarker:
        screenshot = self._screenshots.get(session_id)
        if screenshot is None:
            raise ValueError(
                'VISION_SCREENSHOT_REQUIRED: run screenshot and inspect the current viewport image '
                'before placing a click marker'
            )
        screenshot_page, captured_at = screenshot
        if screenshot_page != page:
            self.invalidate(session_id)
            raise ValueError(
                'VISION_SCREENSHOT_REQUIRED: page or viewport changed; take and inspect a fresh screenshot'
            )
        if self.clock() - captured_at > self.ttl_seconds:
            self.invalidate(session_id)
            raise ValueError(
                'VISION_SCREENSHOT_REQUIRED: take and inspect a fresh screenshot because the previous one expired'
            )
        if not all(math.isfinite(value) for value in (x, y)) or x < 0 or y < 0:
            raise ValueError('vision marker coordinates must be finite and non-negative')
        image_width = page.width if image_width is None else image_width
        image_height = page.height if image_height is None else image_height
        click_x = x if click_x is None else click_x
        click_y = y if click_y is None else click_y
        if image_width < 1 or image_height < 1:
            raise ValueError('vision marker screenshot dimensions must be positive')
        if x >= image_width or y >= image_height:
            raise ValueError(
                f'vision marker coordinates ({x:g}, {y:g}) are outside the current '
                f'{image_width}x{image_height} screenshot'
            )
        if not all(math.isfinite(value) for value in (click_x, click_y)):
            raise ValueError('vision click coordinates must be finite')
        if not image_hash:
            raise ValueError('vision marker requires a rendered screenshot hash')
        marker = VisionMarker(
            token, x, y, click_x, click_y, int(image_width), int(image_height),
            page, image_hash, self.clock()
        )
        self._markers[session_id] = marker
        self._screenshots[session_id] = (page, self.clock())
        return marker

    def current_marker(self, session_id: str, token: str) -> VisionMarker:
        marker = self._markers.get(session_id)
        if marker is None or marker.token != token:
            raise ValueError(
                'VISION_CONFIRMATION_REQUIRED: token does not match the current marked preview; '
                'inspect the latest marked screenshot'
            )
        if self.clock() - marker.created_at > self.ttl_seconds:
            self._markers.pop(session_id, None)
            raise ValueError(
                'VISION_PREVIEW_EXPIRED: the marked preview expired; take a fresh screenshot and mark again'
            )
        return marker

    def consume_marker(
        self,
        session_id: str,
        page: VisionPageState,
        token: str,
        image_hash: str,
    ) -> VisionMarker:
        marker = self.current_marker(session_id, token)
        if marker.page != page:
            self._markers.pop(session_id, None)
            raise ValueError(
                'VISION_CONFIRMATION_REQUIRED: page changed after the marked preview; '
                'take a fresh screenshot and mark again'
            )
        if marker.image_hash != image_hash:
            self._markers.pop(session_id, None)
            raise ValueError(
                'VISION_CONFIRMATION_REQUIRED: rendered content changed after the marked preview; '
                'take a fresh screenshot and mark again'
            )
        self._markers.pop(session_id, None)
        return marker

    def invalidate(self, session_id: str) -> None:
        self._screenshots.pop(session_id, None)
        self._markers.pop(session_id, None)


def parse_devtools_active_port(content: str) -> int:
    port = int(content.splitlines()[0])
    if not 1 <= port <= 65535:
        raise ValueError('DevTools port is out of range')
    return port


def should_disable_sandbox() -> bool:
    return os.environ.get('PI_NODRIVER_NO_SANDBOX', '').lower() in {'1', 'true', 'yes'}


def resolve_profile_dir() -> Path:
    configured = os.environ.get('PI_NODRIVER_PROFILE')
    return Path(configured).expanduser() if configured else Path.home() / '.pi' / 'agent' / 'nodriver-profile'


def resolve_browser_executable() -> str:
    configured = os.environ.get('PI_NODRIVER_CHROME')
    if configured:
        return configured
    for command in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'):
        executable = shutil.which(command)
        if executable:
            return executable
    raise RuntimeError('Chrome or Chromium was not found; set PI_NODRIVER_CHROME')


def parse_dismiss_options(parts: list[str]) -> str:
    if parts[:2] != ['dismiss', 'overlays']:
        raise ValueError('usage: dismiss overlays [--cookies=accept|reject-optional|ignore]')
    policy = 'reject-optional'
    for option in parts[2:]:
        if option.startswith('--cookies='):
            policy = option.split('=', 1)[1]
        else:
            raise ValueError(f'unknown dismiss option: {option}')
    if policy not in {'accept', 'reject-optional', 'ignore'}:
        raise ValueError(f'unsupported cookie policy: {policy}')
    return policy


def parse_command(command: str) -> list[str]:
    parts = shlex.split(command)
    if not parts:
        raise ValueError('empty browser command')
    if any(token in {'&&', '||', ';', '|'} for token in parts):
        raise ValueError('run exactly one browser command per tool call; command chaining is not supported')
    return parts


def is_semantic_click_attempt(parts: list[str]) -> bool:
    if not parts:
        return False
    action = parts[0].lower()
    if action in {'click', 'click-js'}:
        return len(parts) == 2 and parts[1].startswith('@') and len(parts[1]) > 1
    if action in {'click-text', 'click-css'}:
        return len(parts) >= 2 and bool(' '.join(parts[1:]).strip())
    return False


def parse_vision_mark(parts: list[str]) -> tuple[float, float]:
    if len(parts) != 3 or parts[0].lower() != 'vision-mark':
        raise ValueError('usage: vision-mark <x> <y>')
    try:
        x, y = float(parts[1]), float(parts[2])
    except ValueError as error:
        raise ValueError('usage: vision-mark <x> <y>') from error
    if not math.isfinite(x) or not math.isfinite(y) or x < 0 or y < 0:
        raise ValueError('vision-mark coordinates must be finite and non-negative')
    return x, y


def parse_vision_click(parts: list[str]) -> str:
    if len(parts) != 2 or parts[0].lower() != 'vision-click':
        raise ValueError('usage: vision-click <preview-token>')
    token = parts[1].lower()
    if not re.fullmatch(r'[a-f0-9]{24}', token):
        raise ValueError('vision-click requires the exact preview token returned by vision-mark')
    return token


def normalize_option_text(value: object) -> str:
    text = unicodedata.normalize('NFKC', str(value or '')).casefold()
    return ' '.join(re.sub(r'[^\w]+', ' ', text, flags=re.UNICODE).split())


def _compact_normalized(value: object) -> str:
    return normalize_option_text(value).replace(' ', '')


def _search_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    for token in value.split():
        parts = re.findall(r'[^\W\d_]+|\d+', token, flags=re.UNICODE)
        tokens.extend(parts or [token])
    return tokens


def _required_model_pairs(tokens: list[str]) -> list[str]:
    return [
        first + second
        for index, (first, second) in enumerate(zip(tokens, tokens[1:]))
        if len(first) >= 2 and first.isalpha() and second.isdigit() and len(second) >= 3
        and (index == 0 or not tokens[index - 1].isdigit())
    ]


def _option_score(option: dict, query: str) -> dict:
    query_normalized = normalize_option_text(query)
    query_compact = query_normalized.replace(' ', '')
    text = str(option.get('text') or '').strip()
    value = str(option.get('value') or '').strip()
    text_normalized = normalize_option_text(text)
    text_compact = text_normalized.replace(' ', '')
    search_normalized = normalize_option_text(option.get('searchText') or text)
    search_compact = search_normalized.replace(' ', '')
    value_normalized = normalize_option_text(value)
    value_compact = value_normalized.replace(' ', '')

    text_score = 0.0
    match_kind = 'fuzzy'
    if query_normalized and text_normalized == query_normalized:
        text_score = 1200.0
        match_kind = 'exact text'
    elif query_compact and text_compact == query_compact:
        text_score = 1180.0
        match_kind = 'exact text'
    elif query_normalized and f' {query_normalized} ' in f' {search_normalized} ':
        density = len(query_compact) / max(1, len(search_compact))
        text_score = 900.0 + 80.0 * density
        match_kind = 'text phrase'
    else:
        query_tokens = _search_tokens(query_normalized)
        candidate_tokens = _search_tokens(search_normalized)
        if query_tokens:
            weights = [max(1, len(token)) * (2 if any(char.isdigit() for char in token) else 1)
                       for token in query_tokens]
            exact_weight = sum(
                weight for token, weight in zip(query_tokens, weights)
                if token in candidate_tokens or (
                    not any(char.isdigit() for char in token) and token in search_compact
                )
            )
            exact_coverage = exact_weight / max(1, sum(weights))
            fuzzy_weight = 0.0
            for token, weight in zip(query_tokens, weights):
                if any(char.isdigit() for char in token):
                    similarity = 1.0 if token in candidate_tokens else 0.0
                else:
                    similarity = max(
                        (SequenceMatcher(None, token, candidate).ratio() for candidate in candidate_tokens),
                        default=0.0,
                    )
                if similarity >= 0.72:
                    fuzzy_weight += weight * similarity
            fuzzy_coverage = fuzzy_weight / max(1, sum(weights))
            coverage = max(exact_coverage, fuzzy_coverage * 0.86)
            alpha_anchors = [
                token for token in query_tokens
                if len(token) >= 2 and any(char.isalpha() for char in token)
            ]
            anchor_matched = not alpha_anchors or any(
                token in candidate_tokens or token in search_compact or (
                    len(token) >= 3 and any(
                        SequenceMatcher(None, token, candidate).ratio() >= 0.82
                        for candidate in candidate_tokens
                        if any(char.isalpha() for char in candidate)
                    )
                )
                for token in alpha_anchors
            )
            model_pairs_match = all(
                pair in search_compact for pair in _required_model_pairs(query_tokens)
            )
            numeric_tokens_match = all(
                token in candidate_tokens for token in query_tokens if token.isdigit()
            )
            if not anchor_matched or not model_pairs_match or not numeric_tokens_match:
                coverage = 0.0
                exact_coverage = 0.0
            if exact_coverage == 1.0:
                text_score = 780.0 + min(70.0, 10.0 * len(query_tokens))
                match_kind = 'all text tokens'
            elif coverage > 0:
                text_score = 620.0 * coverage

    value_score = 0.0
    value_kind = 'value'
    if query_normalized and value_normalized == query_normalized:
        value_score = 800.0
        value_kind = 'exact value'
    elif query_compact and value_compact == query_compact:
        value_score = 780.0
        value_kind = 'exact value'
    elif query_normalized and f' {query_normalized} ' in f' {value_normalized} ':
        value_score = 560.0
        value_kind = 'value phrase'

    score = text_score
    if value_score > text_score:
        score = value_score
        match_kind = value_kind
    return {
        **option,
        'text': text,
        'value': value,
        'score': round(score, 1),
        'matchKind': match_kind,
    }


def rank_option_matches(options: list[dict], query: str, limit: int | None = None) -> list[dict]:
    if not normalize_option_text(query):
        raise ValueError('option query cannot be empty')
    matches = [_option_score(option, query) for option in options if not option.get('disabled')]
    matches = [match for match in matches if match['score'] >= 220.0]
    matches.sort(key=lambda match: -match['score'])
    return matches if limit is None else matches[:limit]


def is_confident_option_match(matches: list[dict], query: str) -> bool:
    if not matches:
        return False
    top = matches[0]
    exact_text_matches = [match for match in matches if match.get('matchKind') == 'exact text']
    if top.get('matchKind') == 'exact text':
        return len(exact_text_matches) == 1
    if top['score'] < 450.0:
        return False
    if len(matches) == 1:
        return True
    runner_up = matches[1]
    return top['score'] - runner_up['score'] >= 80.0


def _quoted(value: str) -> str:
    return value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ')


def _compact(value: str, limit: int) -> str:
    value = value.strip()
    return value if len(value) <= limit else value[:limit] + '…'


def format_snapshot(elements: list[dict]) -> str:
    lines: list[str] = []
    has_select = False
    for item in elements:
        ref = item.get('ref', '')
        tag = item.get('tag', 'element')
        has_select = has_select or tag == 'select'
        text_source = '' if tag == 'select' else item.get('text') or item.get('value') or ''
        text = _compact(str(text_source), 160)
        line = f'@{ref} <{tag}>'
        if text:
            line += f' "{_quoted(text)}"'
        for key, label in (
            ('controlLabel', 'label'),
            ('ariaLabel', 'aria-label'),
            ('placeholder', 'placeholder'),
            ('selected', 'selected'),
            ('frame', 'frame'),
            ('href', 'href'),
            ('download', 'download'),
        ):
            value = _compact(str(item.get(key) or ''), 160)
            if value:
                line += f' {label}="{_quoted(value)}"'
        if tag == 'select' and item.get('optionCount') is not None:
            option_summary = str(item['optionCount'])
            if item.get('optionType'):
                option_summary += f' {item["optionType"]}'
            line += f' options="{_quoted(option_summary)}"'
        lines.append(line)
    if not lines:
        return '(no interactive elements)'
    if has_select:
        lines.append('Dropdown options are searchable without opening them: find-option "keywords", then use the returned select @ref --index=N command.')
    return '\n'.join(lines)
