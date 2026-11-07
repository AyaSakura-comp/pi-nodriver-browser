"""One browser tab driven over a flat CDP session.

Page scripts run in an isolated world ("pi"): the page cannot see or tamper with
them, and the Runtime domain is never enabled (a common automation tell).
"""
import asyncio
import base64
import contextlib
import json
import time
from pathlib import Path

from .cdp import CDPClosed, CDPError, CDPTimeout

JS_DIR = Path(__file__).with_name('js')
SNAPSHOT_JS = (JS_DIR / 'snapshot.js').read_text()

READY_JS = ("[document.readyState, document.body ? document.body.innerText.length : 0, "
            "document.getElementsByTagName('*').length, location.href]")

# Resolve a snapshot ref to viewport coordinates for a real mouse click.
TARGET_JS = r'''((ref) => {
  const el = window.__pi?.refs?.get(ref);
  if (!el) return {error: 'stale'};
  if (!el.isConnected) return {error: 'detached'};
  el.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
  const r = el.getBoundingClientRect();
  let x = r.left + r.width / 2, y = r.top + r.height / 2;
  let win = el.ownerDocument.defaultView;
  while (win && win.frameElement) {
    const f = win.frameElement.getBoundingClientRect();
    x += f.left; y += f.top; win = win.parent;
  }
  let hit = false;
  if (r.width > 0 && r.height > 0 && el.ownerDocument === document) {
    const at = document.elementFromPoint(x, y);
    hit = !!at && (at === el || el.contains(at) || (at.closest && at.closest('label') && at.closest('label').control === el));
  } else if (r.width > 0 && r.height > 0) {
    hit = true;
  }
  const tag = el.tagName.toLowerCase();
  return {x, y, hit, tag, type: (el.type || '').toLowerCase(), editable: el.isContentEditable};
})'''

JS_CLICK = r'''((ref) => {
  const el = window.__pi?.refs?.get(ref);
  if (!el || !el.isConnected) return false;
  el.click();
  return true;
})'''

FOCUS_JS = r'''((ref) => {
  const el = window.__pi?.refs?.get(ref);
  if (!el || !el.isConnected) return {error: 'stale'};
  el.scrollIntoView({block: 'center', behavior: 'instant'});
  el.focus();
  if (typeof el.select === 'function') { try { el.select(); } catch (e) {} }
  else if (el.isContentEditable) {
    const range = el.ownerDocument.createRange(); range.selectNodeContents(el);
    const sel = el.ownerDocument.getSelection(); sel.removeAllRanges(); sel.addRange(range);
  }
  const active = el.ownerDocument.activeElement;
  return {focused: active === el || el.contains(active), tag: el.tagName.toLowerCase(),
          type: (el.type || '').toLowerCase(), editable: el.isContentEditable};
})'''

VALUE_JS = r'''((ref) => {
  const el = window.__pi?.refs?.get(ref);
  if (!el) return null;
  return el.isContentEditable ? el.innerText : (el.value ?? null);
})'''

# Fallback for inputs that ignore inserted text: React-style native setter + events.
SET_VALUE_JS = r'''((ref, value) => {
  const el = window.__pi?.refs?.get(ref);
  if (!el) return false;
  const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
  setter.call(el, value);
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return true;
})'''

SELECT_JS = r'''((ref, wanted) => {
  const el = window.__pi?.refs?.get(ref);
  if (!el || !el.isConnected) return {error: 'stale'};
  if (el.tagName !== 'SELECT') return {error: 'not a <select>; click it, then click the option'};
  const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  // "R10 台北車站" vs "台北車站": compare without a leading code too.
  const core = (s) => norm(s).replace(/^[a-z]{0,3}\d{1,4}[a-z]?[\s\-.:]*/i, '');
  const w = norm(wanted), wc = core(wanted);
  const options = Array.from(el.options).filter((o) => !o.disabled);
  const tests = [
    (o) => norm(o.text) === w, (o) => norm(o.value) === w, (o) => wc && core(o.text) === wc,
    (o) => w && norm(o.text).includes(w), (o) => wc && core(o.text).includes(wc),
    (o) => core(o.text).length >= 2 && wc.includes(core(o.text)),
  ];
  let option = null;
  for (const test of tests) { option = options.find(test); if (option) break; }
  if (!option) {
    const chars = new Set(wc.replace(/\s/g, ''));
    const near = options.filter((o) => [...core(o.text)].filter((c) => chars.has(c)).length >= Math.min(2, chars.size));
    return {error: 'no option matches', options: (near.length ? near : options).map((o) => o.text.trim()).slice(0, 40)};
  }
  el.value = option.value;
  option.selected = true;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return {selected: option.text.trim()};
})'''

SCROLL_JS = r'''((where) => {
  const s = document.scrollingElement || document.documentElement;
  let best = s;
  if (s.scrollHeight <= innerHeight + 2) {
    let area = 0;
    for (const el of document.querySelectorAll('div,main,section,ul')) {
      if (el.scrollHeight <= el.clientHeight + 2) continue;
      const st = getComputedStyle(el);
      if (!/(auto|scroll)/.test(st.overflowY)) continue;
      const a = el.clientWidth * el.clientHeight;
      if (a > area) { area = a; best = el; }
    }
  }
  best.scrollTo({top: where === 'top' ? 0 : best.scrollHeight, behavior: 'instant'});
})'''

