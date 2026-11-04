"""Shared browser worker for Pi: one Chrome, one Unix socket, many Pi sessions.

Protocol (unchanged from the previous worker): each request is one JSON line
{"id", "command", "sessionId", "research"?}; each reply is one line
"__PI_NODRIVER__" + JSON with the same id. {"cancelId"} cancels a request and
{"type": "planner_reply"} answers a research planner request.

Design rules:
- Every CDP call has a timeout (pibrowser.cdp); nothing can hang forever.
- Liveness is page-level: a fresh tab must run JavaScript (Chrome.probe).
  A failed probe restarts Chrome; idle Chrome is probed in the background,
  so a browser wedged while nobody used it is replaced before the next request.
- One tab per Pi session (plus popups it opened); crawl/search tabs are
  temporary and always closed; idle session tabs are closed after 30 minutes.
"""
import asyncio
import fcntl
import json
import os
import re
import shlex
import signal
import sys
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from pibrowser.cdp import CDPClosed, CDPError, CDPTimeout  # noqa: E402
from pibrowser.chrome import Chrome  # noqa: E402
from pibrowser.fetch import FetchError, fetch_image, pdf_text, safe_name  # noqa: E402
from pibrowser.page import PageError, StaleRef, Tab  # noqa: E402
from research.capture import research_capture_js  # noqa: E402

MARKER = '__PI_NODRIVER__'
AGENT_DIR = Path(os.environ.get('PI_AGENT_DIR', Path.home() / '.pi' / 'agent'))
PROFILE = Path(os.environ.get('PI_NODRIVER_PROFILE', AGENT_DIR / 'nodriver-profile')).expanduser()
DOWNLOADS = Path(os.environ.get('PI_NODRIVER_DOWNLOADS', AGENT_DIR / 'nodriver-downloads')).expanduser()
EVENTS = AGENT_DIR / 'nodriver-browser-events.jsonl'
CHROME_LOG = AGENT_DIR / 'nodriver-chrome.log'
COMMAND_TIMEOUT = float(os.environ.get('PI_NODRIVER_COMMAND_TIMEOUT', '75'))
# Research crawls 32 pages at once (RESEARCH_CRAWL_CONCURRENCY); the old pool allowed 48.
CRAWL_TABS = int(os.environ.get('PI_BROWSER_CRAWL_TABS', os.environ.get('PI_NODRIVER_CRAWL_POOL_TABS', '48')))
MAX_SESSION_TABS = int(os.environ.get('PI_NODRIVER_MAX_TABS', '12'))
IDLE_TAB_SECONDS = float(os.environ.get('PI_BROWSER_IDLE_TAB_SECONDS', '1800'))
HEALTH_INTERVAL = float(os.environ.get('PI_BROWSER_HEALTH_INTERVAL', '60'))
COMMAND_PROBE_AGE = 30.0
SLOW_PROBE_SECONDS = 20.0
IDLE_PROBE_AGE = float(os.environ.get('PI_BROWSER_IDLE_PROBE_SECONDS', '300'))
MAX_PAGE_TARGETS = 40
JS_DIR = HERE / 'pibrowser' / 'js'
GOOGLE_RESULTS_JS = (JS_DIR / 'google_results.js').read_text()
IMAGE_CANDIDATES_JS = (JS_DIR / 'image_candidates.js').read_text()
CHALLENGE_MARKERS = ('just a moment', 'attention required', 'verify you are human', 'checking your browser',
                     'enable javascript and cookies to continue', 'challenge validation')
CHALLENGE_TITLE = re.compile(r'just a moment|請稍候|attention required|checking your browser|one more step', re.I)
GOOGLE_BLOCK_MARKERS = ('unusual traffic', 'before you continue to google', 'verify you are human')


def log_event(event, **fields):
    record = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'event': event, **fields}
    print(json.dumps(record, ensure_ascii=False), file=sys.stderr, flush=True)
    try:
        if EVENTS.exists() and EVENTS.stat().st_size > 1_000_000:
            EVENTS.replace(EVENTS.with_suffix('.jsonl.1'))
        with EVENTS.open('a') as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + '\n')
    except OSError:
        pass


def parse_ref(value):
    match = re.fullmatch(r'@?(e\d+)', value.strip(), re.I)
    if not match:
        raise ValueError(f'not a snapshot ref: {value!r} (expected e12 from the latest snapshot)')
    return match.group(1).lower()


def format_snapshot(snap, *, status=0, dialogs=(), note=''):
    scroll = ''
    if snap.get('scrollHeight'):
        scroll = f"Scroll: {snap.get('scrollY', 0)}/{snap['scrollHeight']}px (viewport {snap.get('viewportHeight', 0)}px)"
    header = [f"URL: {snap.get('url', '')}", f"Title: {snap.get('title', '')}"]
    if status and status >= 400:
        header.append(f'HTTP status: {status} (error page)')
    if scroll:
        header.append(scroll)
    for dialog in dialogs:
        header.append(f'Dialog closed automatically: {dialog}')
    if note:
        header.append(note)
    body = snap.get('text') or '(no visible text)'
    tail = '\n(… snapshot truncated; scroll down for more)' if snap.get('truncated') else ''
    return '\n'.join(header) + '\n---\n' + body + tail


