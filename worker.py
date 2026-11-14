"""Shared browser worker for Pi: one Chrome, one Unix socket, many Pi sessions.

Usage: worker.py --server <socket>. Each request is one JSON line
{"id", "command", "sessionId", "research"?}; each reply is one line "__PI_NODRIVER__" + JSON
with the same id (research jobs send {"type": "progress"} frames before theirs).
{"cancelId": id} cancels a request.

Design rules:
- Every CDP call has a timeout (pibrowser.cdp); nothing can hang forever.
- Liveness is page-level: a fresh tab must run JavaScript (Chrome.probe).
  A failed probe restarts Chrome; idle Chrome is probed in the background,
  so a browser wedged while nobody used it is replaced before the next request.
- One window per Pi session (plus popups it opened); crawl/search tabs are
  temporary and always closed; idle session windows are closed after 30 minutes.
"""
import asyncio
import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import sys
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from pibrowser import macos  # noqa: E402
from pibrowser.cdp import CDPClosed, CDPError, CDPTimeout  # noqa: E402
from pibrowser.chrome import Chrome  # noqa: E402
from pibrowser.fetch import FetchError, fetch_image, pdf_text, safe_name  # noqa: E402
from pibrowser.page import TARGET_JS, PageError, StaleRef, Tab  # noqa: E402
from research.capture import research_capture_js  # noqa: E402

MARKER = '__PI_NODRIVER__'
# Isolated rigs and the live test point these at a scratch directory.
AGENT_DIR = Path(os.environ.get('PI_AGENT_DIR', Path.home() / '.pi' / 'agent'))
PROFILE = Path(os.environ.get('PI_NODRIVER_PROFILE', AGENT_DIR / 'nodriver-profile')).expanduser()
DOWNLOADS = AGENT_DIR / 'nodriver-downloads'
EVENTS = AGENT_DIR / 'nodriver-browser-events.jsonl'
CHROME_LOG = AGENT_DIR / 'nodriver-chrome.log'
COMMAND_TIMEOUT = 75.0
CRAWL_TABS = 48  # research crawls 32 pages at once
MAX_SESSION_TABS = 12
MAX_PAGE_TARGETS = 40
IDLE_TAB_SECONDS = 1800.0
HEALTH_INTERVAL = 60.0
IDLE_PROBE_AGE = 300.0
COMMAND_PROBE_AGE = 30.0
SLOW_PROBE_SECONDS = 20.0
# How a session page's wait tells "still loading" from noise (measured on 22 sites' load
# timelines, 2026-11-11): changes under 1% do not restart the quiet period (22 opens took
# 46-51 s instead of 63-65 s and the same pages came out whole), and a page with almost no
# text (under 50 characters: Google Maps' shell has 1) waits longer for its results.
SETTLE = {'tolerance': 0.01, 'thin': 50}
LOADING_JS = ("(() => { const t = document.body ? document.body.innerText : ''; "
              "return /載入中|讀取中|加载中|查詢中|搜尋中|loading\\s?(\\.\\.\\.|…)|読み込み中/i.test(t.length > 30000 ? t.slice(0, 30000) : t); })()")
POPUP_URL = re.compile(r'(https?:|about:blank)')
JS_DIR = HERE / 'pibrowser' / 'js'
GOOGLE_RESULTS_JS = (JS_DIR / 'google_results.js').read_text()
IMAGE_CANDIDATES_JS = (JS_DIR / 'image_candidates.js').read_text()
CHALLENGE_MARKERS = ('just a moment', 'attention required', 'verify you are human', 'checking your browser',
                     'enable javascript and cookies to continue', 'challenge validation')
CHALLENGE_TITLE = re.compile(r'just a moment|請稍候|attention required|checking your browser|one more step', re.I)
GOOGLE_BLOCK_MARKERS = ('unusual traffic', 'before you continue to google', 'verify you are human')
PAGE_COMMANDS = {'open', 'snapshot', 'click', 'type', 'select', 'press', 'scroll', 'back', 'get', 'screenshot',
                 'upload', 'crawl', 'google-search'}
INCOMING = DOWNLOADS / '_incoming'   # downloads land here under their id until an action claims them
UPLOAD_MAX_BYTES = 2_000_000_000
# Retried once on a relaunched Chrome: they only read pages.
RETRY_AFTER_RESTART = {'open', 'snapshot', 'get', 'screenshot', 'crawl', 'google-search'}
# Not serialized behind the session's page commands.
LOCK_FREE = {'crawl', 'google-search', 'fetch-image', 'browser-health', 'shutdown'}
GET_SCRIPTS = {
    'text': "document.body ? document.body.innerText.slice(0, 60000) : ''",
    # Every link address on the page, for checking URLs an answer gives.
    'links': "[...new Set([...document.links].map((a) => a.href).filter((h) => /^https?:/.test(h)))]"
             ".slice(0, 3000).join('\\n')",
}


def loggable(command):
    """A command as the events log may keep it: what a `type` command types is left out
    (it can be a password the planner was given only as a name)."""
    parts = command.split(' ', 2)
    if parts and parts[0] == 'type' and len(parts) == 3:
        return f"type {parts[1]} (text not logged){' --submit' if command.endswith(' --submit') else ''}"
    return command


# ---- outside the page ---------------------------------------------------------
# An action's result is not always on the page: a system window (a file chooser in a frame
# of another site, xdg-open's "Select Application"), the clipboard. Chrome reports neither;
# the X display the worker's Chrome runs on does. Without the tools or a display these
# looks return nothing.
X_TOOLS = {name: shutil.which(name) for name in ('xwininfo', 'xprop', 'xdotool', 'xclip')}
# Said with everything the tool cannot do: the planner's rules do not speak of tool limits.
NO_WAY = (' Do not try it again: use another way the page offers, or reply '
          '{"action":"stop","status":"unsupported","reason":"<which step is left for the user to do by hand>"}.')
