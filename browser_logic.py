import ipaddress
import json
import math
import os
import re
import shlex
import shutil
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

import idna
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable


GOOGLE_REDIRECT_PATHS = {'/url', '/goto'}
SEARCH_TRACKING_PARAMETERS = {
    'fbclid', 'gclid', 'gbraid', 'msclkid', 'sa', 'source', 'ved', 'wbraid',
}


def parse_google_search_payload(payload: str, limit: int = 4) -> list[dict[str, str]]:
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ValueError('google-search requires a JSON query array') from error

    if isinstance(decoded, dict):
        decoded = decoded.get('searches', [decoded])
    if not isinstance(decoded, list):
        raise ValueError('google-search requires a JSON query array')

    searches: list[dict[str, str]] = []
    seen_queries: set[str] = set()
    for index, item in enumerate(decoded):
        if isinstance(item, str):
            direction = f'方向 {index + 1}'
            query = item
        elif isinstance(item, dict):
            direction = str(item.get('direction') or f'方向 {index + 1}')
            query = str(item.get('query') or '')
        else:
            continue
        direction = ' '.join(direction.split())
        query = ' '.join(query.split())
        key = query.casefold()
        if not query or key in seen_queries:
            continue
        seen_queries.add(key)
        searches.append({'direction': direction, 'query': query})
        if len(searches) >= limit:
            break
    if not searches:
        raise ValueError('google-search requires at least one non-empty query')
    return searches


def _google_redirect_target(url: str) -> tuple[urllib.parse.SplitResult, str] | None:
    try:
        parsed = urllib.parse.urlsplit(str(url or '').strip())
    except ValueError:
        return None
    host = (parsed.hostname or '').lower().removeprefix('www.')
    if not host.startswith('google.') or parsed.path not in GOOGLE_REDIRECT_PATHS:
        return None
    params = urllib.parse.parse_qs(parsed.query)
    return parsed, (params.get('q') or params.get('url') or [''])[0]


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def resolve_google_redirect_url(url: str, timeout: float = 3.0, opener=None) -> str:
    redirect = _google_redirect_target(url)
    if redirect is None:
        return canonicalize_search_url(url)
    parsed, target = redirect
    if target.startswith(('http://', 'https://')):
        return canonicalize_search_url(target)
    if not target:
        return ''

    opener = opener or urllib.request.build_opener(_NoRedirectHandler())
    request_url = urllib.parse.urlunsplit((
        parsed.scheme or 'https',
        'www.google.com',
        parsed.path,
        parsed.query,
        '',
    ))
    request = urllib.request.Request(
        request_url,
        headers={'User-Agent': 'Mozilla/5.0'},
        method='GET',
    )
    response = None
    try:
        response = opener.open(request, timeout=timeout)
        location = response.headers.get('Location', '')
    except urllib.error.HTTPError as error:
        if error.code not in {301, 302, 303, 307, 308}:
            return ''
        location = error.headers.get('Location', '')
    except (OSError, ValueError):
        return ''
    finally:
        close = getattr(response, 'close', None)
        if callable(close):
            close()
    return canonicalize_search_url(urllib.parse.urljoin(str(url), location)) if location else ''


def canonicalize_search_url(url: str) -> str:
    candidate = str(url or '').strip()
    try:
        parsed = urllib.parse.urlsplit(candidate)
    except ValueError:
        return ''

    host = (parsed.hostname or '').lower()
    if host.removeprefix('www.').startswith('google.') and parsed.path in GOOGLE_REDIRECT_PATHS:
        redirect_params = urllib.parse.parse_qs(parsed.query)
        target = (redirect_params.get('q') or redirect_params.get('url') or [''])[0]
        if target.startswith(('http://', 'https://')):
            return canonicalize_search_url(target)
        if target:
            opaque_query = urllib.parse.urlencode({'url': target})
            return urllib.parse.urlunsplit((parsed.scheme.lower(), 'google.com', parsed.path, opaque_query, ''))

    if parsed.scheme not in {'http', 'https'} or not host:
        return ''
    host = host.removeprefix('www.')
    if parsed.port:
        host = f'{host}:{parsed.port}'
    path = parsed.path.rstrip('/') or '/'
    clean_query = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        lower_key = key.lower()
        if lower_key.startswith('utm_') or lower_key in SEARCH_TRACKING_PARAMETERS:
            continue
        clean_query.append((key, value))
    query = urllib.parse.urlencode(clean_query, doseq=True)
    return urllib.parse.urlunsplit((parsed.scheme.lower(), host, path, query, ''))


