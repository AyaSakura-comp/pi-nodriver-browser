#!/usr/bin/env python3
import asyncio
import fcntl
import json
import logging
import os
import signal
import sys
import tempfile
import urllib.request
from pathlib import Path

import nodriver as uc

from browser_logic import format_snapshot, parse_command, parse_devtools_active_port, parse_dismiss_options, resolve_browser_executable, resolve_profile_dir, should_disable_sandbox

MARKER = '__PI_NODRIVER__'
logging.basicConfig(level=logging.CRITICAL)

DISMISS_OVERLAY_JS = r'''JSON.stringify(((policy) => {
  document.querySelectorAll('[data-pi-dismiss-ref]').forEach(el => el.removeAttribute('data-pi-dismiss-ref'));

  const visible = el => {
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
  };
  const normalize = value => (value || '').toLowerCase()
    .replace(/[\\s,，.!！。:：;；_\\-]+/g, '');
  const label = el => (el.innerText || el.textContent || el.value ||
    el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
  const matches = (value, patterns) => {
    const normalized = normalize(value);
    return patterns.some(pattern => {
      const expected = normalize(pattern);
      const mayContain = expected.length >= 3 || /[^\\x00-\\x7f]/.test(expected);
      return normalized === expected || (mayContain && normalized.includes(expected));
    });
  };
  const controls = container => Array.from(container.querySelectorAll(
    'button,a,input[type="button"],input[type="submit"],[role="button"],[aria-label],[class*="close" i]'
  )).filter(visible);

  const containers = Array.from(new Set(Array.from(document.querySelectorAll(
    'dialog,[role="dialog"],[aria-modal="true"],[class*="modal" i],[id*="modal" i],' +
    '[class*="popup" i],[id*="popup" i],[class*="overlay" i],[id*="overlay" i],' +
    '[class*="cookie" i],[id*="cookie" i],[class*="consent" i],[id*="consent" i]'
  )).filter(visible)));

  const cookieWords = ['cookie', 'cookies', '餅乾', 'クッキー', '쿠키'];
  const cookieContainers = containers.filter(container => matches(container.innerText || container.textContent, cookieWords));
  const otherContainers = containers.filter(container => !cookieContainers.includes(container));
  const acceptCookie = ['同意', '接受全部', '全部接受', '我同意', 'acceptall', 'allowall', 'agree', 'gotit', 'ok'];
  const rejectCookie = ['拒絕非必要', '僅必要', '只接受必要', '只允許必要', 'rejectall', 'declineall', 'necessaryonly', 'essentialonly'];
  const declineMarketing = ['不用謝謝', '不用，謝謝', '不需要謝謝', '稍後', '暫時不要', 'nothanks', 'notnow', 'maybelater', 'skip'];
  const closeWords = ['關閉', 'close', 'dismiss', '×', '✕', 'x'];

  let candidate = null;
  if (policy !== 'ignore') {
    const cookiePatterns = policy === 'accept' ? acceptCookie : rejectCookie;
    for (const container of cookieContainers) {
      const element = controls(container).find(el => matches(label(el), cookiePatterns));
      if (element) {
        candidate = { element, kind: 'cookie', label: label(element) };
        break;
      }
    }
  }

  if (!candidate) {
    for (const container of otherContainers) {
      const available = controls(container);
      const element = available.find(el => matches(label(el), declineMarketing)) ||
        available.find(el => matches(label(el), closeWords));
      if (element) {
        candidate = { element, kind: 'overlay', label: label(element) };
        break;
      }
    }
  }

  if (!candidate) return { candidate: null, overlayCount: containers.length };
  candidate.element.setAttribute('data-pi-dismiss-ref', 'active');
  return {
    candidate: { ref: 'active', kind: candidate.kind, label: candidate.label },
    overlayCount: containers.length
  };
})(__PI_COOKIE_POLICY__))'''