TOP_WINDOW = re.compile(r'^     (0x[0-9a-f]+) ', re.M)


async def run_tool(*argv, timeout=1.5):
    if not X_TOOLS.get(argv[0]) or not os.environ.get('DISPLAY'):
        return ''
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            X_TOOLS[argv[0]], *argv[1:], stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        return out.decode('utf-8', 'replace')
    except (OSError, asyncio.TimeoutError):
        if proc is not None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        return ''


async def x_windows():
    """Ids of the display's top-level windows."""
    return set(TOP_WINDOW.findall(await run_tool('xwininfo', '-root', '-tree')))


async def x_dialog_title(window):
    """The title of a top-level window when it is a system dialog on screen, else None.
    (A plain click opens none; Chrome's own menus and pickers are override-redirect popups.)"""
    props = await run_tool('xprop', '-id', window, '_NET_WM_WINDOW_TYPE', '_NET_WM_NAME', 'WM_NAME')
    if '_NET_WM_WINDOW_TYPE_DIALOG' not in props:
        return None
    if 'IsViewable' not in await run_tool('xwininfo', '-id', window):
        return None
    name = re.search(r'^_NET_WM_NAME\(UTF8_STRING\) = "(.*)"$', props, re.M) or re.search(r'^WM_NAME\(\w+\) = "(.*)"$', props, re.M)
    title = name.group(1) if name else ''
    if re.search(r'\\\d{3}', title):   # xprop in a non-UTF-8 locale writes bytes as \351\226\213
        with contextlib.suppress(ValueError, UnicodeError):
            title = title.encode('latin-1').decode('unicode_escape').encode('latin-1').decode('utf-8')
    return title or 'untitled'


async def clipboard_text():
    return (await run_tool('xclip', '-o', '-selection', 'clipboard', timeout=1.0))[:2000]


def file_name(name):
    name = re.sub(r'[\\/\x00-\x1f]+', '_', str(name)).strip('. ')[:150]
    return name or 'download'


def free_path(directory, name):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    stem, suffix = path.stem, path.suffix
    for n in range(2, 1000):
        if not path.exists():
            break
        path = directory / f'{stem}-{n}{suffix}'
    return path


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


def format_snapshot(snap, *, status=0, dialogs=(), note=''):
    header = [f"URL: {snap.get('url', '')}", f"Title: {snap.get('title', '')}"]
    if status and status >= 400:
        header.append(f'HTTP status: {status} (error page)')
    if snap.get('scrollHeight'):
        header.append(f"Scroll: {snap.get('scrollY', 0)}/{snap['scrollHeight']}px "
                      f"(viewport {snap.get('viewportHeight', 0)}px)")
    header += [f'Dialog closed automatically: {dialog}' for dialog in dialogs]
    if note:
        header.append(note)
    tail = '\n(… the page goes on past what could be listed)' if snap.get('truncated') else ''
    return '\n'.join(header) + '\n---\n' + (snap.get('text') or '(no visible text)') + tail


def is_challenge(title, text):
    lower = f'{title}\n{text[:3000]}'.lower()
    return any(marker in lower for marker in CHALLENGE_MARKERS)


class SessionState:
    def __init__(self):
        self.stack = []  # tabs; the last one is current (popups stack on their opener)
        self.ref_base = 0  # the highest element ref used: refs stay unique over the session's pages
        self.last_url = None
        self.last_used = time.monotonic()