def is_challenge(title, text):
    lower = f'{title}\n{text[:3000]}'.lower()
    return any(marker in lower for marker in CHALLENGE_MARKERS)


class SessionState:
    def __init__(self):
        self.stack = []  # tabs; the last one is current (popups stack on their opener)
        self.last_url = None
        self.last_used = time.monotonic()


class BrowserService:
    """Owns Chrome. Also the research engine (research.jobs.ResearchConnection uses
    ensure_browser, search_one, crawl_one, run_fetch_image, image_fetch_semaphore,
    cleanup_research_owner, research_cleanup_tasks and .browser)."""

    def __init__(self):
        self.chrome = None
        self.lifecycle = asyncio.Lock()
        self.verified_at = float('-inf')
        self.sessions = {}
        self.owned = set()
        self.temp_tabs = {}
        self.crawl_slots = asyncio.Semaphore(CRAWL_TABS)
        self.image_fetch_semaphore = asyncio.Semaphore(4)
        self.research_cleanup_tasks = set()
        self.last_activity = time.monotonic()
        self.restarts = 0

    # ---- lifecycle -------------------------------------------------------
    @property
    def browser(self):
        return self.chrome

    async def ensure_browser(self, max_age=None):
        if max_age is not None and self._fresh(max_age):
            return self.chrome
        async with self.lifecycle:
            if max_age is not None and self._fresh(max_age):
                return self.chrome
            if self.chrome is not None:
                started = time.monotonic()
                healthy = await self.chrome.probe()
                if not healthy and self.chrome.alive():
                    # A loaded machine (a model loading, a CPU-bound job) can make a working
                    # Chrome miss 5 s; a restart would close every conversation's window.
                    # A wedged Chrome fails the long probe too.
                    healthy = await self.chrome.probe(timeout=SLOW_PROBE_SECONDS)
                    if healthy:
                        log_event('probe_slow', ms=round((time.monotonic() - started) * 1000))
                if healthy:
                    self.verified_at = time.monotonic()
                    return self.chrome
                log_event('probe_failed', alive=self.chrome.alive(),
                          ms=round((time.monotonic() - started) * 1000))
            await self._restart_locked()
            return self.chrome

    def _fresh(self, max_age):
        return (self.chrome is not None and self.chrome.alive()
                and time.monotonic() - self.verified_at < max_age)

    async def _restart_locked(self):
        old = self.chrome
        if old is not None:
            self.restarts += 1
            await old.stop()
        for state in self.sessions.values():
            state.stack.clear()
        self.owned.clear()
        self.temp_tabs.clear()
        chrome = Chrome(PROFILE, log_path=CHROME_LOG)
        await chrome.start()
        if not await chrome.probe(timeout=10):
            await chrome.stop()
            raise RuntimeError('Chrome started but a new tab cannot run JavaScript')
        self.chrome = chrome
        self.verified_at = time.monotonic()
        log_event('browser_started' if old is None else 'browser_restarted', pid=chrome.proc.pid, port=chrome.port)

    async def shutdown_browser(self):
        async with self.lifecycle:
            if self.chrome is not None:
                await self.chrome.stop()
                self.chrome = None
            self.sessions.clear()
            self.owned.clear()
            self.temp_tabs.clear()

    async def health_loop(self):
        """Background: replace a wedged Chrome before anyone needs it; close idle and leaked tabs."""
        while True:
            await asyncio.sleep(HEALTH_INTERVAL)
            try:
                if self.chrome is None:
                    continue
                if time.monotonic() - self.last_activity > 20:
                    await self.ensure_browser(max_age=IDLE_PROBE_AGE)
                await self.close_idle_tabs()
                await self.sweep_targets()
            except Exception as error:
                log_event('health_error', error=f'{type(error).__name__}: {error}')

    async def close_idle_tabs(self):
        now = time.monotonic()
        for session_id, state in list(self.sessions.items()):
            if now - state.last_used > IDLE_TAB_SECONDS:
                await self.close_session(session_id)
                self.sessions.pop(session_id, None)

    async def sweep_targets(self):
        """Close temp tabs nobody is using and, as a safety valve, the oldest foreign tabs
        once Chrome holds more than MAX_PAGE_TARGETS pages (e.g. leaked by another script)."""
        chrome = self.chrome
        if chrome is None:
            return
        await self.sweep_crawl_pool()
        pages = chrome.pages()
        if len(pages) > MAX_PAGE_TARGETS:
            foreign = sorted((p for p in pages if p['targetId'] not in self.owned), key=lambda p: p['_seen'])
            for info in foreign[:len(pages) - MAX_PAGE_TARGETS]:
                await chrome.close_target(info['targetId'])
            log_event('foreign_tabs_closed', count=min(len(foreign), len(pages) - MAX_PAGE_TARGETS))

    # ---- session tabs ----------------------------------------------------
    def _live(self, tab):
        return tab.chrome is self.chrome and tab.target_id in self.chrome.targets

    async def session_tab(self, session_id, *, create=False):
        state = self.sessions.setdefault(session_id, SessionState())
        state.last_used = time.monotonic()
        state.stack = [tab for tab in state.stack if self._live(tab)]
        if state.stack:
            return state.stack[-1]
        if not create:
            if state.last_url:
                # The tab died (browser restart, closed by the page): bring the page back.
                tab = await self._new_session_tab(session_id, state)
                await tab.goto(state.last_url)
                await tab.wait_ready(10)
                return tab
            raise PageError('no page is open in this session; open a URL first')
        return await self._new_session_tab(session_id, state)

    async def _new_session_tab(self, session_id, state):
        await self._evict_session_tabs(keep=session_id)
        tab = await self.chrome.new_tab(new_window=True)
        self.owned.add(tab.target_id)
        state.stack.append(tab)
        return tab

    async def _evict_session_tabs(self, keep):
        live = [(sid, st) for sid, st in self.sessions.items() if sid != keep and st.stack]
        excess = sum(len(st.stack) for _, st in live) + 1 - MAX_SESSION_TABS
        for session_id, _state in sorted(live, key=lambda item: item[1].last_used):
            if excess <= 0:
                break
            excess -= len(self.sessions[session_id].stack)
            await self.close_session(session_id)

    async def close_session(self, session_id):
        state = self.sessions.get(session_id)
        if not state:
            return 0
        tabs, state.stack = state.stack, []
        for tab in tabs:
            self.owned.discard(tab.target_id)
            await tab.close()
        return len(tabs)

    def _new_popup(self, opener_id, since):
        candidates = [info for info in self.chrome.pages()
                      if info['targetId'] not in self.owned and info['_seen'] >= since]
        preferred = [info for info in candidates if info.get('openerId') == opener_id]
        return (preferred or candidates or [None])[0]

    async def _settle_after_action(self, session_id, tab, started):
        """After a click/submit: adopt a popup it opened, then wait for the page to settle."""
        state = self.sessions[session_id]
        note = ''
        for delay in (0.35, 0.6):
            await asyncio.sleep(delay)
            popup = self._new_popup(tab.target_id, started)
            if popup is not None:
                new_tab = await Tab.attach(self.chrome, popup['targetId'])
                self.owned.add(new_tab.target_id)
                state.stack.append(new_tab)
                if len(state.stack) > 3:
                    oldest = state.stack.pop(0)
                    self.owned.discard(oldest.target_id)
                    await oldest.close()
                await self.chrome.conn.send('Target.activateTarget', {'targetId': new_tab.target_id}, timeout=3)
                tab = new_tab
                note = 'The action opened a new tab; you are now on it (back returns to the previous tab).'
                break
        await tab.wait_ready(8, quiet=0.6, blank_ok=not note)
        return tab, note

    async def page_result(self, session_id, tab, *, action, note='', status=None, from_viewport=False):
        if status is None:
            status = await tab.http_status()
        snap = await tab.snapshot(from_viewport=from_viewport)
        state = self.sessions[session_id]
        state.last_url = snap.get('url') or state.last_url
        dialogs, tab.dialogs = tab.dialogs, []
        text = format_snapshot(snap, status=status, dialogs=dialogs, note=note)
        return {'action': action, 'url': snap.get('url'), 'title': snap.get('title'), 'httpStatus': status,
                'elements': snap.get('elements'), 'text': text, 'snapshot': text}

    # ---- commands --------------------------------------------------------
    async def execute(self, command, session_id='default'):
        command = command.strip()
        if not command:
            raise ValueError('empty command')
        action = command.split(None, 1)[0].lower()
        # crawl/google-search/eval carry raw JSON or JavaScript: never shell-split them.
        args = [] if action in ('crawl', 'google-search', 'eval') else shlex.split(command)[1:]
        self.last_activity = time.monotonic()
        if action == 'browser-health':
            return await self.browser_health()
        if action == 'shutdown':
            await self.shutdown_browser()
            return {'action': action, 'text': 'Browser daemon shutting down'}
        if action == 'session-cleanup':
            closed = await self.close_session(session_id) if self.chrome else 0
            self.sessions.pop(session_id, None)
            return {'action': action, 'text': f'closed {closed} tab(s)'}
        if action == 'close':
            return await self.close_current(session_id)
        if action == 'fetch-image':
            if len(args) != 1:
                raise ValueError('usage: fetch-image <http(s)://image-url>')
            async with self.image_fetch_semaphore:
                path, mime, width, height, final_url = await self.run_fetch_image(args[0], session_id)
            return {'action': action, 'imagePath': str(path), 'mimeType': mime, 'width': width,
                    'height': height, 'size': path.stat().st_size, 'url': final_url,
                    'text': (f'Image fetched\nType: {mime}\nDimensions: {width}x{height}\nPath: {path}\n'
                             f'To send this image to the user, include exactly: [[image: {path}]]')}
        await self.ensure_browser(max_age=COMMAND_PROBE_AGE)
        try:
            return await self._page_command(action, args, command, session_id)
        except CDPTimeout as error:
            # A page that does not answer: slow page, or a wedged browser. Probe now;
            # a dead browser is restarted so the next command works.
            before = self.chrome
            await self.ensure_browser()
            if self.chrome is not before:
                log_event('restarted_after_timeout', action=action, error=str(error))
                if action in ('open', 'snapshot', 'get', 'screenshot', 'crawl', 'google-search'):
                    return await self._page_command(action, args, command, session_id)
                raise PageError(f'the browser stopped responding and was restarted ({error}); '
                                'the page was reloaded, take a new snapshot') from None
            raise

    async def _page_command(self, action, args, command, session_id):
        if action == 'crawl':
            return await self.crawl_command(command[len('crawl'):], session_id)
        if action == 'google-search':
            return await self.google_search_command(command[len('google-search'):], session_id)
        if action == 'open':
            if len(args) != 1:
                raise ValueError('usage: open <url>')
            url = args[0]
            if not re.match(r'^(https?|about|file|data):', url, re.I):
                url = 'https://' + url
            tab = await self.session_tab(session_id, create=True)
            await tab.send('Page.bringToFront', timeout=3)
            await tab.goto(url)
            await tab.wait_ready(10, quiet=0.6)
            await self.wait_out_challenge(tab)
            return await self.page_result(session_id, tab, action='open')
        tab = await self.session_tab(session_id)
        if action == 'snapshot':
            return await self.page_result(session_id, tab, action='snapshot',
                                          from_viewport='--viewport' in args)
        if action in ('click', 'activate'):
            if len(args) != 1:
                raise ValueError('usage: click <ref>')
            started = time.monotonic()
            method = await tab.click_ref(parse_ref(args[0]))
            tab, note = await self._settle_after_action(session_id, tab, started)
            if method == 'js':
                note = (note + ' ' if note else '') + '(element was covered or hidden; clicked it directly)'
            return await self.page_result(session_id, tab, action='click', note=note)
        if action in ('type', 'fill'):
            submit = '--submit' in args
            values = [a for a in args if a != '--submit']
            if len(values) != 2:
                raise ValueError('usage: type <ref> <text> [--submit]')
            started = time.monotonic()
            await tab.type_ref(parse_ref(values[0]), values[1], submit=submit)
            note = ''
            if submit:
                tab, note = await self._settle_after_action(session_id, tab, started)
            else:
                await asyncio.sleep(0.4)
            return await self.page_result(session_id, tab, action='type', note=note)
        if action == 'select':
            if len(args) != 2:
                raise ValueError('usage: select <ref> <option text>')
            selected = await tab.select_ref(parse_ref(args[0]), args[1])
            await tab.wait_ready(5, quiet=0.5)
            return await self.page_result(session_id, tab, action='select', note=f'Selected: {selected}')
        if action == 'press':
            if len(args) != 1:
                raise ValueError('usage: press <key>')
            started = time.monotonic()
            await tab.press(args[0])
            tab, note = await self._settle_after_action(session_id, tab, started)
            return await self.page_result(session_id, tab, action='press', note=note)
        if action == 'scroll':
            direction = (args[0] if args else 'down').lower()
            if direction not in ('down', 'up', 'top', 'bottom'):
                raise ValueError('usage: scroll [down|up|top|bottom]')
            await tab.scroll(direction)
            await tab.wait_ready(3, quiet=0.4)
            return await self.page_result(session_id, tab, action='scroll', from_viewport=True)
        if action in ('back', 'forward'):
            moved = await tab.history(-1 if action == 'back' else 1)
            if not moved and action == 'back' and len(self.sessions[session_id].stack) > 1:
                return await self.close_current(session_id)
            if not moved:
                raise PageError(f'no page to go {action} to')
            await tab.wait_ready(10, quiet=0.6)
            return await self.page_result(session_id, tab, action=action)
        if action == 'reload':
            tab.world = None
            await tab.send('Page.reload', timeout=10)
            await tab.wait_ready(10, quiet=0.6)
            return await self.page_result(session_id, tab, action='reload')
        if action == 'wait':
            seconds = min(float(args[0]) / 1000 if args else 1.0, 10.0)
            await asyncio.sleep(seconds)
            return await self.page_result(session_id, tab, action='wait')
        if action == 'eval':
            # Diagnostics only (the socket is private to this user; agents never send raw commands).
            return {'action': 'eval', 'text': json.dumps(await tab.eval(command.split(None, 1)[1], timeout=15),
                                                         ensure_ascii=False)[:20000]}
        if action == 'get':
            what = (args[0] if args else 'text').lower()
            if what == 'url':
                value = await tab.eval('location.href')
            elif what == 'title':
                value = await tab.eval('document.title')
            elif what == 'text':
                value = await tab.eval("document.body ? document.body.innerText.slice(0, 60000) : ''", timeout=15)
            else:
                raise ValueError('usage: get url|title|text')
            return {'action': 'get', 'text': value or ''}
        if action == 'screenshot':
            directory = DOWNLOADS / safe_name(session_id, 80)
            path = await tab.screenshot(directory / f'screenshot-{int(time.time() * 1000)}.jpg')
            return {'action': 'screenshot', 'path': str(path), 'screenshot': str(path),
                    'text': f'Screenshot: {path}'}
        raise ValueError(f'unsupported browser command: {action}')

    async def wait_out_challenge(self, tab, limit=8.0):
        """A Cloudflare-style interstitial usually lets a real browser through by itself within
        seconds. Wait for that (never reload: repeated loads escalate the block)."""
        deadline = time.monotonic() + limit
        waited = False
        while time.monotonic() < deadline:
            title = str(await tab.eval('document.title', timeout=3) or '')
            if not CHALLENGE_TITLE.search(title):
                if waited:
                    await tab.wait_ready(8, quiet=0.6)
                return
            waited = True
            await asyncio.sleep(1.0)

    async def close_current(self, session_id):
        state = self.sessions.get(session_id)
        if not state or not state.stack:
            return {'action': 'close', 'text': 'No tab open in this session'}
        tab = state.stack.pop()
        self.owned.discard(tab.target_id)
        await tab.close()
        if state.stack:
            current = state.stack[-1]
            await self.chrome.conn.send('Target.activateTarget', {'targetId': current.target_id}, timeout=3)
            return await self.page_result(session_id, current, action='close', note='Closed the tab; back on the previous one.')
        state.last_url = None
        return {'action': 'close', 'text': 'Current Pi session tab closed'}

    async def browser_health(self):
        if self.chrome is None:
            return {'action': 'browser-health', 'text': 'browser not started', 'browser': 'not-started'}
        started = time.monotonic()
        before = self.chrome
        await self.ensure_browser()
        state = 'relaunched' if self.chrome is not before else 'ok'
        return {'action': 'browser-health', 'browser': state, 'text': f'browser {state}',
                'probeMs': round((time.monotonic() - started) * 1000), 'tabs': len(self.chrome.pages()),
                'restarts': self.restarts}

    # ---- temporary tabs (crawl, search) -----------------------------------
    async def _temp_tab(self, owner):
        tab = await self.chrome.new_tab(background=True)
        self.owned.add(tab.target_id)
        self.temp_tabs[tab.target_id] = (tab, owner)
        try:
            await tab.send('Emulation.setDeviceMetricsOverride', {
                'width': 1920, 'height': 1080, 'deviceScaleFactor': 1, 'mobile': False}, timeout=5)
        except CDPError:
            pass
        except BaseException:
            await self._close_temp(tab.target_id)
            raise
        return tab

    async def _close_temp(self, target_id):
        entry = self.temp_tabs.pop(target_id, None)
        self.owned.discard(target_id)
        if entry is not None:
            await entry[0].close()

    async def cleanup_research_owner(self, owner):
        for target_id, (_tab, tab_owner) in list(self.temp_tabs.items()):
            if tab_owner == owner:
                await self._close_temp(target_id)

    async def sweep_crawl_pool(self):
        """Close temp tabs a failed close left behind; no crawl or search runs for 3 minutes."""
        for target_id, (tab, _owner) in list(self.temp_tabs.items()):
            if time.monotonic() - tab.created > 180:
                await self._close_temp(target_id)

    async def search_one(self, search, idx, session_id, search_slots, *, research=False):
        started = time.monotonic()
        query, direction = search['query'], search.get('direction', '')
        tab = None
        async with search_slots, self.crawl_slots:
            try:
                async def run():
                    nonlocal tab
                    tab = await self._temp_tab(session_id)
                    params = urllib.parse.urlencode({'q': query, 'hl': 'zh-TW', 'gl': 'tw', 'filter': '0'})
                    await tab.goto(f'https://www.google.com/search?{params}', timeout=8)
                    await tab.wait_ready(2.5, quiet=0.3, min_chars=50)
                    title = await tab.eval('document.title') or ''
                    body = await tab.eval("document.body ? document.body.innerText.slice(0, 5000) : ''") or ''
                    raw = await tab.eval(GOOGLE_RESULTS_JS)
                    return title, body, json.loads(raw) if isinstance(raw, str) else []
                title, body, rows = await asyncio.wait_for(run(), timeout=8.0)
                if any(marker in f'{title}\n{body}'.lower() for marker in GOOGLE_BLOCK_MARKERS):
                    raise PageError('Google anti-bot or consent challenge detected')
                results = [{'title': str(r.get('title') or '').strip(), 'url': str(r.get('url') or '').strip(),
                            'snippet': str(r.get('snippet') or '').strip(),
                            **({'truncated': bool(r.get('truncated'))} if research else {})}
                           for r in rows if isinstance(r, dict) and r.get('title') and r.get('url')]
                return {'index': idx + 1, 'direction': direction, 'query': query, 'results': results,
                        'ok': bool(results), 'error': None if results else 'No Google result links extracted',
                        'elapsed': round(time.monotonic() - started, 2)}
            except Exception as error:
                message = 'Google search timed out' if isinstance(error, asyncio.TimeoutError) else str(error)
                return {'index': idx + 1, 'direction': direction, 'query': query, 'results': [], 'ok': False,
                        'error': message, 'elapsed': round(time.monotonic() - started, 2)}
            finally:
                if tab is not None:
                    await self._close_temp(tab.target_id)

    async def crawl_one(self, target_url, idx, session_id, crawl_slots, *, max_text_units=None, fetch_timeout=3.0):
        if max_text_units is not None and (type(max_text_units) is not int or not 1 <= max_text_units <= 1_000_000):
            raise ValueError('invalid research DOM capture limit')
        if type(fetch_timeout) not in (int, float) or not 1.0 <= fetch_timeout <= 30.0:
            raise ValueError('invalid crawl fetch timeout')
        started = time.monotonic()
        tab = None
        capture = {}

        def failure(error, **extra):
            return {'index': idx + 1, 'url': target_url, 'title': extra.pop('title', 'Error'), 'text': '',
                    'ok': False, 'error': error, 'chars': 0, 'elapsed': round(time.monotonic() - started, 2),
                    'imageCandidates': [], 'imageCount': 0, **extra}

        async with crawl_slots, self.crawl_slots:
            try:
                async def load():
                    nonlocal tab, capture
                    tab = await self._temp_tab(session_id)
                    await tab.goto(target_url, timeout=fetch_timeout + 2)
                    await tab.wait_ready(max(1.0, fetch_timeout - 0.5), quiet=0.3, min_chars=21)
                    title = str(await tab.eval('document.title') or '').strip() or 'No Title'
                    if await tab.eval('document.contentType') == 'application/pdf':
                        return title, None
                    if max_text_units is not None:
                        capture = await tab.eval(f'JSON.stringify({research_capture_js(max_text_units)})')
                        capture = json.loads(capture) if isinstance(capture, str) else None
                        if not isinstance(capture, dict) or not isinstance(capture.get('text'), str):
                            raise PageError('invalid research DOM capture')
                        return title, capture.pop('text')
                    text = await tab.eval("document.body ? document.body.innerText : ''", timeout=10)
                    return title, str(text or '').strip()
                try:
                    title, text = await asyncio.wait_for(load(), timeout=fetch_timeout + 3)
                except asyncio.TimeoutError:
                    partial = await self._partial_capture(tab, max_text_units)
                    if partial is not None:
                        title, text, capture = partial
                    else:
                        return failure(f'page took more than {fetch_timeout:g}s to load', title='Timeout',
                                       browserSuspect=True)
                if text is None:
                    pdf, _final = await pdf_text(target_url)
                    if max_text_units is not None:
                        pdf = pdf[:max_text_units]
                    ok = bool(pdf.strip())
                    return {'index': idx + 1, 'url': target_url, 'title': title, 'text': pdf, 'ok': ok,
                            'error': None if ok else 'No extractable PDF text', 'chars': len(pdf),
                            'elapsed': round(time.monotonic() - started, 2), 'contentType': 'application/pdf',
                            'imageCandidates': [], 'imageCount': 0, 'sourceChars': len(pdf)}
                if is_challenge(title, text):
                    return failure('Anti-bot / Cloudflare challenge page (access blocked)', title=title)
                status = await tab.http_status()
                if status >= 400:
                    return failure(f'HTTP {status} error page', title=title)
                candidates = await self._image_candidates(tab)
                ok = len(text.strip()) > 20
                return {'index': idx + 1, 'url': target_url, 'title': title, 'text': text, 'ok': ok,
                        'error': None if ok else 'No readable text content extracted', 'chars': len(text),
                        'elapsed': round(time.monotonic() - started, 2), 'imageCandidates': candidates,
                        'imageCount': len(candidates), **capture}
            except FetchError as error:
                return failure(f'PDF download failed: {error}')
            except (PageError, CDPError) as error:
                return failure(str(error), browserSuspect=isinstance(error, (CDPTimeout, CDPClosed)))
            except Exception as error:
                return failure(f'{type(error).__name__}: {error}', browserSuspect=True)
            finally:
                if tab is not None:
                    await self._close_temp(tab.target_id)

    async def _partial_capture(self, tab, max_text_units):
        """Research: keep what a slow page rendered so far (RESEARCH_PARTIAL_ON_TIMEOUT=1)."""
        if tab is None or max_text_units is None or os.environ.get('RESEARCH_PARTIAL_ON_TIMEOUT') != '1':
            return None
        try:
            title = str(await tab.eval('document.title', timeout=0.8) or '').strip() or 'No Title'
            raw = await tab.eval(f'JSON.stringify({research_capture_js(max_text_units)})', timeout=0.8)
            capture = json.loads(raw)
            text = capture.pop('text')
            if len(text.strip()) > 20:
                return title, text, {**capture, 'partialOnTimeout': True}
        except Exception:
            pass
        return None

    async def _image_candidates(self, tab):
        try:
            raw = await tab.eval(IMAGE_CANDIDATES_JS, timeout=1.0)
            items = json.loads(raw) if isinstance(raw, str) and len(raw) < 131072 else []
        except Exception:
            return []
        candidates = []
        for item in items[:8] if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            url = str(item.get('url') or '').strip()
            if not url.startswith(('http://', 'https://')) or len(url) > 4096 or '[[' in url or ']]' in url:
                continue
            if any(c['url'] == url for c in candidates):
                continue
            role = item.get('role') if item.get('role') in ('representative', 'content', 'gallery', 'thumbnail') else 'content'

            def number(value):
                try:
                    value = int(float(value or 0))
                except (TypeError, ValueError, OverflowError):
                    return 0
                return value if 0 < value <= 100000 else 0
            candidates.append({'id': f'img-{len(candidates) + 1}', 'url': url,
                               'source': re.sub(r'\s+', ' ', str(item.get('source') or 'unknown'))[:80],
                               'role': role, 'width': number(item.get('width')), 'height': number(item.get('height')),
                               'alt': re.sub(r'\s+', ' ', str(item.get('alt') or ''))[:200],
                               'caption': re.sub(r'\s+', ' ', str(item.get('caption') or ''))[:200],
                               'score': number(item.get('score'))})
        return candidates

    async def run_fetch_image(self, target_url, session_id):
        return await fetch_image(target_url, DOWNLOADS / safe_name(str(session_id), 80))

    async def crawl_command(self, remainder, session_id):
        urls = []
        for url in re.findall(r'https?://[^\s"\'\]\[<>]+', remainder):
            url = url.rstrip('.,;)')
            if url and url not in urls:
                urls.append(url)
        if not urls:
            raise ValueError('usage: crawl <url1> [url2] ...')
        results = await asyncio.gather(*(self.crawl_one(url, i, session_id, asyncio.Semaphore(CRAWL_TABS),
                                                        fetch_timeout=8.0) for i, url in enumerate(urls)))
        ok = [r for r in results if r['ok']]
        parts = [f"Crawled {len(ok)}/{len(urls)} pages ({sum(r['chars'] for r in results):,} characters)."]
        if not ok:
            parts = [f'CRAWL FAILED: 0/{len(urls)} pages captured. Do not retry the same URLs right away.']
        for r in results:
            if r['ok']:
                images = ''.join(f"\n- image {c['id']}: {c['url']}" + (f" ({c['alt']})" if c['alt'] else '')
                                 for c in r['imageCandidates'][:3])
                parts.append(f"### [{r['index']}] [{r['title']}]({r['url']})\n"
                             f"*{r['chars']:,} chars, {r['elapsed']}s*{images}\n\n{r['text']}\n")
            else:
                parts.append(f"### [{r['index']}] [FAILED] {r['url']}\n*{r['error']} ({r['elapsed']}s)*\n")
        return {'action': 'crawl', 'text': '\n---\n'.join(parts), 'results': results,
                'successCount': len(ok), 'failedCount': len(results) - len(ok), 'totalCount': len(results),
                'imageCount': sum(r.get('imageCount', 0) for r in results)}

    async def google_search_command(self, remainder, session_id):
        payload = json.loads(remainder)
        searches = payload.get('searches') if isinstance(payload, dict) else payload
        if not isinstance(searches, list) or not searches:
            raise ValueError('usage: google-search {"searches":[{"direction":"...","query":"..."}]}')
        searches = [s if isinstance(s, dict) else {'query': str(s), 'direction': f'q{i + 1}'}
                    for i, s in enumerate(searches[:4])]
        for i, s in enumerate(searches):
            s.setdefault('direction', f'q{i + 1}')
            if not str(s.get('query') or '').strip():
                raise ValueError('every search needs a query')
        groups = await asyncio.gather(*(self.search_one(s, i, session_id, asyncio.Semaphore(4))
                                        for i, s in enumerate(searches)))
        from research.jobs import resolve_google_redirect
        seen, selected = set(), []
        for rank in range(10):
            for group in groups:
                if rank < len(group['results']):
                    item = group['results'][rank]
                    url = await asyncio.to_thread(resolve_google_redirect, item['url'])
                    if url in seen:
                        continue
                    seen.add(url)
                    selected.append({**item, 'url': url, 'directions': [group['direction']], 'query': group['query']})
        selected = selected[:10]
        if selected:
            lines = [f'Google search results for {len(searches)} direction(s) (Google via the shared browser, region Taiwan):']
            for n, item in enumerate(selected, 1):
                lines.append(f"### {n}. [{item['title']}]({item['url']})\n"
                             f"*Direction: {', '.join(item['directions'])} | Query: {item['query']}*\n"
                             f"{item['snippet'] or 'No snippet available.'}")
            text = '\n\n'.join(lines)
        else:
            text = 'No Google search results found. ' + '; '.join(f"{g['direction']}: {g['error']}" for g in groups)
        ok = sum(1 for g in groups if g['ok'])
        return {'action': 'google-search', 'provider': 'google-nodriver-tw', 'text': text, 'results': selected,
                'searches': groups, 'successCount': ok, 'failedCount': len(groups) - ok, 'totalCount': len(groups)}

    async def close(self):
        await self.shutdown_browser()