SNAPSHOT_JS = r'''JSON.stringify((() => {
  const seen = new Set();
  const elements = [];
  const semanticSelector = 'a,button,input,textarea,select,summary,details,label,' +
    '[role="button"],[role="link"],[role="menuitem"],[role="option"],[role="tab"],' +
    '[role="checkbox"],[role="radio"],[role="switch"],[contenteditable="true"]';

  const visible = el => {
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
  };
  const interactive = el => {
    if (el.matches(semanticSelector)) return true;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
    const style = getComputedStyle(el);
    const pointerCursor = ['pointer', 'grab', 'zoom-in'].includes(style.cursor) &&
      (!el.parentElement || getComputedStyle(el.parentElement).cursor !== style.cursor);
    return typeof el.onclick === 'function' || el.hasAttribute('onclick') ||
      el.tabIndex >= 0 || pointerCursor || el.hasAttribute('data-action') ||
      el.hasAttribute('data-testid') && /button|link|submit|cart|checkout|action/i.test(el.getAttribute('data-testid'));
  };
  const visit = root => {
    try {
      root.querySelectorAll('[data-pi-ref]').forEach(el => el.removeAttribute('data-pi-ref'));
      root.querySelectorAll('*').forEach(el => {
        if (!seen.has(el) && visible(el) && interactive(el)) {
          seen.add(el);
          elements.push(el);
        }
        if (el.shadowRoot) visit(el.shadowRoot);
        if (el.tagName === 'IFRAME') {
          try { if (el.contentDocument) visit(el.contentDocument); } catch (_) {}
        }
      });
    } catch (_) {}
  };
  visit(document);

  return elements.map((el, index) => {
    const ref = `e${index + 1}`;
    el.setAttribute('data-pi-ref', ref);
    return {
      ref,
      tag: el.tagName.toLowerCase(),
      text: (el.innerText || el.textContent || '').trim(),
      value: el.value || '',
      href: el.href || '',
      placeholder: el.getAttribute('placeholder') || '',
      ariaLabel: el.getAttribute('aria-label') || ''
    };
  });
})())'''

CLICK_TARGET_JS = r'''JSON.stringify(((request) => {
  const visible = el => {
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
  };
  const semanticSelector = 'a,button,input,textarea,select,summary,details,label,' +
    '[role="button"],[role="link"],[role="menuitem"],[role="option"],[role="tab"],' +
    '[role="checkbox"],[role="radio"],[role="switch"],[contenteditable="true"]';
  const interactive = el => el.matches(semanticSelector) || typeof el.onclick === 'function' ||
    el.hasAttribute('onclick') || el.tabIndex >= 0 || ['pointer', 'grab', 'zoom-in'].includes(getComputedStyle(el).cursor);
  const normalize = value => (value || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const entries = [];

  const visit = (root, offsetX = 0, offsetY = 0, frames = []) => {
    let all = [];
    try { all = Array.from(root.querySelectorAll('*')); } catch (_) { return; }
    for (const el of all) {
      if (visible(el)) entries.push({ el, offsetX, offsetY, frames });
      if (el.shadowRoot) visit(el.shadowRoot, offsetX, offsetY, frames);
      if (el.tagName === 'IFRAME') {
        try {
          const rect = el.getBoundingClientRect();
          if (el.contentDocument) visit(el.contentDocument, offsetX + rect.left, offsetY + rect.top, [...frames, el]);
        } catch (_) {}
      }
    }
  };
  visit(document);

  let match = null;
  if (request.kind === 'ref') {
    match = entries.find(item => item.el.getAttribute('data-pi-ref') === request.value) || null;
  } else if (request.kind === 'css') {
    match = entries.find(item => {
      try { return item.el.matches(request.value); } catch (_) { return false; }
    }) || null;
  } else if (request.kind === 'text') {
    const wanted = normalize(request.value);
    const candidates = entries.filter(item => {
      const el = item.el;
      const label = normalize(el.innerText || el.textContent || el.value ||
        el.getAttribute('aria-label') || el.getAttribute('title'));
      return label === wanted || label.includes(wanted);
    });
    candidates.sort((a, b) => {
      const aText = normalize(a.el.innerText || a.el.textContent || a.el.value || a.el.getAttribute('aria-label'));
      const bText = normalize(b.el.innerText || b.el.textContent || b.el.value || b.el.getAttribute('aria-label'));
      const aScore = (aText === wanted ? 0 : 1000) + (interactive(a.el) ? 0 : 100) + aText.length;
      const bScore = (bText === wanted ? 0 : 1000) + (interactive(b.el) ? 0 : 100) + bText.length;
      return aScore - bScore;
    });
    match = candidates[0] || null;
  }
  if (!match) return { found: false };

  for (const frame of match.frames) frame.scrollIntoView({ block: 'center', inline: 'center' });
  match.el.scrollIntoView({ block: 'center', inline: 'center' });
  const rect = match.el.getBoundingClientRect();
  const currentOffset = match.frames.reduce((offset, frame) => {
    const frameRect = frame.getBoundingClientRect();
    return { x: offset.x + frameRect.left, y: offset.y + frameRect.top };
  }, { x: 0, y: 0 });
  return {
    found: true,
    x: currentOffset.x + rect.left + rect.width / 2,
    y: currentOffset.y + rect.top + rect.height / 2,
    tag: match.el.tagName.toLowerCase(),
    text: (match.el.innerText || match.el.textContent || match.el.value || '').trim()
  };
})(__PI_CLICK_REQUEST__))'''


