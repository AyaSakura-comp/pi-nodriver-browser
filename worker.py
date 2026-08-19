#!/usr/bin/env python3
import asyncio
import fcntl
import hashlib
import json
import logging
import mimetypes
import os
import re
import signal
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

import nodriver as uc

from browser_logic import format_snapshot, parse_command, parse_devtools_active_port, parse_dismiss_options, resolve_browser_executable, resolve_profile_dir, should_disable_sandbox

MARKER = '__PI_NODRIVER__'
logging.basicConfig(level=logging.CRITICAL)


class StaleRefError(ValueError):
    def __init__(self, ref):
        self.ref = ref
        super().__init__(f'element {ref} not found; run snapshot -i again')


# Commands that observe the page without changing it. Repeating one of these
# verbatim cannot produce new information, so an identical repeat is a loop.
NON_PROGRESSING_ACTIONS = {'wait', 'snapshot', 'screenshot', 'get', 'downloads', 'download-info'}
REPEAT_LIMIT = 3


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
      Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0 &&
      rect.bottom > 0 && rect.right > 0 && rect.top < innerHeight && rect.left < innerWidth;
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
      download: el.getAttribute('download') || '',
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
    text: (match.el.innerText || match.el.textContent || match.el.value || '').trim(),
    href: match.el.href || match.el.closest?.('a')?.href || '',
    download: match.el.getAttribute?.('download') || match.el.closest?.('a')?.getAttribute?.('download') || ''
  };
})(__PI_CLICK_REQUEST__))'''


class BrowserWorker:
    def __init__(self):
        self.browser = None
        self.launched_browser = None
        self.pages = {}
        self.popup_openers = {}
        self.popup_just_switched = set()
        self.popup_just_closed = set()
        self.snapshot_required_sessions = set()
        self.repeated_commands = {}
        configured_download_dir = os.environ.get('PI_NODRIVER_DOWNLOAD_DIR')
        self.download_dir = (
            Path(configured_download_dir).expanduser()
            if configured_download_dir else Path.home() / '.pi' / 'agent' / 'nodriver-downloads'
        )
        self.downloads = {}
        self.download_frame_sessions = {}
        self.download_target_sessions = {}
        self.download_route_session = 'default'

    async def ensure_browser(self):
        if self.browser is None:
            profile = resolve_profile_dir()
            profile.mkdir(parents=True, exist_ok=True)
            try:
                self.browser = await uc.start(
                    headless=False,
                    browser_executable_path=resolve_browser_executable(),
                    user_data_dir=str(profile),
                    browser_args=['--window-size=1600,1000', '--no-first-run', '--no-default-browser-check'],
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
            self.download_dir.mkdir(parents=True, exist_ok=True)
            self.browser.add_handler(uc.cdp.target.TargetCreated, self.on_target_created)
            self.browser.add_handler(uc.cdp.browser.DownloadWillBegin, self.on_download_will_begin)
            self.browser.add_handler(uc.cdp.browser.DownloadProgress, self.on_download_progress)
            await self.browser.send(uc.cdp.browser.set_download_behavior(
                'allow', download_path=str(self.download_dir), events_enabled=True
            ))
        return self.browser

    @staticmethod
    def path_has_symlink_component(path):
        path = Path(path).expanduser().absolute()
        return any(
            component.exists() and component.is_symlink()
            for component in (path, *path.parents)
        )

    def session_download_dir(self, session_id='default'):
        root = self.download_dir.expanduser().absolute()
        if self.path_has_symlink_component(root):
            raise ValueError('configured download directory cannot contain a symlink')
        root.mkdir(parents=True, exist_ok=True)
        if session_id is None:
            path = root / '.quarantine'
        elif session_id == 'default':
            path = root
        else:
            digest = hashlib.sha256(session_id.encode()).hexdigest()[:24]
            path = root / digest
        if path.is_symlink():
            raise ValueError('session download directory cannot be a symlink')
        path.mkdir(parents=True, exist_ok=True)
        if path != root and path.resolve().parent != root.resolve():
            raise ValueError('session download directory escaped the configured root')
        return path

    async def configure_download_session(self, session_id, page=None):
        download_dir = self.session_download_dir(session_id)
        self.download_route_session = session_id
        await self.browser.send(uc.cdp.browser.set_download_behavior(
            'allow', download_path=str(download_dir), events_enabled=True
        ))
        if page is None:
            return
        self.download_target_sessions[str(page.target.target_id)] = session_id
        page.add_handler(uc.cdp.page.FrameAttached, self.on_frame_attached)
        try:
            frame_tree = await page.send(uc.cdp.page.get_frame_tree())
        except Exception:
            return

        def register(tree):
            self.download_frame_sessions[str(tree.frame.id_)] = session_id
            for child in tree.child_frames or []:
                register(child)

        register(frame_tree)

    def on_frame_attached(self, event):
        session_id = self.download_frame_sessions.get(str(event.parent_frame_id))
        if session_id is not None:
            self.download_frame_sessions[str(event.frame_id)] = session_id

    def on_target_created(self, event):
        target = event.target_info
        session_id = self.download_target_sessions.get(str(target.opener_id))
        if session_id is not None:
            self.download_target_sessions[str(target.target_id)] = session_id
            self.download_frame_sessions[str(target.target_id)] = session_id

    def on_download_will_begin(self, event):
        frame_id = str(event.frame_id)
        session_id = self.download_frame_sessions.get(
            frame_id,
            self.download_target_sessions.get(frame_id),
        )
        self.downloads[event.guid] = {
            'guid': event.guid,
            'sessionId': session_id,
            'url': event.url,
            'filename': Path(event.suggested_filename).name,
            'state': 'inProgress',
            'receivedBytes': 0,
            'totalBytes': 0,
            'startedAt': time.time(),
            'path': None,
        }

    def place_completed_download(self, record, value):
        source = Path(value).expanduser().absolute()
        root = self.download_dir.resolve()
        resolved_parent = source.parent.resolve()
        if source.parent.is_symlink() or resolved_parent != source.parent.absolute():
            raise ValueError('completed download parent cannot be a symlink')
        if resolved_parent != root and root not in resolved_parent.parents:
            raise ValueError('completed download escaped the configured download directory')
        if source.is_symlink():
            raise ValueError('completed download cannot be a symlink')
        if not source.is_file():
            return str(source)
        destination_dir = self.session_download_dir(record['sessionId']).resolve()
        if source.parent == destination_dir:
            return str(source)
        requested = Path(record.get('filename') or source.name)
        destination = destination_dir / requested.name
        counter = 1
        while destination.exists():
            destination = destination_dir / f'{requested.stem} ({counter}){requested.suffix}'
            counter += 1
        source.replace(destination)
        return str(destination)

    def on_download_progress(self, event):
        record = self.downloads.setdefault(event.guid, {
            'guid': event.guid,
            'sessionId': None,
            'url': '',
            'filename': event.guid,
            'startedAt': time.time(),
            'path': None,
        })
        record.update({
            'state': event.state,
            'receivedBytes': int(event.received_bytes),
            'totalBytes': int(event.total_bytes),
        })
        if event.file_path:
            record['path'] = event.file_path
        if event.state == 'completed':
            if record.get('path'):
                record['path'] = self.place_completed_download(record, record['path'])
            else:
                record['path'] = str(
                    self.session_download_dir(record['sessionId']) / record['filename']
                )

    def download_file_snapshot(self, session_id='default'):
        download_dir = self.session_download_dir(session_id)
        files = {}
        for path in download_dir.iterdir():
            try:
                if not path.is_file() or path.is_symlink() or path.name.endswith('.crdownload'):
                    continue
                resolved = self.safe_download_path(path, session_id)
                stat = resolved.stat()
                files[resolved] = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                continue
        return files

    def list_downloads(self, limit=10, session_id='default'):
        download_dir = self.session_download_dir(session_id)
        items = []
        for path in download_dir.iterdir():
            try:
                if not path.is_file() or path.is_symlink() or path.name.endswith('.crdownload'):
                    continue
                resolved = self.safe_download_path(path, session_id)
                stat = resolved.stat()
            except OSError:
                continue
            items.append({
                'name': resolved.name,
                'path': str(resolved),
                'size': stat.st_size,
                'mimeType': mimetypes.guess_type(resolved.name)[0] or 'application/octet-stream',
                'state': 'completed',
                'modifiedAt': stat.st_mtime,
            })
        for record in self.downloads.values():
            if record.get('sessionId', 'default') != session_id or record.get('state') == 'completed':
                continue
            total = record.get('totalBytes', 0)
            received = record.get('receivedBytes', 0)
            progress = int(received * 100 / total) if total else None
            items.append({
                'name': record.get('filename') or record['guid'],
                'path': None,
                'url': record.get('url') or '',
                'size': received,
                'mimeType': mimetypes.guess_type(record.get('filename', ''))[0] or 'application/octet-stream',
                'state': 'downloading' if record.get('state') == 'inProgress' else record.get('state', 'unknown'),
                'progress': progress,
                'modifiedAt': record.get('startedAt', 0),
            })
        items.sort(key=lambda item: item['modifiedAt'], reverse=True)
        return items[:limit]

    def safe_download_path(self, value, session_id='default'):
        path = Path(value).expanduser().resolve()
        root = self.session_download_dir(session_id).resolve()
        if path != root and root not in path.parents:
            raise ValueError('download path escaped this session download directory')
        return path

    async def wait_for_download(self, baseline_guids, before_files, timeout_ms, session_id='default'):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000
        while loop.time() < deadline:
            new_records = [
                record for guid, record in self.downloads.items()
                if guid not in baseline_guids and record.get('sessionId', 'default') == session_id
            ]
            new_records.sort(key=lambda record: record.get('startedAt', 0), reverse=True)
            for record in new_records:
                if record.get('state') == 'canceled':
                    raise RuntimeError(f'download canceled: {record.get("filename", record["guid"])}')
                if record.get('state') == 'completed' and record.get('path'):
                    path = self.safe_download_path(record['path'], session_id)
                    if path.is_file() and not path.name.endswith('.crdownload'):
                        record['path'] = str(path)
                        return record

            current_files = self.download_file_snapshot(session_id)
            changed_files = [
                path for path, signature in current_files.items()
                if before_files.get(path) != signature
            ]
            if changed_files:
                path = max(changed_files, key=lambda item: item.stat().st_mtime_ns)
                return {
                    'guid': '',
                    'sessionId': session_id,
                    'url': '',
                    'filename': path.name,
                    'state': 'completed',
                    'receivedBytes': path.stat().st_size,
                    'totalBytes': path.stat().st_size,
                    'startedAt': time.time(),
                    'path': str(path),
                }
            await asyncio.sleep(0.05)
        raise TimeoutError(f'timed out waiting {timeout_ms}ms for a download to complete')

    def download_response(self, record, action, session_id='default'):
        path = self.safe_download_path(record['path'], session_id)
        size = path.stat().st_size
        mime_type = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        return {
            'text': (
                f'Download completed\nName: {path.name}\nSize: {size} bytes\n'
                f'Type: {mime_type}\nPath: {path}'
            ),
            'action': action,
            'downloadPath': str(path),
            'filename': path.name,
            'size': size,
            'mimeType': mime_type,
            'url': record.get('url') or '',
        }

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
            self.popup_openers.clear()
            self.popup_just_switched.clear()
            self.popup_just_closed.clear()
            self.download_frame_sessions.clear()
            self.download_target_sessions.clear()

    async def wait_for_page_ready(self, page, timeout_sec=2.0, poll_interval=0.08):
        """
        Adaptive fast-path DOM ready detector.
        Returns as soon as document.readyState is interactive/complete and body has content,
        polling every 80ms up to timeout_sec (default 2.0s).
        """
        deadline = asyncio.get_running_loop().time() + timeout_sec
        try:
            while asyncio.get_running_loop().time() < deadline:
                state = await page.evaluate("document.readyState")
                if state in ("interactive", "complete"):
                    has_content = await page.evaluate(
                        "Boolean(document.body && (document.body.innerText.length > 0 || document.body.children.length > 0))"
                    )
                    if has_content:
                        await asyncio.sleep(0.05)
                        return
                await asyncio.sleep(poll_interval)
        except Exception:
            await page.sleep(0.3)

    async def require_page(self, session_id):
        page = self.pages.get(session_id)
        if page is None:
            raise ValueError('this Pi session has no open page; run open <url> first')
        openers = self.popup_openers.get(session_id, [])
        if openers:
            await self.browser.update_targets()
            if page not in self.browser.tabs:
                while openers:
                    opener = openers.pop()
                    if opener in self.browser.tabs:
                        await opener.bring_to_front()
                        self.pages[session_id] = opener
                        self.popup_just_switched.discard(session_id)
                        self.popup_just_closed.add(session_id)
                        return opener
                raise ValueError('popup and its opener are no longer available')
        return page

    def stale_ref_error(self, session_id, ref):
        self.snapshot_required_sessions.add(session_id)
        return StaleRefError(ref)

    async def stale_ref_recovery(self, session_id, ref):
        page = await self.require_page(session_id)
        elements = json.loads(await page.evaluate(SNAPSHOT_JS))
        output_dir = Path(tempfile.mkdtemp(prefix='pi-nodriver-stale-'))
        output = output_dir / 'snapshot.jpg'
        screenshot_timeout = float(os.environ.get('PI_NODRIVER_SCREENSHOT_TIMEOUT', '30'))
        await asyncio.wait_for(
            page.save_screenshot(output, format='jpeg', full_page=False),
            timeout=screenshot_timeout,
        )
        snapshot = format_snapshot(elements or [])
        return {
            'text': (
                f'CLICK NOT PERFORMED: {ref} is stale.\n'
                'STALE_REF_GUARD remains active; use the image and fresh DOM snapshot together to reassess the page. '
                'Run exactly: snapshot -i before issuing another ref-based command.\n\n'
                f'Fresh DOM snapshot:\n{snapshot}'
            ),
            'action': 'stale-ref-recovery',
            'count': len(elements or []),
            'screenshotPath': str(output),
        }

    async def element(self, session_id, ref):
        page = await self.require_page(session_id)
        normalized = ref.removeprefix('@')
        element = await page.select(f'[data-pi-ref="{normalized}"]')
        if not element:
            raise self.stale_ref_error(session_id, ref)
        return element

    async def resolve_click_target(self, page, kind, value, session_id=None):
        request = json.dumps({'kind': kind, 'value': value}, ensure_ascii=False)
        script = CLICK_TARGET_JS.replace('__PI_CLICK_REQUEST__', request)
        result = json.loads(await page.evaluate(script))
        if not result.get('found'):
            if kind == 'ref':
                if session_id is not None:
                    raise self.stale_ref_error(session_id, f'@{value}')
                raise ValueError(f'element @{value} not found; run snapshot -i again')
            raise ValueError(f'click target not found by {kind}: {value}')
        return result

    @staticmethod
    def is_owned_popup(opener, popup):
        return popup.target.opener_id == opener.target.target_id

    async def mouse_click_allowing_target_close(self, page, x, y, timeout_seconds=1.0):
        try:
            await asyncio.wait_for(
                page.mouse_click(float(x), float(y)),
                timeout=timeout_seconds,
            )
            return True
        except TimeoutError:
            await self.browser.update_targets()
            if page not in self.browser.tabs:
                return False
            raise TimeoutError('native mouse click did not complete')

    async def native_click(self, page, x, y):
        minimum_settle_seconds = 0.1
        maximum_settle_seconds = 0.5
        new_tab_timeout_seconds = 2.0
        poll_seconds = 0.05
        before_tabs = len(self.browser.tabs)
        before_target_ids = {tab.target.target_id for tab in self.browser.tabs}
        clicking_page = page
        before_url = page.url
        await page.bring_to_front()
        try:
            expect_new_tab = bool(await page.evaluate(f'''(() => {{
              window.__piClickSettle?.observer?.disconnect();
              const hit = document.elementFromPoint({float(x)}, {float(y)});
              const anchor = hit?.closest?.('a');
              const control = hit?.closest?.('button,input');
              const form = control?.form || hit?.closest?.('form');
              const target = anchor?.target || control?.formTarget || form?.target || '';
              const opensBrowsingContext = target && !['_self', '_parent', '_top']
                .includes(target.toLowerCase());
              let clickHandler = '';
              for (let element = hit; element; element = element.parentElement) {{
                clickHandler += ` ${{element.getAttribute?.('onclick') || ''}} ${{element.onclick || ''}}`;
              }}
              const state = {{ mutations: 0, observer: null }};
              state.observer = new MutationObserver(() => state.mutations++);
              state.observer.observe(document.documentElement, {{
                subtree: true, childList: true, characterData: true
              }});
              window.__piClickSettle = state;
              return opensBrowsingContext || /(?:window\\.)?open\\s*\\(/.test(clickHandler);
            }})()'''))
        except Exception:
            expect_new_tab = False

        click_completed = await self.mouse_click_allowing_target_close(page, x, y)
        if not click_completed:
            return page
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + (new_tab_timeout_seconds if expect_new_tab else maximum_settle_seconds)
        last_change = started
        last_mutations = 0
        while loop.time() < deadline:
            await page.sleep(poll_seconds)
            now = loop.time()
            if len(self.browser.tabs) > before_tabs:
                await self.browser.update_targets()
                owned_popups = [
                    tab for tab in self.browser.tabs
                    if tab.target.target_id not in before_target_ids
                    and self.is_owned_popup(clicking_page, tab)
                ]
                if owned_popups:
                    page = owned_popups[-1]
                    await page.bring_to_front()
                    before_tabs = len(self.browser.tabs)
                    before_target_ids = {tab.target.target_id for tab in self.browser.tabs}
                    before_url = page.url
                    expect_new_tab = False
                    deadline = min(deadline, now + maximum_settle_seconds)
                    last_change = now
            try:
                state = json.loads(await page.evaluate('''JSON.stringify({
                  ready: document.readyState,
                  mutations: window.__piClickSettle?.mutations || 0,
                  url: location.href
                })'''))
                if state['url'] != before_url:
                    before_url = state['url']
                    last_change = now
                if state['mutations'] != last_mutations:
                    last_mutations = state['mutations']
                    last_change = now
            except Exception:
                last_change = now

            elapsed = now - started
            quiet = now - last_change
            if not expect_new_tab and (
                (elapsed >= minimum_settle_seconds and quiet >= minimum_settle_seconds)
                or elapsed >= maximum_settle_seconds
            ):
                break

        try:
            await page.evaluate('window.__piClickSettle?.observer?.disconnect()')
        except Exception:
            pass
        return page

    async def track_clicked_page(self, session_id, previous, page):
        if page != previous:
            if not self.is_owned_popup(previous, page):
                page = previous
            else:
                self.popup_openers.setdefault(session_id, []).append(previous)
                self.popup_just_switched.add(session_id)
                await self.configure_download_session(session_id, page)
        if self.popup_openers.get(session_id):
            await asyncio.sleep(0.1)
        await self.browser.update_targets()
        if page not in self.browser.tabs:
            openers = self.popup_openers.get(session_id, [])
            while openers:
                opener = openers.pop()
                if opener in self.browser.tabs:
                    await opener.bring_to_front()
                    self.popup_just_closed.add(session_id)
                    return opener
        return page

    def track_repeat(self, session_id, action, parts):
        signature = ' '.join(parts)
        previous, count = self.repeated_commands.get(session_id, (None, 0))
        if signature != previous:
            self.repeated_commands[session_id] = (signature, 1)
            return
        count += 1
        self.repeated_commands[session_id] = (signature, count)
        if action not in NON_PROGRESSING_ACTIONS or count < REPEAT_LIMIT:
            return
        self.repeated_commands.pop(session_id, None)
        raise ValueError(
            f'LOOP_GUARD: "{signature}" ran {count} times in a row and cannot return anything new. '
            'Stop repeating it. The browser is only worth using when the answer requires driving a '
            'live page (logging in, clicking through a flow, reading something behind interaction). '
            'If the question is general research or the page is not cooperating, abandon the browser '
            'now and answer using web search, firecrawl, or your own knowledge instead. '
            'If you do stay in the browser, the next command must be a different one that changes '
            'state or target: open <url>, scroll, click, or close.'
        )

    async def execute(self, command, session_id='default'):
        parts = parse_command(command)
        action = parts[0].lower()
        self.track_repeat(session_id, action, parts)
        uses_ref = (
            (action in {'click', 'click-js', 'download', 'download-info'} and len(parts) > 1 and parts[1].startswith('@'))
            or (action in {'fill', 'type', 'select'} and len(parts) > 1)
            or (action == 'get' and len(parts) > 2 and parts[2].startswith('@'))
            or (action == 'wait' and len(parts) > 1 and parts[1].startswith('@'))
        )
        if uses_ref and session_id in self.snapshot_required_sessions:
            raise ValueError(
                'STALE_REF_GUARD: ref-based commands are blocked after a stale ref; '
                'do not retry the old ref; run exactly: snapshot -i'
            )
        if action != 'wait-popup':
            self.popup_just_switched.discard(session_id)
        if action != 'wait-popup-close':
            self.popup_just_closed.discard(session_id)

        if action == 'wait-download':
            if len(parts) > 2:
                raise ValueError('usage: wait-download [ms]')
            timeout_ms = int(parts[1]) if len(parts) == 2 else 30000
            session_records = {
                guid: record for guid, record in self.downloads.items()
                if record.get('sessionId', 'default') == session_id
            }
            if session_records:
                latest_guid = max(
                    session_records,
                    key=lambda guid: session_records[guid].get('startedAt', 0),
                )
                baseline_guids = set(self.downloads) - {latest_guid}
                record = await self.wait_for_download(
                    baseline_guids, self.download_file_snapshot(session_id), timeout_ms, session_id
                )
                return self.download_response(record, action, session_id)
            existing = self.list_downloads(1, session_id)
            if existing:
                return self.download_response({'path': existing[0]['path'], 'url': ''}, action, session_id)
            record = await self.wait_for_download(
                set(self.downloads), self.download_file_snapshot(session_id), timeout_ms, session_id
            )
            return self.download_response(record, action, session_id)

        if action == 'download-latest':
            if len(parts) != 1:
                raise ValueError('usage: download-latest')
            items = self.list_downloads(1, session_id)
            if not items:
                raise ValueError(f'no completed downloads for this session')
            return self.download_response({'path': items[0]['path'], 'url': ''}, action, session_id)

        if action == 'downloads':
            if len(parts) > 2:
                raise ValueError('usage: downloads [limit]')
            limit = int(parts[1]) if len(parts) == 2 else 10
            if not 1 <= limit <= 100:
                raise ValueError('download list limit must be between 1 and 100')
            items = self.list_downloads(limit, session_id)
            if items:
                lines = []
                for index, item in enumerate(items, 1):
                    state = item['state']
                    if item.get('progress') is not None:
                        state += f' {item["progress"]}%'
                    location = item.get('path') or item.get('url') or '(pending path)'
                    lines.append(
                        f'{index}. {item["name"]} — {state} — {item["size"]} bytes — {item["mimeType"]}\n   {location}'
                    )
                text = '\n'.join(lines)
            else:
                text = 'No downloads for this session'
            return {'text': text, 'action': action, 'downloads': items}

        if action == 'open':
            if len(parts) != 2:
                raise ValueError('usage: open <url>')
            browser = await self.ensure_browser()
            previous = self.pages.get(session_id)
            previous_openers = self.popup_openers.pop(session_id, [])
            await self.configure_download_session(session_id)
            page = await browser.get('about:blank', new_tab=True)

            # Enforce iPhone Mobile Mode (Portrait 390x844 with Touch Emulation)
            ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"
            w, h = 390, 844
            await page.send(uc.cdp.network.set_user_agent_override(user_agent=ua))
            await page.send(uc.cdp.emulation.set_device_metrics_override(
                width=w, height=h, device_scale_factor=3.0, mobile=True
            ))
            await page.send(uc.cdp.emulation.set_touch_emulation_enabled(enabled=True))
            page._is_mobile_mode = True

            await page.get(parts[1])
            self.pages[session_id] = page
            await self.configure_download_session(session_id, page)
            for old_page in [previous, *previous_openers]:
                if old_page is not None:
                    try:
                        await old_page.close()
                    except Exception:
                        pass
            await self.wait_for_page_ready(page)
            return {'text': f'Opened {page.url or parts[1]} (iPhone Mobile Mode 390x844)', 'action': action, 'url': page.url or parts[1]}

        if action == 'snapshot':
            if parts not in (['snapshot', '-i'], ['snapshot', '-i', '--full']):
                raise ValueError('usage: snapshot -i [--full]')
            page = await self.require_page(session_id)
            if '--full' in parts:
                output_dir = Path(tempfile.mkdtemp(prefix='pi-nodriver-full-'))
                output = output_dir / 'overview.jpg'
                screenshot_timeout = float(os.environ.get('PI_NODRIVER_SCREENSHOT_TIMEOUT', '30'))
                await asyncio.wait_for(
                    page.save_screenshot(output, format='jpeg', full_page=True),
                    timeout=screenshot_timeout,
                )
                return {
                    'text': (
                        'Visual overview only; no DOM refs were generated. Inspect the image first. '
                        'Then use scroll down or scroll up and run snapshot -i to inspect and interact with '
                        'objects in each current viewport. Do not report an object as missing until you have '
                        'checked the likely page sections and reached the relevant page boundary.'
                    ),
                    'action': 'snapshot-full-vision',
                    'count': 0,
                    'screenshotPath': str(output),
                }
            elements = json.loads(await page.evaluate(SNAPSHOT_JS))
            self.snapshot_required_sessions.discard(session_id)
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

        if action == 'download-info':
            if len(parts) != 2 or not parts[1].startswith('@'):
                raise ValueError('usage: download-info <@ref>')
            page = await self.require_page(session_id)
            target = await self.resolve_click_target(page, 'ref', parts[1].removeprefix('@'), session_id)
            url = target.get('href') or ''
            if not url:
                raise ValueError(f'{parts[1]} does not expose a download URL')
            parsed = urllib.parse.urlparse(url)
            filename = target.get('download') or Path(urllib.parse.unquote(parsed.path)).name or 'download'
            mime_type = mimetypes.guess_type(filename)[0] or mimetypes.guess_type(parsed.path)[0] or 'application/octet-stream'
            page_origin = urllib.parse.urlparse(page.url)
            cross_origin = (parsed.scheme, parsed.netloc) != (page_origin.scheme, page_origin.netloc)
            text = (
                f'Download target: {target.get("text") or filename}\n'
                f'Name: {filename}\nType: {mime_type}\n'
                f'Cross-origin: {str(cross_origin).lower()}\nURL: {url}'
            )
            return {
                'text': text,
                'action': action,
                'url': url,
                'filename': filename,
                'mimeType': mime_type,
                'crossOrigin': cross_origin,
            }

        if action == 'download':
            if len(parts) not in (2, 3) or not parts[1].startswith('@'):
                raise ValueError('usage: download <@ref> [ms]')
            timeout_ms = int(parts[2]) if len(parts) == 3 else 30000
            page = await self.require_page(session_id)
            target = await self.resolve_click_target(page, 'ref', parts[1].removeprefix('@'), session_id)
            await self.configure_download_session(session_id, page)
            baseline_guids = set(self.downloads)
            before_files = self.download_file_snapshot(session_id)
            previous = page
            page = await self.native_click(page, target['x'], target['y'])
            page = await self.track_clicked_page(session_id, previous, page)
            self.pages[session_id] = page
            record = await self.wait_for_download(
                baseline_guids, before_files, timeout_ms, session_id
            )
            return self.download_response(record, action, session_id)

        if action == 'click':
            page = await self.require_page(session_id)
            if len(parts) == 3:
                try:
                    x, y = float(parts[1]), float(parts[2])
                except ValueError as error:
                    raise ValueError('usage: click <@ref> or click <x> <y>') from error
                previous = page
                await self.configure_download_session(session_id, page)
                page = await self.native_click(page, x, y)
                page = await self.track_clicked_page(session_id, previous, page)
                self.pages[session_id] = page
                return {'text': f'Clicked viewport coordinates ({x:g}, {y:g})\nURL: {page.url}', 'action': action, 'url': page.url}
            if len(parts) != 2 or not parts[1].startswith('@'):
                raise ValueError('usage: click <@ref> or click <x> <y>')
            normalized = parts[1].removeprefix('@')
            target = await self.resolve_click_target(page, 'ref', normalized, session_id)
            previous = page
            await self.configure_download_session(session_id, page)
            page = await self.native_click(page, target['x'], target['y'])
            page = await self.track_clicked_page(session_id, previous, page)
            self.pages[session_id] = page
            return {'text': f'Clicked {parts[1]} ({target.get("tag", "element")}: {target.get("text", "")[:120]})\nURL: {page.url}', 'action': action, 'url': page.url}

        if action in ('click-text', 'click-css'):
            if len(parts) < 2:
                raise ValueError(f'usage: {action} <text-or-selector>')
            page = await self.require_page(session_id)
            value = ' '.join(parts[1:])
            kind = 'text' if action == 'click-text' else 'css'
            target = await self.resolve_click_target(page, kind, value)
            previous = page
            await self.configure_download_session(session_id, page)
            page = await self.native_click(page, target['x'], target['y'])
            page = await self.track_clicked_page(session_id, previous, page)
            self.pages[session_id] = page
            return {'text': f'Clicked by {kind} "{value}" ({target.get("tag", "element")}: {target.get("text", "")[:120]})\nURL: {page.url}', 'action': action, 'url': page.url}

        if action == 'click-js':
            if len(parts) != 2 or not parts[1].startswith('@'):
                raise ValueError('usage: click-js <@ref>')
            page = await self.require_page(session_id)
            normalized = parts[1].removeprefix('@')
            target = await self.resolve_click_target(page, 'ref', normalized, session_id)
            await self.configure_download_session(session_id, page)
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
            await self.configure_download_session(session_id, page)
            focused = await page.select(':focus') or await page.select('body')
            await focused.send_keys(key)
            if parts[1].lower() == 'enter':
                submit_script = '''(() => {
                    const el = document.activeElement;
                    if (!el) return false;
                    const opts = { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true };
                    el.dispatchEvent(new KeyboardEvent('keydown', opts));
                    el.dispatchEvent(new KeyboardEvent('keypress', opts));
                    el.dispatchEvent(new KeyboardEvent('keyup', opts));
                    const form = el.closest ? el.closest('form') : (el.form || null);
                    if (form && typeof form.requestSubmit === 'function') {
                        try { form.requestSubmit(); return true; } catch (e) {}
                    }
                    const searchBtn = el.parentElement ? el.parentElement.querySelector('button, [class*="search"], [type="submit"]') : null;
                    if (searchBtn) {
                        try { searchBtn.click(); return true; } catch (e) {}
                    }
                    return false;
                })()'''
                await page.evaluate(submit_script)
            await page.sleep(0.3)
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

        if action == 'wait-popup':
            if len(parts) > 2:
                raise ValueError('usage: wait-popup [ms]')
            timeout_ms = int(parts[1]) if len(parts) == 2 else 30000
            page = await self.require_page(session_id)
            if session_id in self.popup_just_switched:
                self.popup_just_switched.discard(session_id)
                return {
                    'text': f'Popup is already active\nURL: {page.url}',
                    'action': action,
                    'url': page.url,
                }
            opener_id = page.target.target_id
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout_ms / 1000
            while loop.time() < deadline:
                await self.browser.update_targets()
                popup = next((
                    tab for tab in reversed(self.browser.tabs)
                    if tab != page and tab.target.opener_id == opener_id
                ), None)
                if popup is not None:
                    self.popup_openers.setdefault(session_id, []).append(page)
                    self.pages[session_id] = popup
                    self.popup_just_switched.add(session_id)
                    await self.configure_download_session(session_id, popup)
                    await popup.bring_to_front()
                    while popup.url in ('', 'about:blank') and loop.time() < deadline:
                        await asyncio.sleep(0.05)
                        await self.browser.update_targets()
                    return {
                        'text': f'Popup opened\nURL: {popup.url}',
                        'action': action,
                        'url': popup.url,
                    }
                await asyncio.sleep(0.05)
            raise TimeoutError(f'timed out waiting {timeout_ms}ms for popup to open')

        if action == 'switch':
            if len(parts) != 2 or parts[1].lower() != 'opener':
                raise ValueError('usage: switch opener')
            openers = self.popup_openers.get(session_id, [])
            await self.browser.update_targets()
            while openers:
                opener = openers.pop()
                if opener in self.browser.tabs:
                    await opener.bring_to_front()
                    self.pages[session_id] = opener
                    self.popup_just_switched.discard(session_id)
                    return {
                        'text': f'Switched to popup opener\nURL: {opener.url}',
                        'action': action,
                        'url': opener.url,
                    }
            raise ValueError('the current page has no available popup opener')

        if action == 'wait-popup-close':
            if len(parts) > 2:
                raise ValueError('usage: wait-popup-close [ms]')
            timeout_ms = int(parts[1]) if len(parts) == 2 else 30000
            page = await self.require_page(session_id)
            openers = self.popup_openers.get(session_id, [])
            if not openers:
                if session_id in self.popup_just_closed:
                    self.popup_just_closed.discard(session_id)
                    return {
                        'text': f'Popup is already closed; opener is active\nURL: {page.url}',
                        'action': action,
                        'url': page.url,
                    }
                raise ValueError('the current page has no tracked popup opener')
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout_ms / 1000
            while loop.time() < deadline:
                await self.browser.update_targets()
                if page not in self.browser.tabs:
                    while openers:
                        opener = openers.pop()
                        if opener in self.browser.tabs:
                            await opener.bring_to_front()
                            self.pages[session_id] = opener
                            self.popup_just_switched.discard(session_id)
                            return {
                                'text': f'Popup closed; returned to opener\nURL: {opener.url}',
                                'action': action,
                                'url': opener.url,
                            }
                    raise ValueError('popup closed, but its opener is no longer available')
                await asyncio.sleep(0.05)
            raise TimeoutError(f'timed out waiting {timeout_ms}ms for popup to close')

        if action == 'wait':
            raise ValueError("Command 'wait' does not exist and is unnecessary. All browser actions (open, click, scroll) automatically settle DOM and network. Proceed DIRECTLY to snapshot -i or screenshot.")


        if action == 'mobile':
            page = await self.require_page(session_id)
            target = parts[1].lower() if len(parts) > 1 else 'on'
            if target in ('off', 'disable', 'false', 'desktop'):
                await page.send(uc.cdp.network.set_user_agent_override(user_agent=''))
                await page.send(uc.cdp.emulation.clear_device_metrics_override())
                await page.send(uc.cdp.emulation.set_touch_emulation_enabled(enabled=False))
                page._is_mobile_mode = False
                return {'text': 'Mobile emulation disabled; restored desktop viewport', 'action': action}
            else:
                ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"
                if target in ('android', 'pixel'):
                    ua = "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
                
                w, h = 390, 844
                if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
                    w, h = int(parts[1]), int(parts[2])
                elif len(parts) >= 4 and parts[2].isdigit() and parts[3].isdigit():
                    w, h = int(parts[2]), int(parts[3])

                await page.send(uc.cdp.network.set_user_agent_override(user_agent=ua))
                await page.send(uc.cdp.emulation.set_device_metrics_override(
                    width=w,
                    height=h,
                    device_scale_factor=3.0,
                    mobile=True
                ))
                await page.send(uc.cdp.emulation.set_touch_emulation_enabled(enabled=True))
                page._is_mobile_mode = True
                return {'text': f'Mobile emulation enabled ({w}x{h} viewport, touch enabled)', 'action': action, 'viewport': [w, h]}

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

        if action == 'crawl':
            remainder = command[len('crawl'):].strip()
            # Robust URL extraction: supports JSON arrays, space/newline separated, or quoted URLs
            extracted = re.findall(r'https?://[^\s"\'\]\[\<\>]+', remainder)
            # Clean trailing punctuation that might come from sentence end
            urls = []
            for u in extracted:
                u_clean = u.rstrip('.,;)')
                if u_clean and u_clean not in urls:
                    urls.append(u_clean)

            if not urls:
                raise ValueError('usage: crawl <url1> [url2] [url3] ...')

            await self.ensure_browser()

            async def crawl_single(target_url, idx):
                tab = None
                t0 = asyncio.get_running_loop().time()
                try:
                    tab = await self.browser.get("about:blank", new_tab=True)
                    # Custom Crawl Mode Resolution: Force 1920x1080 Full-Desktop Viewport per tab
                    try:
                        await tab.send(uc.cdp.emulation.set_device_metrics_override(
                            width=1920,
                            height=1080,
                            device_scale_factor=1.0,
                            mobile=False
                        ))
                    except Exception:
                        pass
                    await tab.get(target_url)
                    await self.wait_for_page_ready(tab, timeout_sec=4.0)
                    title = await tab.evaluate("document.title") or "No Title"
                    text = await tab.evaluate("document.body.innerText") or ""
                    clean_text = str(text).strip()
                    elapsed = round(asyncio.get_running_loop().time() - t0, 2)
                    return {
                        "index": idx + 1,
                        "url": target_url,
                        "title": str(title).strip(),
                        "text": clean_text,
                        "ok": bool(clean_text),
                        "chars": len(clean_text),
                        "elapsed": elapsed
                    }
                except Exception as err:
                    elapsed = round(asyncio.get_running_loop().time() - t0, 2)
                    return {
                        "index": idx + 1,
                        "url": target_url,
                        "title": "Error",
                        "text": "",
                        "ok": False,
                        "error": str(err),
                        "chars": 0,
                        "elapsed": elapsed
                    }
                finally:
                    if tab is not None:
                        try:
                            await tab.close()
                        except Exception:
                            pass

            results = await asyncio.gather(*(crawl_single(url, i) for i, url in enumerate(urls)))
            successful = [r for r in results if r["ok"]]
            total_chars = sum(r["chars"] for r in results)

            output_parts = [
                f"Parallel Crawl Completed: {len(successful)}/{len(urls)} pages successfully captured ({total_chars:,} total characters)."
            ]
            for r in results:
                if r["ok"]:
                    output_parts.append(
                        f"### [{r['index']}] [{r['title']}]({r['url']})\n"
                        f"*Status: OK | Length: {r['chars']:,} chars | Time: {r['elapsed']}s*\n\n"
                        f"{r['text']}\n"
                    )
                else:
                    output_parts.append(
                        f"### [{r['index']}] [Failed] {r['url']}\n"
                        f"*Status: FAILED | Error: {r.get('error', 'No text extracted')}*\n"
                    )

            return {
                "text": "\n---\n".join(output_parts),
                "action": "crawl",
                "results": results,
                "successCount": len(successful),
                "totalCount": len(urls)
            }

        if action == 'close':
            page = self.pages.pop(session_id, None)
            self.popup_openers.pop(session_id, None)
            self.popup_just_switched.discard(session_id)
            self.popup_just_closed.discard(session_id)
            self.repeated_commands.pop(session_id, None)
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
    except StaleRefError as error:
        try:
            recovery = await asyncio.wait_for(
                worker.stale_ref_recovery(session_id, error.ref),
                timeout=command_timeout,
            )
            return {'id': request.get('id'), 'sessionId': session_id, 'ok': True, **recovery}
        except Exception as recovery_error:
            return {
                'id': request.get('id'),
                'sessionId': session_id,
                'ok': False,
                'error': f'{type(error).__name__}: {error}; visual recovery failed: {recovery_error}',
            }
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
    client_writers = set()
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
        client_writers.add(writer)
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
                        if action in {'open', 'click', 'click-text', 'click-css', 'click-js', 'download', 'press', 'close'}:
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
                for client_writer in tuple(client_writers):
                    client_writer.close()

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
            client_writers.discard(writer)
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