class BrowserService:
    """Owns Chrome and runs the page commands. Research (research.jobs.ResearchConnection)
    also uses ensure_browser, search_one, crawl_one, run_fetch_image, image_fetch_semaphore,
    cleanup_research_owner, research_cleanup_tasks and .browser."""

    def __init__(self):
        self.browser = None
        self.lifecycle = asyncio.Lock()
        self.verified_at = float('-inf')
        self.sessions = {}
        self.temp_tabs = {}  # crawl/search tabs: target id -> (tab, owner)
        self.crawl_slots = asyncio.Semaphore(CRAWL_TABS)
        self.image_fetch_semaphore = asyncio.Semaphore(4)
        self.research_cleanup_tasks = set()
        self.last_activity = time.monotonic()
        self.restarts = 0

    # ---- lifecycle -------------------------------------------------------
    async def ensure_browser(self, max_age=None):
        if max_age is not None and self._fresh(max_age):
            return self.browser
        async with self.lifecycle:
            if max_age is not None and self._fresh(max_age):
                return self.browser
            if self.browser is not None:
                started = time.monotonic()
                healthy = await self.browser.probe()
                if not healthy and self.browser.alive():
                    # A loaded machine (a model loading, a CPU-bound job) can make a working
                    # Chrome miss 5 s; a restart would close every conversation's window.
                    # A wedged Chrome fails the long probe too.
                    healthy = await self.browser.probe(timeout=SLOW_PROBE_SECONDS)
                    if healthy:
                        log_event('probe_slow', ms=round((time.monotonic() - started) * 1000))
                if healthy:
                    self.verified_at = time.monotonic()
                    return self.browser
                log_event('probe_failed', alive=self.browser.alive(),
                          ms=round((time.monotonic() - started) * 1000))
            await self._restart_locked()
            return self.browser

    def _fresh(self, max_age):
        return (self.browser is not None and self.browser.alive()
                and time.monotonic() - self.verified_at < max_age)

    async def _restart_locked(self):
        old = self.browser
        if old is not None:
            self.restarts += 1
            await old.stop()
        for state in self.sessions.values():
            state.stack.clear()
        self.temp_tabs.clear()
        chrome = Chrome(PROFILE, CHROME_LOG, downloads=INCOMING)
        await chrome.start()
        if not await chrome.probe(timeout=10):
            await chrome.stop()
            raise RuntimeError('Chrome started but a new tab cannot run JavaScript')
        self.browser = chrome
        self.verified_at = time.monotonic()
        log_event('browser_started' if old is None else 'browser_restarted', pid=chrome.proc.pid, port=chrome.port)

    async def close(self):
        async with self.lifecycle:
            if self.browser is not None:
                await self.browser.stop()
                self.browser = None
            self.sessions.clear()
            self.temp_tabs.clear()

    async def health_loop(self):
        """Background: replace a wedged Chrome before anyone needs it; close idle and leaked tabs."""
        while True:
            await asyncio.sleep(HEALTH_INTERVAL)
            try:
                if self.browser is None:
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
        """Close temp tabs a failed close left behind (no crawl or search runs 3 minutes) and,
        as a safety valve, the oldest foreign tabs once Chrome holds more than MAX_PAGE_TARGETS
        pages (e.g. leaked by another script)."""
        if self.browser is None:  # shut down meanwhile
            return
        for target_id, (tab, _owner) in list(self.temp_tabs.items()):
            if time.monotonic() - tab.created > 180:
                await self._close_temp(target_id)
        pages = self.browser.pages()
        if len(pages) > MAX_PAGE_TARGETS:
            owned = self._owned()
            foreign = sorted((p for p in pages if p['targetId'] not in owned), key=lambda p: p['_seen'])
            for info in foreign[:len(pages) - MAX_PAGE_TARGETS]:
                await self.browser.close_target(info['targetId'])
            log_event('foreign_tabs_closed', count=min(len(foreign), len(pages) - MAX_PAGE_TARGETS))

    # ---- session tabs ----------------------------------------------------
    def _owned(self):
        """Target ids of every tab a session or a crawl/search job holds."""
        return {tab.target_id for state in self.sessions.values() for tab in state.stack} | self.temp_tabs.keys()

    def _live(self, tab):
        return tab.chrome is self.browser and tab.target_id in self.browser.targets

    async def session_tab(self, session_id, *, create=False):
        state = self.sessions.setdefault(session_id, SessionState())
        state.last_used = time.monotonic()
        state.stack = [tab for tab in state.stack if self._live(tab)]
        if state.stack:
            return state.stack[-1]
        if create:
            return await self._new_session_tab(session_id, state)
        if not state.last_url:
            raise PageError('no page is open in this session; open a URL first')
        # The tab died (browser restart, closed by the page): bring the page back.
        tab = await self._new_session_tab(session_id, state)
        await tab.goto(state.last_url)
        await tab.wait_ready(10)
        return tab

    async def _new_session_tab(self, session_id, state):
        await self._evict_session_tabs(keep=session_id)
        tab = await self.browser.new_tab(new_window=True)
        state.stack.append(tab)
        with contextlib.suppress(CDPError):
            await tab.watch_network()
        with contextlib.suppress(CDPError):
            await tab.watch_outside()
        return tab

    async def _evict_session_tabs(self, keep):
        live = [(sid, st) for sid, st in self.sessions.items() if sid != keep and st.stack]
        excess = sum(len(st.stack) for _, st in live) + 1 - MAX_SESSION_TABS
        for session_id, state in sorted(live, key=lambda item: item[1].last_used):
            if excess <= 0:
                break
            excess -= len(state.stack)
            await self.close_session(session_id)

    async def close_session(self, session_id):
        state = self.sessions.get(session_id)
        if not state:
            return 0
        tabs, state.stack = state.stack, []
        for tab in tabs:
            await tab.close()
        return len(tabs)

    def _new_popup(self, opener_id, since):
        """The tab an action opened: one this tab opened, else a new web page nobody here
        owns. Never Chrome's own UI targets (chrome://omnibox-popup appeared after a click
        on thsrc.com.tw) or another session's or job's tab."""
        owned = self._owned()
        candidates = [info for info in self.browser.pages()
                      if info['targetId'] not in owned and info['_seen'] >= since
                      and POPUP_URL.match(info.get('url') or 'about:blank')]
        preferred = [info for info in candidates if info.get('openerId') == opener_id]
        return (preferred or candidates or [None])[0]

    async def _outside_before(self):
        """The display's windows and the clipboard before an action, to tell what it changed."""
        windows, clip = await asyncio.gather(x_windows(), clipboard_text())
        return {'windows': windows, 'clip': clip}

    async def _outside_after(self, session_id, tab, started, outside, *, ref=''):
        """What the action did outside the page: (notes for the reader, files it produced).
        Every line says what happened and, where the tool has an answer, how to go on; where it
        has none it says so, so the task can end as a tool limit and not as a blocked site."""
        notes, files = [], []
        chooser = tab.chooser
        if chooser and chooser['at'] >= started and not chooser.get('told'):
            chooser['told'] = True
            chooser['ref'] = ref
            if 'backendNodeId' in chooser:
                many = 'several files' if chooser.get('mode') == 'selectMultiple' else 'one file'
                notes.append(f'The action opened a file chooser (for {many}); nothing was chosen. To give it a file: '
                             '{"action":"upload","ref":"%s","path":"<a file path the goal gives>"}' % (ref or 'the same ref'))
            else:
                tab.chooser = None
                notes.append('TOOL LIMIT: the page asked for a file through the File System Access picker, '
                             'which this tool cannot fill.' + NO_WAY)
        if macos.IS_MACOS and hasattr(tab, 'take_blocked_apps'):
            await tab.take_blocked_apps()
        apps, tab.other_apps = tab.other_apps, []
        for address in apps:
            notes.append(f'The page tried to open another program ({address}); nothing outside the browser opens here.')
        # Downloads this tab started (or one no other session's tab did).
        others = {t.target_id for sid, st in self.sessions.items() if sid != session_id for t in st.stack}
        mine = [(guid, d) for guid, d in self.browser.downloads.items()
                if not d.get('claimed') and d['at'] >= started - 0.2 and d['frame'] not in others]
        if mine:
            deadline = time.monotonic() + 8
            while any(d['state'] == 'inProgress' for _, d in mine) and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            for guid, d in mine:
                d['claimed'] = True
                source = INCOMING / guid
                if d['state'] == 'completed' and source.exists():
                    path = free_path(DOWNLOADS / safe_name(session_id, 80), file_name(d['name']))
                    source.replace(path)
                    files.append(str(path))
                    notes.append(f"The action downloaded a file: {d['name']} ({path.stat().st_size} bytes), saved as {path}")
                elif d['state'] == 'canceled':
                    notes.append(f"A download of {d['name']} started and was cancelled.")
                else:
                    notes.append(f"A download of {d['name']} is still running ({d['bytes']} bytes so far); it is being saved as {source}")
        asked, tab.auth_asked = tab.auth_asked, None
        if asked and asked[1] >= started:
            notes.append(f'TOOL LIMIT: {asked[0]} asks for a user name and password in the browser\'s own sign-in box '
                         '(HTTP authentication), not on a page; this tool cannot type into that box.' + NO_WAY)
        # A window that prints as it loads (a ticket, a receipt opened in a new tab) shows Chrome's
        # print preview before the tab is watched: close the preview, the page is saved below.
        current = self.sessions[session_id].stack[-1] if self.sessions[session_id].stack else tab
        preview = lambda: [i for i in self.browser.targets.values()
                           if str(i.get('url') or '').startswith('chrome://print') and i.get('_seen', 0) >= started - 0.2]
        printed = bool(preview())
        if printed:
            await run_tool('xdotool', 'key', 'Escape')
            deadline = time.monotonic() + 2
            while preview() and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            if preview():
                printed = False
                notes.append('TOOL LIMIT: the page opened the browser\'s print dialog, which this tool cannot operate; it is still open.' + NO_WAY)
        if self._live(tab) and await tab.print_calls():
            printed, current = True, tab
        if printed and self._live(current):
            tab = current
            try:
                path = await tab.save_pdf(free_path(DOWNLOADS / safe_name(session_id, 80), f'print-{int(time.time())}.pdf'))
                files.append(str(path))
                notes.append(f'The page asked to print. There is no printer here: the page was saved as a PDF instead: {path}')
            except (CDPError, OSError) as error:
                notes.append(f'The page asked to print. There is no printer here, and saving it as a PDF failed ({error}).')
        if outside is not None:
            clip = await clipboard_text()
            if clip and clip != outside['clip']:
                notes.append('The action copied to the clipboard: ' + json.dumps(clip[:400], ensure_ascii=False))
            for window in sorted(await x_windows() - outside['windows']):
                title = await x_dialog_title(window)
                if title is None:
                    continue
                # A real Escape to the window: GTK ignores a key event that is only sent to it.
                await run_tool('xdotool', 'windowfocus', str(int(window, 16)), 'key', 'Escape')
                await asyncio.sleep(0.2)
                closed = await x_dialog_title(window) is None
                log_event('system_dialog', title=title, closed=closed, session=session_id)
                notes.append(f'TOOL LIMIT: the action opened a system window ("{title}") outside the page, '
                             f"which this tool cannot operate; it {'was closed' if closed else 'is still open'}." + NO_WAY)
        return notes, files

    async def _upload(self, tab, ref, paths):
        """Give files to the page through `ref`: the file field itself, or what opens the file
        chooser (a button, a box). Returns (done, what happened). Without a chooser the files go
        to the file field found for the ref, or are dropped onto it."""
        names = ', '.join(Path(p).name for p in paths)
        pending, tab.chooser = tab.chooser, None
        if pending and pending.get('ref') == ref and 'backendNodeId' in pending and time.monotonic() - pending['at'] < 300:
            with contextlib.suppress(CDPError):
                if pending.get('mode') == 'selectSingle' and len(paths) > 1:
                    raise PageError('this file chooser takes one file')
                await tab.set_files(paths, backend_node=pending['backendNodeId'])
                return True, f'Gave {names} to the file chooser.'
        target = await tab.call(TARGET_JS, ref)
        if not target or target.get('error'):
            raise StaleRef(f'{ref} is not on the page any more; take a new snapshot')
        if target.get('tag') == 'input' and target.get('type') == 'file':
            await tab.set_files(paths, ref=ref)
            return True, f'Put {names} into the file field.'
        state = await tab.ready_state()
        await tab.click_ref(ref)
        deadline = time.monotonic() + 1.5
        while tab.chooser is None and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        chooser, tab.chooser = tab.chooser, None
        if chooser is not None:
            if 'backendNodeId' not in chooser:
                return False, ('TOOL LIMIT: the page asks for the file through the File System Access picker, '
                               'which this tool cannot fill; nothing was uploaded.' + NO_WAY)
            if chooser.get('mode') == 'selectSingle' and len(paths) > 1:
                raise PageError('this file chooser takes one file; upload one path')
            await tab.set_files(paths, backend_node=chooser['backendNodeId'])
            return True, f'Gave {names} to the file chooser it opened.'
        now = await tab.ready_state()
        if tab.nav_pending or now is None or state is None or tuple(now[1:]) != tuple(state[1:]):
            return False, ('Clicking it opened no file chooser and changed the page instead; nothing was uploaded. '
                           'Give upload the element that takes the file.')
        if await tab.set_files(paths, ref=ref):
            return True, f'It opened no file chooser; put {names} into the file field next to it.'
        await tab.drop_files(ref, paths)
        return True, (f'It opened no file chooser and the page has no file field; dropped {names} onto it. '
                      'Check that the page shows the file.')

    async def _settle_after_action(self, session_id, tab, started, before):
        """After a click/submit/key: adopt a popup it opened, or wait for what it started on
        the page. `before` is the page's ready_state() from before the action: a page that
        shows no difference from it within a second (and opened nothing) is taken as is.
        (This used to sleep 0.95 s before it looked at the page at all.)"""
        state = self.sessions[session_id]
        note = ''
        popup = lambda: self._new_popup(tab.target_id, started)
        await tab.wait_ready(8, quiet=0.5, react=(before, 1.0), stop=popup, data_since=started, **SETTLE)
        opened = popup()
        if opened is not None:
            tab = await Tab.attach(self.browser, opened['targetId'])
            state.stack.append(tab)
            with contextlib.suppress(CDPError):
                await tab.watch_network()
            with contextlib.suppress(CDPError):
                await tab.watch_outside()
            if len(state.stack) > 3:
                await state.stack.pop(0).close()
            if not macos.should_hide():  # macOS: would unhide Chrome and take the user's focus
                await self.browser.conn.send('Target.activateTarget', {'targetId': tab.target_id}, timeout=3)
            note = 'The action opened a new tab; you are now on it (back returns to the previous tab).'
            await tab.wait_ready(8, quiet=0.6, blank_ok=False, **SETTLE)
        await self.wait_out_loading(tab)
        return tab, note

    async def page_result(self, session_id, tab, *, action, note='', notes=(), files=()):
        status = await tab.http_status()
        state = self.sessions[session_id]
        snap = await tab.snapshot(base=state.ref_base)
        state.ref_base = max(state.ref_base, snap.get('lastRef') or 0)
        state.last_url = snap.get('url') or state.last_url
        dialogs, tab.dialogs = tab.dialogs, []
        dialogs = [dialog + (' (to answer OK, do the action again with "dialog":"ok")' if dialog.endswith('answered Cancel') else '')
                   for dialog in dialogs]
        note = ' '.join(filter(None, [note, *notes]))
        text = format_snapshot(snap, status=status, dialogs=dialogs, note=note)
        # 'snapshot' is the page as one text (header, ---, lines). The agent reads the parts:
        # 'body' (the lines), 'doc' (changes with the document), 'links' (ref -> address) and 'notes'.
        notes = [f'Dialog closed automatically: {dialog}' for dialog in dialogs] + ([note] if note else [])
        return {'action': action, 'url': snap.get('url'), 'title': snap.get('title'), 'httpStatus': status,
                'files': list(files),
                'elements': snap.get('elements'), 'text': text, 'snapshot': text,
                'body': snap.get('text') or '', 'doc': f"{tab.target_id[:8]}.{snap.get('doc', '')}",
                'links': snap.get('links') or {}, 'truncated': bool(snap.get('truncated')), 'notes': notes,
                'scrollY': snap.get('scrollY', 0), 'scrollHeight': snap.get('scrollHeight', 0)}

    # ---- commands --------------------------------------------------------
    async def execute(self, command, session_id='default'):
        action = command.split(None, 1)[0].lower() if command.strip() else ''
        # crawl and google-search carry raw JSON: never shell-split them.
        args = [] if action in ('crawl', 'google-search') else shlex.split(command)[1:]
        self.last_activity = time.monotonic()
        if action == 'browser-health':
            return await self.browser_health()
        if action == 'shutdown':
            await self.close()
            return {'action': action, 'text': 'Browser daemon shutting down'}
        if action == 'session-cleanup':
            closed = await self.close_session(session_id)
            self.sessions.pop(session_id, None)
            return {'action': action, 'text': f'closed {closed} tab(s)'}
        if action == 'fetch-image':
            async with self.image_fetch_semaphore:
                path, mime, width, height, final_url = await self.run_fetch_image(args[0], session_id)
            return {'action': action, 'imagePath': str(path), 'mimeType': mime, 'width': width,
                    'height': height, 'size': path.stat().st_size, 'url': final_url,
                    'text': (f'Image fetched\nType: {mime}\nDimensions: {width}x{height}\nPath: {path}\n'
                             f'To send this image to the user, include exactly: [[image: {path}]]')}
        if action not in PAGE_COMMANDS:
            raise ValueError(f'unsupported browser command: {action}')
        await self.ensure_browser(max_age=COMMAND_PROBE_AGE)
        try:
            return await self._page_command(action, args, command, session_id)
        except CDPTimeout as error:
            # A page that does not answer: slow page, or a wedged browser. Probe now;
            # a dead browser is restarted so the next command works.
            before = self.browser
            await self.ensure_browser()
            if self.browser is before:
                raise
            log_event('restarted_after_timeout', action=action, error=str(error))
            if action in RETRY_AFTER_RESTART:
                return await self._page_command(action, args, command, session_id)
            raise PageError(f'the browser stopped responding and was restarted ({error}); '
                            'the page was reloaded, take a new snapshot') from None

    async def _page_command(self, action, args, command, session_id):
        if action == 'crawl':
            return await self.crawl_command(command[len('crawl'):], session_id)
        if action == 'google-search':
            return await self.google_search_command(command[len('google-search'):], session_id)
        if action == 'open':
            url = args[0]
            if not re.match(r'^(https?|about|file|data):', url, re.I):
                url = 'https://' + url
            tab = await self.session_tab(session_id, create=True)
            if not macos.should_hide():  # macOS: would unhide Chrome and take the user's focus
                await tab.send('Page.bringToFront', timeout=3)
            outside = await self._outside_before()
            started = time.monotonic()
            await tab.goto(url)
            await tab.wait_ready(10, quiet=0.6, **SETTLE)
            if not await self.wait_out_challenge(tab):
                await self.wait_out_loading(tab)
            notes, files = await self._outside_after(session_id, tab, started, outside)
            return await self.page_result(session_id, tab, action='open', notes=notes, files=files)
        tab = await self.session_tab(session_id)
        note = ''
        if action == 'get':
            return {'action': 'get', 'text': await tab.eval(GET_SCRIPTS[args[0] if args else 'text'], timeout=15) or ''}
        if action == 'screenshot':
            name = f'screenshot-{int(time.time() * 1000)}.jpg'
            path = await tab.screenshot(DOWNLOADS / safe_name(session_id, 80) / name)
            return {'action': 'screenshot', 'path': str(path), 'screenshot': str(path), 'text': f'Screenshot: {path}'}
        if action == 'scroll':
            await tab.scroll(args[0] if args else 'down')
            await tab.wait_ready(3, quiet=0.4)
            return await self.page_result(session_id, tab, action='scroll')
        before = await tab.ready_state() if action in ('click', 'type', 'select', 'press', 'upload') else None
        # --ok: a confirm box this action opens is answered OK (otherwise Cancel).
        acts = action in ('click', 'press', 'upload') or (action == 'type' and '--submit' in args)
        tab.accept_dialogs = acts and '--ok' in args
        args = [arg for arg in args if arg != '--ok'] if acts else args
        outside = await self._outside_before() if acts else None
        first, started, notes, files = tab, time.monotonic(), [], []
        if action == 'click':
            method = await tab.click_ref(args[0])
            tab, note = await self._settle_after_action(session_id, tab, started, before)
            if method == 'js':
                note = (note + ' ' if note else '') + '(element was covered or hidden; clicked it directly)'
        elif action == 'upload':
            ref, paths = args[0], [str(Path(path).expanduser()) for path in args[1:]]
            if not paths:
                raise PageError('upload needs a file path')
            for path in paths:
                if not Path(path).is_file():
                    raise PageError(f'no such file: {path}')
                if Path(path).stat().st_size > UPLOAD_MAX_BYTES:
                    raise PageError(f'{path} is larger than 2 GB')
            done, note = await self._upload(tab, ref, paths)
            log_event('upload', done=done, files=len(paths), session=session_id)
            tab, more = await self._settle_after_action(session_id, tab, started, before)
            note = ' '.join(filter(None, [note, more]))
        elif action == 'type':
            submit = '--submit' in args
            ref, text = [arg for arg in args if arg != '--submit']
            await tab.type_ref(ref, text, submit=submit)
            if submit:
                tab, note = await self._settle_after_action(session_id, tab, started, before)
            else:
                # A suggestion list may open: wait for it to stand, or 0.4 s when nothing shows.
                await tab.wait_ready(2, quiet=0.2, react=(before, 0.4))
        elif action == 'select':
            started = time.monotonic()
            note = f'Selected: {await tab.select_ref(args[0], args[1])}'
            await tab.wait_ready(5, quiet=0.4, react=(before, 0.3), data_since=started, **SETTLE)
        elif action == 'press':
            await tab.press(args[0])
            tab, note = await self._settle_after_action(session_id, tab, started, before)
        elif action == 'back':
            if await tab.back():
                await tab.wait_ready(10, quiet=0.6, **SETTLE)
            else:
                # The first page of a popup: close it and return to the tab that opened it.
                state = self.sessions[session_id]
                if len(state.stack) < 2:
                    raise PageError('no page to go back to')
                await state.stack.pop().close()
                tab = state.stack[-1]
                if not macos.should_hide():  # macOS: would unhide Chrome and take the user's focus
                    await self.browser.conn.send('Target.activateTarget', {'targetId': tab.target_id}, timeout=3)
                note = 'Closed the tab; back on the previous one.'
        first.accept_dialogs = False
        if acts:
            notes, files = await self._outside_after(session_id, first, started, outside,
                                                     ref=args[0] if action in ('click', 'upload') else '')
        return await self.page_result(session_id, tab, action=action, note=note, notes=notes, files=files)

    async def wait_out_loading(self, tab, limit=3.0):
        """A page that says it is loading is waited for: Ruten's result list stood as "載入中
        請稍候..." with nameless links for 1-3 s after the page had gone quiet, and the planner
        took 13 steps to find the products. Once per address: a page that always says so is
        not waited for again."""
        try:
            url = await tab.eval('location.href', timeout=3)
            if getattr(tab, 'said_loading', None) == url or not await tab.eval(LOADING_JS, timeout=3):
                return
            deadline = time.monotonic() + limit
            while time.monotonic() < deadline:
                await asyncio.sleep(0.2)
                if not await tab.eval(LOADING_JS, timeout=3):
                    await tab.wait_ready(3, quiet=0.4, **SETTLE)
                    return
            tab.said_loading = url
        except (CDPError, PageError):
            pass

    async def wait_out_challenge(self, tab, limit=8.0):
        """A Cloudflare-style interstitial usually lets a real browser through by itself within
        seconds. Wait for that (never reload: repeated loads escalate the block). True when the
        interstitial is still there at the end."""
        deadline = time.monotonic() + limit
        waited = False
        while time.monotonic() < deadline:
            if not CHALLENGE_TITLE.search(str(await tab.eval('document.title', timeout=3) or '')):
                if waited:
                    await tab.wait_ready(8, quiet=0.6, **SETTLE)
                return False
            waited = True
            await asyncio.sleep(1.0)
        return True

    async def browser_health(self):
        if self.browser is None:
            return {'action': 'browser-health', 'text': 'browser not started', 'browser': 'not-started'}
        started = time.monotonic()
        before = self.browser
        await self.ensure_browser()
        state = 'relaunched' if self.browser is not before else 'ok'
        return {'action': 'browser-health', 'browser': state, 'text': f'browser {state}',
                'probeMs': round((time.monotonic() - started) * 1000), 'tabs': len(self.browser.pages()),
                'restarts': self.restarts}

    # ---- temporary tabs (crawl, search) -----------------------------------
    async def _temp_tab(self, owner):
        tab = await self.browser.new_tab()
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
        if entry is not None:
            await entry[0].close()

    async def cleanup_research_owner(self, owner):
        for target_id, (_tab, tab_owner) in list(self.temp_tabs.items()):
            if tab_owner == owner:
                await self._close_temp(target_id)

    async def search_one(self, search, idx, session_id, search_slots):
        started = time.monotonic()
        result = {'index': idx + 1, 'direction': search.get('direction', ''), 'query': search['query'],
                  'results': [], 'ok': False, 'error': None}
        tab = None
        async with search_slots, self.crawl_slots:
            try:
                async with asyncio.timeout(8.0):
                    tab = await self._temp_tab(session_id)
                    params = urllib.parse.urlencode({'q': search['query'], 'hl': 'zh-TW', 'gl': 'tw', 'filter': '0'})
                    await tab.goto(f'https://www.google.com/search?{params}', timeout=8)
                    await tab.wait_ready(2.5, quiet=0.3, min_chars=50)
                    title = await tab.eval('document.title') or ''
                    body = await tab.eval("document.body ? document.body.innerText.slice(0, 5000) : ''") or ''
                    rows = await tab.eval(GOOGLE_RESULTS_JS) or []
                if any(marker in f'{title}\n{body}'.lower() for marker in GOOGLE_BLOCK_MARKERS):
                    raise PageError('Google anti-bot or consent challenge detected')
                result['results'] = [
                    {'title': row['title'].strip(), 'url': row['url'].strip(), 'snippet': row['snippet'].strip()}
                    for row in rows]
                result['ok'] = bool(result['results'])
                if not result['ok']:
                    result['error'] = 'No Google result links extracted'
            except Exception as error:
                result['error'] = 'Google search timed out' if isinstance(error, TimeoutError) else str(error)
            finally:
                if tab is not None:
                    await self._close_temp(tab.target_id)
        result['elapsed'] = round(time.monotonic() - started, 2)
        return result

    async def crawl_one(self, target_url, idx, session_id, crawl_slots, *, max_text_units=None, fetch_timeout=3.0):
        """One page in a temporary tab. Research passes max_text_units and gets the capture
        metadata of research.capture with the text; the crawl command gets the whole text."""
        started = time.monotonic()
        tab = None

        def page(title, text='', error=None, **extra):
            return {'index': idx + 1, 'url': target_url, 'title': title, 'text': text, 'ok': error is None,
                    'error': error, 'chars': len(text), 'elapsed': round(time.monotonic() - started, 2),
                    'imageCandidates': [], 'imageCount': 0, **extra}

        async with crawl_slots, self.crawl_slots:
            try:
                try:
                    async with asyncio.timeout(fetch_timeout + 3):
                        tab = await self._temp_tab(session_id)
                        await tab.goto(target_url, timeout=fetch_timeout + 2)
                        await tab.wait_ready(max(1.0, fetch_timeout - 0.5), quiet=0.3, min_chars=21)
                        title = str(await tab.eval('document.title') or '').strip() or 'No Title'
                        is_pdf = await tab.eval('document.contentType') == 'application/pdf'
                        if is_pdf:
                            capture = {}
                        elif max_text_units is None:
                            text = await tab.eval("document.body ? document.body.innerText : ''", timeout=10)
                            capture = {'text': str(text or '').strip()}
                        else:
                            capture = await tab.eval(research_capture_js(max_text_units))
                except TimeoutError:
                    return page('Timeout', error=f'page took more than {fetch_timeout:g}s to load', browserSuspect=True)
                if is_pdf:
                    text = (await pdf_text(target_url))[:max_text_units]
                    return page(title, text, None if text.strip() else 'No extractable PDF text',
                                contentType='application/pdf', sourceChars=len(text))
                text = capture.pop('text')
                if is_challenge(title, text):
                    return page(title, error='Anti-bot / Cloudflare challenge page (access blocked)')
                status = await tab.http_status()
                if status >= 400:
                    return page(title, error=f'HTTP {status} error page')
                try:
                    candidates = await tab.eval(IMAGE_CANDIDATES_JS, timeout=1.0) or []
                except Exception:
                    candidates = []
                return page(title, text, None if len(text.strip()) > 20 else 'No readable text content extracted',
                            imageCandidates=candidates, imageCount=len(candidates), **capture)
            except FetchError as error:
                return page('Error', error=f'PDF download failed: {error}')
            except (PageError, CDPError) as error:
                return page('Error', error=str(error), browserSuspect=isinstance(error, (CDPTimeout, CDPClosed)))
            except Exception as error:
                return page('Error', error=f'{type(error).__name__}: {error}', browserSuspect=True)
            finally:
                if tab is not None:
                    await self._close_temp(tab.target_id)

    async def run_fetch_image(self, target_url, session_id):
        return await fetch_image(target_url, DOWNLOADS / safe_name(str(session_id), 80))

    async def crawl_command(self, remainder, session_id):
        urls = list(dict.fromkeys(url.rstrip('.,;)') for url in re.findall(r'https?://[^\s"\'\]\[<>]+', remainder)))
        if not urls:
            raise ValueError('usage: crawl <url1> [url2] ...')
        slots = asyncio.Semaphore(CRAWL_TABS)
        results = await asyncio.gather(*(self.crawl_one(url, i, session_id, slots, fetch_timeout=8.0)
                                         for i, url in enumerate(urls)))
        ok = [r for r in results if r['ok']]
        parts = [f"Crawled {len(ok)}/{len(urls)} pages ({sum(r['chars'] for r in results):,} characters)." if ok else
                 f'CRAWL FAILED: 0/{len(urls)} pages captured. Do not retry the same URLs right away.']
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
                'imageCount': sum(r['imageCount'] for r in results)}

    async def google_search_command(self, remainder, session_id):
        from research.jobs import resolve_google_redirect
        searches = json.loads(remainder)[:4]  # [{"direction", "query"}, ...] from index.ts
        groups = await asyncio.gather(*(self.search_one(s, i, session_id, asyncio.Semaphore(4))
                                        for i, s in enumerate(searches)))
        seen, selected = set(), []
        for rank in range(10):
            for group in groups:
                if rank < len(group['results']):
                    item = group['results'][rank]
                    url = await asyncio.to_thread(resolve_google_redirect, item['url'])
                    if url not in seen:
                        seen.add(url)
                        selected.append({**item, 'url': url, 'directions': [group['direction']],
                                         'query': group['query']})
        selected = selected[:10]
        if selected:
            text = '\n\n'.join([f'Google search results for {len(searches)} direction(s) '
                                '(Google via the shared browser, region Taiwan):'] + [
                f"### {n}. [{item['title']}]({item['url']})\n"
                f"*Direction: {', '.join(item['directions'])} | Query: {item['query']}*\n"
                f"{item['snippet'] or 'No snippet available.'}" for n, item in enumerate(selected, 1)])
        else:
            text = 'No Google search results found. ' + '; '.join(f"{g['direction']}: {g['error']}" for g in groups)
        ok = sum(1 for g in groups if g['ok'])
        return {'action': 'google-search', 'provider': 'google-nodriver-tw', 'text': text, 'results': selected,
                'searches': groups, 'successCount': ok, 'failedCount': len(groups) - ok, 'totalCount': len(groups)}