class BrowserWorker:
    def __init__(self):
        self.browser = None
        self.launched_browser = None
        self.pages = {}

    async def ensure_browser(self):
        if self.browser is None:
            profile = resolve_profile_dir()
            profile.mkdir(parents=True, exist_ok=True)
            try:
                self.browser = await uc.start(
                    headless=False,
                    browser_executable_path=resolve_browser_executable(),
                    user_data_dir=str(profile),
                    browser_args=['--window-size=1280,720', '--no-first-run', '--no-default-browser-check'],
                    sandbox=not should_disable_sandbox(),
                    lang='zh-TW',
                )
                self.launched_browser = self.browser
            except Exception as startup_error:
                # Nodriver 0.50.x waits less than three seconds for DevTools.
                # On cold CI machines Chrome can become ready just after that
                # deadline, so reconnect to the process Nodriver already started.
                active_port_file = profile / 'DevToolsActivePort'
                candidates = [
                    browser for browser in uc.util.get_registered_instances()
                    if getattr(browser.config, 'port', None)
                    and Path(browser.config.user_data_dir) == profile
                ]
                if candidates:
                    self.launched_browser = max(candidates, key=lambda browser: browser._process_pid or 0)
                for _ in range(150):
                    await asyncio.sleep(0.1)
                    try:
                        if self.launched_browser is not None:
                            port = self.launched_browser.config.port
                        else:
                            port = parse_devtools_active_port(active_port_file.read_text())
                        with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version', timeout=0.2) as response:
                            if not json.load(response).get('webSocketDebuggerUrl'):
                                continue
                        self.browser = await uc.start(host='127.0.0.1', port=port)
                        break
                    except Exception:
                        continue
                if self.browser is None:
                    raise startup_error
        return self.browser

    async def shutdown_browser(self):
        if self.browser is not None:
            try:
                await self.browser.send(uc.cdp.browser.close())
            except Exception:
                pass
            self.browser.stop()
            if self.launched_browser is not None and self.launched_browser is not self.browser:
                self.launched_browser.stop()
            self.browser = None
            self.launched_browser = None
            self.pages.clear()

    async def require_page(self, session_id):
        page = self.pages.get(session_id)
        if page is None:
            raise ValueError('this Pi session has no open page; run open <url> first')
        return page

    async def element(self, session_id, ref):
        page = await self.require_page(session_id)
        normalized = ref.removeprefix('@')
        element = await page.select(f'[data-pi-ref="{normalized}"]')
        if not element:
            raise ValueError(f'element {ref} not found; run snapshot -i again')
        return element

    async def resolve_click_target(self, page, kind, value):
        request = json.dumps({'kind': kind, 'value': value}, ensure_ascii=False)
        script = CLICK_TARGET_JS.replace('__PI_CLICK_REQUEST__', request)
        result = json.loads(await page.evaluate(script))
        if not result.get('found'):
            if kind == 'ref':
                raise ValueError(f'element @{value} not found; run snapshot -i again')
            raise ValueError(f'click target not found by {kind}: {value}')
        return result

    async def native_click(self, page, x, y):
        before_tabs = len(self.browser.tabs)
        await page.bring_to_front()
        await page.mouse_click(float(x), float(y))
        await page.sleep(2)
        if len(self.browser.tabs) > before_tabs:
            page = self.browser.tabs[-1]
            await page.bring_to_front()
            await page.sleep(2)
        return page

    async def execute(self, command, session_id='default'):
        parts = parse_command(command)
        action = parts[0].lower()

        if action == 'open':
            if len(parts) != 2:
                raise ValueError('usage: open <url>')
            browser = await self.ensure_browser()
            previous = self.pages.get(session_id)
            page = await browser.get(parts[1], new_tab=True)
            self.pages[session_id] = page
            if previous is not None:
                try:
                    await previous.close()
                except Exception:
                    pass
            await page.sleep(2)
            return {'text': f'Opened {page.url or parts[1]}', 'action': action, 'url': page.url or parts[1]}

        if action == 'snapshot':
            page = await self.require_page(session_id)
            elements = json.loads(await page.evaluate(SNAPSHOT_JS))
            return {'text': format_snapshot(elements or []), 'action': action, 'count': len(elements or [])}

        if action == 'dismiss':
            policy = parse_dismiss_options(parts)
            page = await self.require_page(session_id)
            dismissed = []
            remaining = 0
            script = DISMISS_OVERLAY_JS.replace('__PI_COOKIE_POLICY__', json.dumps(policy))
            for _ in range(8):
                result = json.loads(await page.evaluate(script))
                remaining = result.get('overlayCount', 0)
                candidate = result.get('candidate')
                if not candidate:
                    break
                element = await page.select('[data-pi-dismiss-ref="active"]')
                if not element:
                    break
                await page.bring_to_front()
                await element.scroll_into_view()
                await page.sleep(0.2)
                await element.mouse_click()
                dismissed.append(candidate)
                await page.sleep(0.8)
            if dismissed:
                summary = '; '.join(f"{item['kind']}: {item['label']}" for item in dismissed)
                text = f'Dismissed {len(dismissed)} overlay control(s): {summary}'
            else:
                text = f'No matching overlay controls found (cookie policy: {policy})'
            if remaining:
                text += f'\nVisible overlay containers remaining: {remaining}'
            return {'text': text, 'action': action, 'dismissed': dismissed, 'cookiePolicy': policy}

        if action == 'click':
            page = await self.require_page(session_id)
            if len(parts) == 3:
                try:
                    x, y = float(parts[1]), float(parts[2])
                except ValueError as error:
                    raise ValueError('usage: click <@ref> or click <x> <y>') from error
                page = await self.native_click(page, x, y)
                self.pages[session_id] = page
                return {'text': f'Clicked viewport coordinates ({x:g}, {y:g})\nURL: {page.url}', 'action': action, 'url': page.url}
            if len(parts) != 2 or not parts[1].startswith('@'):
                raise ValueError('usage: click <@ref> or click <x> <y>')
            normalized = parts[1].removeprefix('@')
            target = await self.resolve_click_target(page, 'ref', normalized)
            page = await self.native_click(page, target['x'], target['y'])
            self.pages[session_id] = page
            return {'text': f'Clicked {parts[1]} ({target.get("tag", "element")}: {target.get("text", "")[:120]})\nURL: {page.url}', 'action': action, 'url': page.url}

        if action in ('click-text', 'click-css'):
            if len(parts) < 2:
                raise ValueError(f'usage: {action} <text-or-selector>')
            page = await self.require_page(session_id)
            value = ' '.join(parts[1:])
            kind = 'text' if action == 'click-text' else 'css'
            target = await self.resolve_click_target(page, kind, value)
            page = await self.native_click(page, target['x'], target['y'])
            self.pages[session_id] = page
            return {'text': f'Clicked by {kind} "{value}" ({target.get("tag", "element")}: {target.get("text", "")[:120]})\nURL: {page.url}', 'action': action, 'url': page.url}

        if action == 'click-js':
            if len(parts) != 2 or not parts[1].startswith('@'):
                raise ValueError('usage: click-js <@ref>')
            page = await self.require_page(session_id)
            normalized = parts[1].removeprefix('@')
            target = await self.resolve_click_target(page, 'ref', normalized)
            script = f'''(() => {{
                const element = document.elementFromPoint({float(target['x'])}, {float(target['y'])});
                if (!element) return false;
                setTimeout(() => element.click(), 0);
                return true;
            }})()'''
            if not await page.evaluate(script):
                raise ValueError(f'element {parts[1]} is not clickable at its current coordinates')
            return {
                'text': f'Deferred DOM click dispatched for {parts[1]} ({target.get("tag", "element")}: {target.get("text", "")[:120]})',
                'action': action,
                'url': page.url,
            }

        if action in ('fill', 'type'):
            if len(parts) < 3:
                raise ValueError(f'usage: {action} <@ref> <text>')
            element = await self.element(session_id, parts[1])
            text = ' '.join(parts[2:])
            await element.focus()
            if action == 'fill':
                await element.clear_input()
            await element.send_keys(text)
            return {'text': f'{action.title()}d {parts[1]}', 'action': action}

        if action == 'select':
            if len(parts) < 3:
                raise ValueError('usage: select <@ref> <value>')
            page = await self.require_page(session_id)
            select_element = await self.element(session_id, parts[1])
            wanted = ' '.join(parts[2:])
            options = await select_element.query_selector_all('option')
            match = next((o for o in options if wanted == (o.text_all or '').strip() or wanted == (o.attrs or {}).get('value')), None)
            if not match:
                match = next((o for o in options if wanted.lower() in (o.text_all or '').strip().lower()), None)
            if not match:
                raise ValueError(f'option not found: {wanted}')
            await match.select_option()
            await page.sleep(2)
            return {'text': f'Selected "{(match.text_all or wanted).strip()}"', 'action': action}

        if action == 'press':
            if len(parts) != 2:
                raise ValueError('usage: press <key>')
            key_map = {'enter': '\n', 'tab': '\t', 'space': ' ', 'backspace': '\b'}
            key = key_map.get(parts[1].lower(), parts[1])
            page = await self.require_page(session_id)
            focused = await page.select(':focus') or await page.select('body')
            await focused.send_keys(key)
            await page.sleep(1)
            return {'text': f'Pressed {parts[1]}', 'action': action}

        if action == 'scroll':
            page = await self.require_page(session_id)
            direction = parts[1].lower() if len(parts) > 1 else 'down'
            amount = int(parts[2]) if len(parts) > 2 else 600
            if direction == 'down':
                await page.scroll_down(amount)
            elif direction == 'up':
                await page.scroll_up(amount)
            elif direction in ('left', 'right'):
                delta = amount if direction == 'right' else -amount
                await page.evaluate(f'window.scrollBy({delta}, 0)')
            else:
                raise ValueError('scroll direction must be up, down, left, or right')
            return {'text': f'Scrolled {direction} {amount}px', 'action': action}

        if action == 'get':
            if len(parts) < 2:
                raise ValueError('usage: get text|url|title [@ref]')
            page = await self.require_page(session_id)
            kind = parts[1].lower()
            if kind == 'url':
                text = page.url
            elif kind == 'title':
                text = await page.evaluate('document.title')
            elif kind == 'text' and len(parts) > 2:
                text = (await self.element(session_id, parts[2])).text_all or ''
            elif kind == 'text':
                text = await page.evaluate('document.body.innerText')
            else:
                raise ValueError('usage: get text|url|title [@ref]')
            return {'text': str(text).strip(), 'action': action}

        if action == 'wait':
            page = await self.require_page(session_id)
            target = parts[1] if len(parts) > 1 else '1000'
            if target.startswith('@'):
                for _ in range(100):
                    try:
                        await self.element(session_id, target)
                        return {'text': f'Element {target} is available', 'action': action}
                    except ValueError:
                        await page.sleep(0.1)
                raise TimeoutError(f'timed out waiting for {target}')
            await page.sleep(int(target) / 1000)
            return {'text': f'Waited {target}ms', 'action': action}

        if action == 'screenshot':
            page = await self.require_page(session_id)
            full_page = '--full' in parts[1:]
            output_dir = Path(tempfile.mkdtemp(prefix='pi-nodriver-shot-'))
            output = output_dir / 'screenshot.png'
            screenshot_timeout = float(os.environ.get('PI_NODRIVER_SCREENSHOT_TIMEOUT', '30'))
            try:
                await asyncio.wait_for(
                    page.save_screenshot(output, format='png', full_page=full_page),
                    timeout=screenshot_timeout,
                )
            except TimeoutError as error:
                raise TimeoutError(f'screenshot timed out after {screenshot_timeout:g} seconds') from error
            return {'text': f'Screenshot saved: {output}', 'action': action, 'screenshotPath': str(output)}

        if action == 'close':
            page = self.pages.pop(session_id, None)
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass
            return {'text': 'Current Pi session tab closed', 'action': action}

        if action == 'shutdown':
            await self.shutdown_browser()
            return {'text': 'Browser daemon shutting down', 'action': action}

        raise ValueError(f'unsupported browser command: {action}')

    async def close(self):
        await self.shutdown_browser()