def select_diverse_search_results(groups: list[dict], limit: int = 10) -> list[dict]:
    selected: list[dict] = []
    selected_by_url: dict[str, dict] = {}
    positions = [0] * len(groups)

    while len(selected) < limit:
        progressed = False
        for group_index, group in enumerate(groups):
            results = group.get('results') or []
            while positions[group_index] < len(results):
                raw = results[positions[group_index]]
                positions[group_index] += 1
                canonical_url = canonicalize_search_url(raw.get('url', ''))
                if not canonical_url:
                    continue
                direction = str(group.get('direction') or f'方向 {group_index + 1}')
                existing = selected_by_url.get(canonical_url)
                if existing is not None:
                    if direction not in existing['directions']:
                        existing['directions'].append(direction)
                    continue
                item = {
                    'title': str(raw.get('title') or 'No Title').strip(),
                    'url': canonical_url,
                    'snippet': str(raw.get('snippet') or '').strip(),
                    'direction': direction,
                    'directions': [direction],
                    'query': str(group.get('query') or '').strip(),
                }
                selected.append(item)
                selected_by_url[canonical_url] = item
                progressed = True
                break
            if len(selected) >= limit:
                break
        if not progressed:
            break
    return selected


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


def normalize_open_url(target_url: str) -> str:
    normalized = str(target_url)
    try:
        parsed = urllib.parse.urlsplit(normalized)
    except (TypeError, ValueError):
        return normalized
    if parsed.scheme.lower() not in {'http', 'https'} or not parsed.hostname:
        return normalized

    host = parsed.hostname.lower()
    replacement_host = None
    for alias, canonical in (
        ('momoshop.tw', 'momoshop.com.tw'),
        ('pchome.tw', 'pchome.com.tw'),
    ):
        if host == alias or host.endswith(f'.{alias}'):
            replacement_host = f'{host[:-len(alias)]}{canonical}'
            break
    if replacement_host is not None:
        netloc = parsed.netloc
        host_index = netloc.lower().rfind(host)
        if host_index >= 0:
            netloc = (
                f'{netloc[:host_index]}{replacement_host}'
                f'{netloc[host_index + len(host):]}'
            )
            parsed = parsed._replace(netloc=netloc)
            normalized = urllib.parse.urlunsplit(parsed)
            host = replacement_host

    if (
        (host == 'momoshop.com.tw' or host.endswith('.momoshop.com.tw'))
        and parsed.path == '/mymomo/login.momo'
    ):
        return 'https://account.momoshop.com.tw/mobile'
    return normalized