async def execute_request(service, request):
    session_id = str(request.get('sessionId') or 'default')
    command = str(request.get('command') or '')
    base = {'id': request.get('id'), 'sessionId': session_id}
    try:
        async with asyncio.timeout(COMMAND_TIMEOUT) as deadline:
            return {**base, 'ok': True, **await service.execute(command, session_id)}
    except StaleRef as error:
        return {**base, 'ok': False, 'error': f'STALE_REF: {error}'}
    except Exception as error:
        if isinstance(error, TimeoutError) and deadline.expired():
            log_event('command_timeout', command=loggable(command)[:200], session=session_id)
            return {**base, 'ok': False, 'error': f'Browser command timed out after {COMMAND_TIMEOUT:g} seconds'}
        return {**base, 'ok': False, 'error': f'{type(error).__name__}: {error}'}


async def server_main(socket_path):
    from research.jobs import ResearchConnection
    service = BrowserService()
    session_locks = {}
    research_slots = asyncio.Semaphore(2)
    research_connections = set()
    stop = asyncio.Event()
    path = Path(socket_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = path.with_name(path.name + '.lock').open('w')
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('another browser worker owns this socket; exiting', file=sys.stderr, flush=True)
        return
    # Read by the Xvfb streaming scripts to find this worker's display.
    env_info = path.parent / 'nodriver-browser.env'
    env_info.write_text(f"DISPLAY={os.environ.get('DISPLAY', '')}\nXAUTHORITY={os.environ.get('XAUTHORITY', '')}\n"
                        f'PID={os.getpid()}\n')
    path.unlink(missing_ok=True)

    async def handle_client(reader, writer):
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
            base = {'id': request_id, 'sessionId': session_id}
            try:
                if action == 'research':
                    result = await research.run(request_id, session_id, request.get('research'))
                    response = {**base, 'ok': True, 'type': 'result', **result}
                elif action == 'research-add-query':
                    research.add_query(session_id, request.get('research'))
                    response = {**base, 'ok': True, 'type': 'result'}
                elif action in LOCK_FREE:
                    response = await execute_request(service, request)
                else:
                    lock = session_locks.setdefault(session_id, asyncio.Lock())
                    async with lock:
                        response = await execute_request(service, request)
            except asyncio.CancelledError:
                response = {**base, 'ok': False, 'error': 'Browser command cancelled'}
            except Exception as error:
                message = f'{type(error).__name__}: {error}'
                if action == 'research':  # research reports a code, not the exception
                    message = ('research_cleanup_incomplete' if str(error) == 'research_cleanup_incomplete'
                               else 'research_failed')
                response = {**base, 'ok': False, 'error': message}
            finally:
                active.pop(request_id, None)
            await send(response)
            if response.get('action') == 'shutdown':
                stop.set()

        try:
            while line := await reader.readline():
                request = json.loads(line)
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
            with contextlib.suppress(Exception):
                await writer.wait_closed()

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
        # Pi processes keep an idle connection open; without this the exit waits for them.
        server.close_clients()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.gather(*(c.close() for c in tuple(research_connections)),
                                                  return_exceptions=True), 5)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(service.close(), 15)
        path.unlink(missing_ok=True)
        env_info.unlink(missing_ok=True)
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
        log_event('worker_stopped', pid=os.getpid())


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--server':
        sys.exit('usage: worker.py --server <socket>')
    asyncio.run(server_main(sys.argv[2]))