KEYS = {
    'enter': ('Enter', 'Enter', 13, '\r'),
    'tab': ('Tab', 'Tab', 9, None),
    'escape': ('Escape', 'Escape', 27, None),
    'esc': ('Escape', 'Escape', 27, None),
    'backspace': ('Backspace', 'Backspace', 8, None),
    'delete': ('Delete', 'Delete', 46, None),
    'space': (' ', 'Space', 32, ' '),
    'arrowdown': ('ArrowDown', 'ArrowDown', 40, None),
    'arrowup': ('ArrowUp', 'ArrowUp', 38, None),
    'arrowleft': ('ArrowLeft', 'ArrowLeft', 37, None),
    'arrowright': ('ArrowRight', 'ArrowRight', 39, None),
    'pagedown': ('PageDown', 'PageDown', 34, None),
    'pageup': ('PageUp', 'PageUp', 33, None),
    'home': ('Home', 'Home', 36, None),
    'end': ('End', 'End', 35, None),
}


class PageError(Exception):
    pass


class StaleRef(PageError):
    pass


class Tab:
    def __init__(self, chrome, target_id, session):
        self.chrome = chrome
        self.target_id = target_id
        self.session = session
        self.world = None
        self.dialogs = []
        self.created = time.monotonic()

    @classmethod
    async def attach(cls, chrome, target_id, *, timeout=5.0):
        result = await chrome.conn.send('Target.attachToTarget', {'targetId': target_id, 'flatten': True},
                                        timeout=timeout)
        tab = cls(chrome, target_id, result['sessionId'])
        chrome.conn.on('Page.javascriptDialogOpening', tab._on_dialog, session=tab.session)
        await tab.send('Page.enable', timeout=timeout)
        return tab

    def _on_dialog(self, params):
        # An open alert/confirm blocks the renderer and every later call would hang.
        # Alerts and leave-page prompts are accepted; confirm/prompt are dismissed (safe default).
        kind = params.get('type', 'alert')
        self.dialogs.append(f"{kind}: {str(params.get('message', ''))[:300]}")
        del self.dialogs[:-5]
        accept = kind in ('alert', 'beforeunload')
        asyncio.get_running_loop().create_task(self._answer_dialog(accept))

    async def _answer_dialog(self, accept):
        with contextlib.suppress(CDPError):
            await self.send('Page.handleJavaScriptDialog', {'accept': accept}, timeout=5)

    async def send(self, method, params=None, *, timeout=10.0):
        return await self.chrome.conn.send(method, params, session=self.session, timeout=timeout)

    async def _new_world(self, timeout):
        tree = await self.send('Page.getFrameTree', timeout=timeout)
        frame_id = tree['frameTree']['frame']['id']
        result = await self.send('Page.createIsolatedWorld', {
            'frameId': frame_id, 'worldName': 'pi', 'grantUniveralAccess': True}, timeout=timeout)
        return result['executionContextId']

    async def eval(self, expression, *, timeout=10.0):
        for attempt in (1, 2, 3):
            if self.world is None:
                self.world = await self._new_world(timeout)
            try:
                result = await self.send('Runtime.evaluate', {
                    'expression': expression, 'contextId': self.world,
                    'returnByValue': True}, timeout=timeout)
            except CDPError as error:
                # The isolated world dies with its document; make a new one.
                if 'context' in str(error).lower() and attempt < 3 and not isinstance(error, CDPTimeout):
                    self.world = None
                    await asyncio.sleep(0.1)
                    continue
                raise
            if 'exceptionDetails' in result:
                details = result['exceptionDetails']
                text = (details.get('exception') or {}).get('description') or details.get('text') or 'script error'
                raise PageError(text.splitlines()[0][:300])
            return (result.get('result') or {}).get('value')
        raise PageError('page kept replacing its document')

    async def call(self, function_source, *args, timeout=10.0):
        expression = f'({function_source})(' + ','.join(json.dumps(arg) for arg in args) + ')'
        return await self.eval(expression, timeout=timeout)

    async def goto(self, url, *, timeout=20.0):
        self.world = None
        result = await self.send('Page.navigate', {'url': url}, timeout=timeout)
        error = result.get('errorText')
        if error and error != 'net::ERR_ABORTED':
            raise PageError(f'{error} while loading {url}')

    async def wait_ready(self, timeout=10.0, *, quiet=0.5, min_chars=0, blank_ok=True):
        """Wait until the document is parsed and its text and element count stop changing
        for `quiet` seconds. Returns False at the deadline (the page may still be usable).
        blank_ok=False: a tab still on about:blank is not ready (a popup that has not
        started loading its URL yet looks complete and quiet)."""
        deadline = time.monotonic() + timeout
        last = None
        since = time.monotonic()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                state = await self.eval(READY_JS, timeout=max(0.5, min(3.0, remaining)))
            except CDPTimeout:
                state = None  # busy renderer; keep waiting until the deadline
            except (PageError, CDPError):
                if self.chrome.conn.closed:
                    raise CDPClosed('browser connection closed')
                state = None
            if state and state[0] != 'loading' and state[1] >= min_chars and (blank_ok or state[3] != 'about:blank'):
                fingerprint = (state[1], state[2], state[3])
                if fingerprint == last:
                    if time.monotonic() - since >= quiet:
                        return True
                else:
                    last, since = fingerprint, time.monotonic()
            await asyncio.sleep(0.15)

    async def http_status(self):
        try:
            return await self.eval("(performance.getEntriesByType('navigation')[0] || {}).responseStatus || 0",
                                   timeout=3)
        except (CDPError, PageError):
            return 0

    async def snapshot(self, *, from_viewport=False):
        return await self.call(SNAPSHOT_JS, from_viewport)

    async def click_ref(self, ref):
        target = await self.call(TARGET_JS, ref)
        if not target or target.get('error'):
            raise StaleRef(f'{ref} is not on the page any more; take a new snapshot')
        if target['hit']:
            x, y = target['x'], target['y']
            await self.send('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x, 'y': y}, timeout=5)
            for kind in ('mousePressed', 'mouseReleased'):
                await self.send('Input.dispatchMouseEvent', {
                    'type': kind, 'x': x, 'y': y, 'button': 'left', 'clickCount': 1}, timeout=5)
            return 'mouse'
        # Covered (overlay) or zero-sized (styled checkbox): click the element itself.
        if not await self.call(JS_CLICK, ref):
            raise StaleRef(f'{ref} is not on the page any more; take a new snapshot')
        return 'js'

    async def type_ref(self, ref, text, *, submit=False):
        focus = await self.call(FOCUS_JS, ref)
        if not focus or focus.get('error'):
            raise StaleRef(f'{ref} is not on the page any more; take a new snapshot')
        if not focus.get('focused'):
            await self.click_ref(ref)
            await self.call(FOCUS_JS, ref)
        if text:
            await self.send('Input.insertText', {'text': text}, timeout=5)
        else:
            await self.press('Backspace')
        value = await self.call(VALUE_JS, ref)
        if text and isinstance(value, str) and text not in value and focus.get('tag') in ('input', 'textarea'):
            await self.call(SET_VALUE_JS, ref, text)
        if submit:
            await self.press('Enter')

    async def select_ref(self, ref, option):
        result = await self.call(SELECT_JS, ref, option)
        if not result or result.get('error') == 'stale':
            raise StaleRef(f'{ref} is not on the page any more; take a new snapshot')
        if result.get('error'):
            options = result.get('options')
            raise PageError(result['error'] + (f"; options: {' | '.join(options)}" if options else ''))
        return result['selected']

    async def press(self, key):
        spec = KEYS.get(key.lower().replace('_', '').replace(' ', ''))
        if spec is None:
            if len(key) == 1:
                await self.send('Input.insertText', {'text': key}, timeout=5)
                return
            raise PageError(f'unknown key {key!r}; use one of: ' + ', '.join(sorted(KEYS)))
        name, code, keycode, text = spec
        down = {'type': 'keyDown' if text else 'rawKeyDown', 'key': name, 'code': code,
                'windowsVirtualKeyCode': keycode, 'nativeVirtualKeyCode': keycode}
        if text:
            down['text'] = text
            down['unmodifiedText'] = text
        await self.send('Input.dispatchKeyEvent', down, timeout=5)
        await self.send('Input.dispatchKeyEvent', {
            'type': 'keyUp', 'key': name, 'code': code,
            'windowsVirtualKeyCode': keycode, 'nativeVirtualKeyCode': keycode}, timeout=5)

    async def scroll(self, direction):
        direction = direction.lower()
        if direction in ('top', 'bottom'):
            await self.call(SCROLL_JS, direction)
            return
        size = await self.eval('[innerWidth, innerHeight]', timeout=5) or [1280, 640]
        delta = size[1] * 0.8 * (-1 if direction == 'up' else 1)
        # A wheel event at the centre scrolls whatever scroller is there (inner panes too)
        # and triggers lazy loading the way a user does.
        await self.send('Input.dispatchMouseEvent', {
            'type': 'mouseWheel', 'x': size[0] / 2, 'y': size[1] / 2, 'deltaX': 0, 'deltaY': delta}, timeout=5)
        await asyncio.sleep(0.3)

    async def back(self):
        """Go back one history entry; False when this is the tab's first page."""
        history = await self.send('Page.getNavigationHistory', timeout=5)
        index = history['currentIndex'] - 1
        if index < 0:
            return False
        self.world = None
        await self.send('Page.navigateToHistoryEntry', {'entryId': history['entries'][index]['id']}, timeout=10)
        return True

    async def screenshot(self, path):
        await self.send('Page.bringToFront', timeout=5)
        result = await self.send('Page.captureScreenshot', {'format': 'jpeg', 'quality': 70}, timeout=15)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(result['data']))
        return path

    async def close(self):
        self.chrome.conn.drop_session(self.session)
        await self.chrome.close_target(self.target_id)