class OpenActionGuard:
    def __init__(self, limit: int = 2):
        self.limit = limit
        self._counts: dict[str, tuple[str, int]] = {}
        self._failed_counts: dict[str, dict[str, int]] = {}

    @staticmethod
    def _ipv4_number(piece: str) -> int | None:
        base = 10
        digits = piece
        if len(digits) >= 2 and digits[:2].lower() == '0x':
            base, digits = 16, digits[2:]
        elif len(digits) >= 2 and digits.startswith('0'):
            base, digits = 8, digits[1:]
        if not digits:
            return 0
        allowed = {
            8: r'[0-7]+',
            10: r'[0-9]+',
            16: r'[0-9a-fA-F]+',
        }[base]
        if not re.fullmatch(allowed, digits):
            return None
        return int(digits, base)

    @classmethod
    def _canonical_ipv4(cls, host: str) -> str | None:
        pieces = host.split('.')
        if pieces and pieces[-1] == '':
            pieces.pop()
        if not pieces or len(pieces) > 4 or any(piece == '' for piece in pieces):
            return None
        numbers = [cls._ipv4_number(piece) for piece in pieces]
        if any(number is None for number in numbers):
            return None
        values = [int(number) for number in numbers]
        if any(number > 255 for number in values[:-1]):
            return None
        last_limit = 256 ** (5 - len(values))
        if values[-1] >= last_limit:
            return None
        numeric = values[-1]
        for index, number in enumerate(values[:-1]):
            numeric += number * (256 ** (3 - index))
        return str(ipaddress.IPv4Address(numeric))

    @staticmethod
    def _canonical_ipv6(host: str) -> str:
        value = int(ipaddress.IPv6Address(host))
        pieces = [(value >> (16 * (7 - index))) & 0xFFFF for index in range(8)]
        best_start = -1
        best_length = 0
        index = 0
        while index < len(pieces):
            if pieces[index] != 0:
                index += 1
                continue
            end = index
            while end < len(pieces) and pieces[end] == 0:
                end += 1
            length = end - index
            if length > best_length:
                best_start, best_length = index, length
            index = end
        rendered = [format(piece, 'x') for piece in pieces]
        if best_length < 2:
            return ':'.join(rendered)
        left = ':'.join(rendered[:best_start])
        right = ':'.join(rendered[best_start + best_length:])
        if left and right:
            return f'{left}::{right}'
        if left:
            return f'{left}::'
        if right:
            return f'::{right}'
        return '::'

    @staticmethod
    def _strip_chromium_ignored_host_characters(host: str) -> str:
        ignored_singletons = {0x00AD, 0x034F, 0x200B, 0x3164, 0xFEFF, 0xFFA0}
        ignored_ranges = (
            (0x115F, 0x1160),
            (0x17B4, 0x17B5),
            (0x180B, 0x180E),
            (0x2060, 0x2064),
            (0x206A, 0x206F),
            (0xFE00, 0xFE0F),
            (0x1BCA0, 0x1BCA3),
            (0x1D173, 0x1D17A),
            (0xE0100, 0xE01EF),
        )
        return ''.join(
            character for character in host
            if ord(character) not in ignored_singletons and not any(
                start <= ord(character) <= end for start, end in ignored_ranges
            )
        )

    @classmethod
    def _canonical_host(cls, host: str) -> str:
        decoded = urllib.parse.unquote(host, encoding='utf-8', errors='strict')
        lowered = cls._strip_chromium_ignored_host_characters(decoded).lower()
        if ':' in lowered:
            try:
                return cls._canonical_ipv6(lowered)
            except ipaddress.AddressValueError:
                pass
        remapped = idna.uts46_remap(
            lowered,
            std3_rules=False,
            transitional=False,
        )
        ipv4 = cls._canonical_ipv4(remapped)
        if ipv4 is not None:
            return ipv4
        if remapped.isascii():
            return remapped
        try:
            return idna.encode(
                remapped,
                uts46=True,
                transitional=False,
                std3_rules=True,
            ).decode('ascii').lower()
        except idna.IDNAError:
            return '.'.join(
                label if label.isascii() else f'xn--{label.encode("punycode").decode("ascii")}'
                for label in remapped.split('.')
            ).lower()

    @classmethod
    def origin(cls, target_url: str | None) -> str:
        if not target_url:
            return '<unknown>'
        try:
            cleaned_url = re.sub(r'[\t\n\r]', '', str(target_url)).strip(
                ''.join(chr(value) for value in range(33))
            )
            normalized_url = normalize_open_url(cleaned_url)
            scheme, separator, remainder = normalized_url.partition(':')
            scheme = scheme.lower()
            special_schemes = {'ftp', 'http', 'https', 'ws', 'wss'}
            if separator and scheme == 'blob':
                inner_scheme = remainder.partition(':')[0].lower()
                return cls.origin(remainder) if inner_scheme in {'http', 'https'} else 'null'
            if separator and scheme in special_schemes:
                remainder = remainder.lstrip('/\\').replace('\\', '/')
                normalized_url = f'{scheme}://{remainder}'
            parsed = urllib.parse.urlsplit(normalized_url)
            parsed_scheme = parsed.scheme.lower()
            if parsed_scheme and parsed_scheme not in special_schemes:
                return 'null'
            raw_host = parsed.hostname or ''
            if not parsed_scheme or not raw_host:
                return target_url
            host = cls._canonical_host(raw_host)
            port = parsed.port
            default_ports = {'ftp': 21, 'http': 80, 'https': 443, 'ws': 80, 'wss': 443}
            default_port = port == default_ports.get(parsed.scheme.lower())
            display_host = f'[{host}]' if ':' in host else host
            authority = display_host if port is None or default_port else f'{display_host}:{port}'
            return f'{parsed.scheme.lower()}://{authority}'
        except (TypeError, ValueError, UnicodeError, idna.IDNAError):
            return str(target_url)

    def pending_open(self, session_id: str, target_url: str | None = None) -> tuple[str, int]:
        origin = self.origin(target_url)
        previous_origin, previous_count = self._counts.get(session_id, ('', 0))
        successful_count = previous_count if previous_origin == origin else 0
        failed_count = self._failed_counts.get(session_id, {}).get(origin, 0)
        count = successful_count + failed_count + 1
        if count > self.limit:
            blocked_count = count
            raise ValueError(
                f'OPEN_LOOP_GUARD: open has targeted the same origin {origin} {blocked_count} times '
                f'consecutively and is blocked until a non-open browser action or a different-origin '
                f'open runs. Use the current page, crawl same-site URLs in one batch, or stop browsing.'
            )
        return origin, count

    def record_failure(self, session_id: str, target_url: str | None = None) -> None:
        origin = self.origin(target_url)
        counts = self._failed_counts.setdefault(session_id, {})
        counts[origin] = counts.get(origin, 0) + 1

    def check(self, session_id: str, action: str, target_url: str | None = None) -> None:
        if action != 'open':
            self.clear(session_id)
            return
        self._counts[session_id] = self.pending_open(session_id, target_url)
        self._failed_counts.pop(session_id, None)

    def clear(self, session_id: str) -> None:
        self._counts.pop(session_id, None)
        self._failed_counts.pop(session_id, None)


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
        if os.environ.get('PI_NODRIVER_ALLOW_DIRECT_VISION', '1') == '1' or os.environ.get('PI_NODRIVER_VISION_ONLY', '0') == '1':
            return
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