class CommandDeadline(Exception):
    pass


async def run_with_deadline(coroutine, timeout):
    task = asyncio.create_task(coroutine)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
    except BaseException:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    if task in done:
        return task.result()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    raise CommandDeadline


async def execute_request(service, request):
    session_id = str(request.get('sessionId') or 'default')
    base = {'id': request.get('id'), 'sessionId': session_id}
    try:
        result = await run_with_deadline(service.execute(str(request.get('command') or ''), session_id),
                                         COMMAND_TIMEOUT)
        return {**base, 'ok': True, **result}
    except CommandDeadline:
        log_event('command_timeout', command=str(request.get('command'))[:200], session=session_id)
        return {**base, 'ok': False, 'error': f'Browser command timed out after {COMMAND_TIMEOUT:g} seconds'}
    except StaleRef as error:
        return {**base, 'ok': False, 'error': f'STALE_REF: {error}'}
    except Exception as error:
        return {**base, 'ok': False, 'error': f'{type(error).__name__}: {error}'}


async def server_main(socket_path):
    from research.jobs import ResearchConnection
    service = BrowserService()
    session_locks = {}
    research_slots = asyncio.Semaphore(2)
    research_connections = set()
    client_writers = set()
    stop = asyncio.Event()
    path = Path(socket_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = path.with_name(path.name + '.lock').open('a+')
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('another browser worker owns this socket; exiting', file=sys.stderr, flush=True)
        return
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(f'{os.getpid()}\n')
    lock_file.flush()
    env_info = path.parent / 'nodriver-browser.env'
    env_info.write_text(f"DISPLAY={os.environ.get('DISPLAY', '')}\nXAUTHORITY={os.environ.get('XAUTHORITY', '')}\n"
                        f'PID={os.getpid()}\n')
    path.unlink(missing_ok=True)

    async def handle_client(reader, writer):
        client_writers.add(writer)
        active = {}
        write_lock = asyncio.Lock()

        async def send(response):
            async with write_lock:
                if writer.is_closing():
                    return
                writer.write((MARKER + json.dumps(response, ensure_ascii=False) + '\n').encode())
                await writer.drain()

        research = ResearchConnection(service, send, path.parent / 'research-artifacts', admission=research_slots)
        research_connections.add(research)

        async def process(request):
            request_id = request.get('id')
            session_id = str(request.get('sessionId') or 'default')
            command = str(request.get('command') or '').strip()
            action = command.split(maxsplit=1)[0].lower() if command else ''
            try:
                if action == 'research':
                    result = await research.run(request_id, session_id, request.get('research'))
                    response = {'id': request_id, 'sessionId': session_id, 'ok': True, 'type': 'result', **result}
                elif action == 'research-add-query':
                    research.add_query(session_id, request.get('research'))
                    response = {'id': request_id, 'sessionId': session_id, 'ok': True, 'type': 'result'}
                elif action == 'research-prefetch':
                    await research.prefetch(session_id, request.get('research'))
                    response = {'id': request_id, 'sessionId': session_id, 'ok': True, 'type': 'result'}
                elif action in ('crawl', 'google-search', 'fetch-image', 'browser-health', 'shutdown'):
                    response = await execute_request(service, request)
                else:
                    lock = session_locks.setdefault(session_id, asyncio.Lock())
                    async with lock:
                        response = await execute_request(service, request)
            except asyncio.CancelledError:
                response = {'id': request_id, 'sessionId': session_id, 'ok': False, 'error': 'Browser command cancelled'}
            except Exception as error:
                code = ('research_cleanup_incomplete' if str(error) in ('research_cleanup_incomplete', 'crawl_cleanup_incomplete')
                        else 'research_failed')
                response = {'id': request_id, 'sessionId': session_id, 'ok': False,
                            'error': code if action == 'research' else f'{type(error).__name__}: {error}'}
            finally:
                active.pop(request_id, None)
            await send(response)
            if response.get('action') == 'shutdown':
                stop.set()

        try:
            while line := await reader.readline():
                request = json.loads(line)
                if request.get('type') == 'planner_reply':
                    try:
                        research.reply(request)
                    except (ValueError, TypeError):
                        await send({'id': None, 'ok': False, 'error': 'invalid_planner_reply'})
                    continue
                cancel_id = request.get('cancelId')
                if cancel_id is not None:
                    task = active.get(cancel_id)
                    if task is not None:
                        task.cancel()
                    await send({'id': request.get('id'), 'sessionId': str(request.get('sessionId') or 'default'),
                                'ok': True, 'action': 'cancel', 'text': f'Cancellation requested for {cancel_id}'})
                    continue
                if request.get('id') in active:
                    raise ValueError('duplicate active request ID')
                active[request.get('id')] = asyncio.create_task(process(request))
        except Exception as error:
            await send({'id': None, 'ok': False, 'error': f'{type(error).__name__}: {error}'})
        finally:
            writer.close()
            pending = tuple(active.values())
            for task in pending:
                task.cancel()
            await research.close()
            research_connections.discard(research)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            client_writers.discard(writer)
            try:
                await writer.wait_closed()
            except Exception:
                pass

    loop = asyncio.get_running_loop()
    for value in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(value, stop.set)
    server = await asyncio.start_unix_server(handle_client, path=str(path), limit=1024 * 1024)
    path.chmod(0o600)
    health = asyncio.create_task(service.health_loop())
    log_event('worker_started', pid=os.getpid(), socket=str(path))
    try:
        await stop.wait()
    finally:
        health.cancel()
        server.close()
        if hasattr(server, 'close_clients'):
            server.close_clients()
        for writer in tuple(client_writers):
            writer.close()
        try:
            await asyncio.wait_for(asyncio.gather(*(c.close() for c in tuple(research_connections)),
                                                  return_exceptions=True), 5)
        except asyncio.TimeoutError:
            pass
        try:
            await asyncio.wait_for(service.close(), 15)
        except asyncio.TimeoutError:
            pass
        path.unlink(missing_ok=True)
        env_info.unlink(missing_ok=True)
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
        log_event('worker_stopped', pid=os.getpid())


async def stdio_main():
    service = BrowserService()
    try:
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            response = await execute_request(service, json.loads(line))
            print(MARKER + json.dumps(response, ensure_ascii=False), flush=True)
            if response.get('action') == 'shutdown':
                break
    finally:
        await service.close()


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--server':
        asyncio.run(server_main(sys.argv[2]))
    else:
        asyncio.run(stdio_main())