async def execute_request(worker, request):
    session_id = str(request.get('sessionId') or 'default')
    command_timeout = float(os.environ.get('PI_NODRIVER_COMMAND_TIMEOUT', '75'))
    try:
        result = await asyncio.wait_for(
            worker.execute(request.get('command', ''), session_id=session_id),
            timeout=command_timeout,
        )
        return {'id': request.get('id'), 'sessionId': session_id, 'ok': True, **result}
    except TimeoutError:
        return {
            'id': request.get('id'),
            'sessionId': session_id,
            'ok': False,
            'error': f'Browser command timed out after {command_timeout:g} seconds',
        }
    except Exception as error:
        return {'id': request.get('id'), 'sessionId': session_id, 'ok': False, 'error': f'{type(error).__name__}: {error}'}


async def stdio_main():
    worker = BrowserWorker()
    try:
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            response = await execute_request(worker, json.loads(line))
            print(MARKER + json.dumps(response, ensure_ascii=False), flush=True)
            if response.get('action') == 'shutdown':
                break
    finally:
        await worker.close()


async def server_main(socket_path):
    worker = BrowserWorker()
    session_locks = {}
    browser_structure_lock = asyncio.Lock()
    stop = asyncio.Event()
    path = Path(socket_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = path.with_name(path.name + '.lock').open('a+')
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return
    path.unlink(missing_ok=True)

    async def handle_client(reader, writer):
        active_tasks = {}
        write_lock = asyncio.Lock()

        async def send_response(response):
            async with write_lock:
                writer.write((MARKER + json.dumps(response, ensure_ascii=False) + '\n').encode())
                await writer.drain()

        async def process_request(request):
            request_id = request.get('id')
            session_id = str(request.get('sessionId') or 'default')
            command = str(request.get('command') or '').strip()
            action = command.split(maxsplit=1)[0].lower() if command else ''
            session_lock = session_locks.setdefault(session_id, asyncio.Lock())
            try:
                if action == 'shutdown':
                    async with browser_structure_lock:
                        response = await execute_request(worker, request)
                else:
                    async with session_lock:
                        if action in {'open', 'click', 'click-text', 'click-css', 'click-js', 'close'}:
                            async with browser_structure_lock:
                                response = await execute_request(worker, request)
                        else:
                            response = await execute_request(worker, request)
            except asyncio.CancelledError:
                response = {
                    'id': request_id,
                    'sessionId': session_id,
                    'ok': False,
                    'error': 'Browser command cancelled',
                }
            finally:
                active_tasks.pop(request_id, None)
            await send_response(response)
            if response.get('action') == 'shutdown':
                stop.set()
                writer.close()

        try:
            while line := await reader.readline():
                request = json.loads(line)
                request_id = request.get('id')
                cancel_id = request.get('cancelId')
                if cancel_id is not None:
                    task = active_tasks.get(cancel_id)
                    if task is not None:
                        task.cancel()
                    await send_response({
                        'id': request_id,
                        'sessionId': str(request.get('sessionId') or 'default'),
                        'ok': True,
                        'action': 'cancel',
                        'text': f'Cancellation requested for browser command {cancel_id}',
                    })
                    continue
                task = asyncio.create_task(process_request(request))
                active_tasks[request_id] = task
        except Exception as error:
            await send_response({'id': None, 'ok': False, 'error': f'{type(error).__name__}: {error}'})
        finally:
            if active_tasks:
                await asyncio.gather(*tuple(active_tasks.values()), return_exceptions=True)
            writer.close()
            await writer.wait_closed()

    loop = asyncio.get_running_loop()
    for signal_value in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signal_value, stop.set)
        except NotImplementedError:
            pass

    server = await asyncio.start_unix_server(handle_client, path=str(path))
    path.chmod(0o600)
    try:
        async with server:
            await stop.wait()
    finally:
        server.close()
        await server.wait_closed()
        await worker.close()
        path.unlink(missing_ok=True)
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--server':
        uc.loop().run_until_complete(server_main(sys.argv[2]))
    else:
        uc.loop().run_until_complete(stdio_main())