def parse_vision_mark_drag(parts: list[str]) -> tuple[float, float, float, float]:
    if len(parts) != 5:
        raise ValueError('usage: vision-mark-drag <start_x> <start_y> <end_x> <end_y>')
    try:
        x1, y1, x2, y2 = float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])
    except ValueError as err:
        raise ValueError('vision drag coordinates must be numeric') from err
    return x1, y1, x2, y2


def parse_long_press(parts: list[str]) -> tuple[str, int]:
    if len(parts) not in (2, 3) or not parts[1].startswith('@'):
        raise ValueError('usage: long-press @ref [duration_ms]')
    duration_ms = 1000
    if len(parts) == 3:
        try:
            duration_ms = int(parts[2])
            if duration_ms <= 0:
                raise ValueError()
        except ValueError as err:
            raise ValueError('duration_ms must be a positive integer in milliseconds') from err
    return parts[1], duration_ms


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
    is_drag: bool = False
    end_x: float = 0.0
    end_y: float = 0.0
    click_end_x: float = 0.0
    click_end_y: float = 0.0


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
            token=token, x=x, y=y, click_x=click_x, click_y=click_y,
            image_width=int(image_width), image_height=int(image_height),
            page=page, image_hash=image_hash, created_at=self.clock()
        )
        self._markers[session_id] = marker
        self._screenshots[session_id] = (page, self.clock())
        return marker

    def issue_drag_marker(
        self,
        session_id: str,
        page: VisionPageState,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        token: str,
        image_hash: str,
        image_width: int | None = None,
        image_height: int | None = None,
        click_x1: float | None = None,
        click_y1: float | None = None,
        click_x2: float | None = None,
        click_y2: float | None = None,
    ) -> VisionMarker:
        screenshot = self._screenshots.get(session_id)
        if screenshot is None:
            raise ValueError(
                'VISION_SCREENSHOT_REQUIRED: run screenshot and inspect the current viewport image '
                'before placing a drag marker'
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
        for val in (x1, y1, x2, y2):
            if not math.isfinite(val) or val < 0:
                raise ValueError('vision drag coordinates must be finite and non-negative')
        image_width = page.width if image_width is None else image_width
        image_height = page.height if image_height is None else image_height
        click_x1 = x1 if click_x1 is None else click_x1
        click_y1 = y1 if click_y1 is None else click_y1
        click_x2 = x2 if click_x2 is None else click_x2
        click_y2 = y2 if click_y2 is None else click_y2
        if image_width < 1 or image_height < 1:
            raise ValueError('vision marker screenshot dimensions must be positive')
        if x1 >= image_width or y1 >= image_height or x2 >= image_width or y2 >= image_height:
            raise ValueError('vision drag coordinates are outside the current screenshot')
        if not all(math.isfinite(val) for val in (click_x1, click_y1, click_x2, click_y2)):
            raise ValueError('vision drag coordinates must be finite')
        if not image_hash:
            raise ValueError('vision marker requires a rendered screenshot hash')
        marker = VisionMarker(
            token=token, x=x1, y=y1, click_x=click_x1, click_y=click_y1,
            image_width=int(image_width), image_height=int(image_height),
            page=page, image_hash=image_hash, created_at=self.clock(),
            is_drag=True, end_x=x2, end_y=y2, click_end_x=click_x2, click_end_y=click_y2
        )
        self._markers[session_id] = marker
        self._screenshots[session_id] = (page, self.clock())
        return marker

    def current_marker(self, session_id: str, token: str | None = None) -> VisionMarker:
        marker = self._markers.get(session_id)
        if marker is None or (token and token != 'latest' and marker.token != token):
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
            if os.environ.get('PI_NODRIVER_XVFB_FORWARD_CLICK', '1') != '1' and os.environ.get('PI_NODRIVER_ALLOW_DIRECT_VISION', '1') != '1':
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


REF_FIRST_ARGUMENT_ACTIONS = {
    'click', 'click-js', 'download', 'download-info', 'fill', 'fill-submit',
    'fill_submit', 'select', 'type', 'upload',
}


def _normalize_legacy_ref_token(token: str) -> str:
    match = re.fullmatch(r'<@e(\d+)>', token, flags=re.IGNORECASE)
    return f'@e{match.group(1)}' if match else token


def parse_command(command: str) -> list[str]:
    parts = shlex.split(command)
    if not parts:
        raise ValueError('empty browser command')
    if any(token in {'&&', '||', ';', '|'} for token in parts):
        raise ValueError('run exactly one browser command per tool call; command chaining is not supported')

    action = parts[0].lower()
    if action in REF_FIRST_ARGUMENT_ACTIONS and len(parts) > 1:
        parts[1] = _normalize_legacy_ref_token(parts[1])
    elif action == 'get' and len(parts) > 2:
        parts[2] = _normalize_legacy_ref_token(parts[2])
    return parts


def is_semantic_click_attempt(parts: list[str]) -> bool:
    if not parts:
        return False
    action = parts[0].lower()
    if action in {'click', 'click-js', 'long-press', 'longpress', 'press-hold'}:
        return len(parts) in (2, 3) and parts[1].startswith('@') and len(parts[1]) > 1
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
        control_type = _compact(str(item.get('controlType') or ''), 40)
        if control_type:
            attribute = 'control' if tag == 'label' else 'type'
            line += f' {attribute}="{_quoted(control_type)}"'
        for state, label in (
            ('checked', 'checked'),
            ('required', 'required'),
            ('disabled', 'disabled'),
            ('valueSet', 'value-set'),
        ):
            if item.get(state) is not None:
                line += f' {label}="{str(bool(item[state])).lower()}"'
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
