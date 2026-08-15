#!/usr/bin/env python3
import asyncio
import json
import logging
import sys
import tempfile
from pathlib import Path

import nodriver as uc

from browser_logic import format_snapshot, parse_command, resolve_browser_executable, should_disable_sandbox

MARKER = '__PI_NODRIVER__'
logging.basicConfig(level=logging.CRITICAL)

SNAPSHOT_JS = r'''JSON.stringify((() => {
  document.querySelectorAll('[data-pi-ref]').forEach(el => el.removeAttribute('data-pi-ref'));
  const selector = 'a,button,input,textarea,select,summary,[role="button"],[role="link"],[contenteditable="true"]';
  const elements = Array.from(document.querySelectorAll(selector)).filter(el => {
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  });
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


class BrowserWorker:
    def __init__(self):
        self.browser = None
        self.page = None

    async def ensure_browser(self):
        if self.browser is None:
            profile = Path.home() / '.pi' / 'agent' / 'nodriver-profile'
            profile.mkdir(parents=True, exist_ok=True)
            self.browser = await uc.start(
                headless=False,
                browser_executable_path=resolve_browser_executable(),
                user_data_dir=str(profile),
                browser_args=['--window-size=1440,1000', '--no-first-run', '--no-default-browser-check'],
                no_sandbox=should_disable_sandbox(),
                lang='zh-TW',
            )
        return self.browser

    async def require_page(self):
        if self.page is None:
            raise ValueError('browser has no open page; run open <url> first')
        return self.page

    async def element(self, ref):
        page = await self.require_page()
        normalized = ref.removeprefix('@')
        element = await page.select(f'[data-pi-ref="{normalized}"]')
        if not element:
            raise ValueError(f'element {ref} not found; run snapshot -i again')
        return element

    async def execute(self, command):
        parts = parse_command(command)
        action = parts[0].lower()

        if action == 'open':
            if len(parts) != 2:
                raise ValueError('usage: open <url>')
            browser = await self.ensure_browser()
            self.page = await browser.get(parts[1])
            await self.page.sleep(2)
            return {'text': f'Opened {self.page.url or parts[1]}', 'action': action, 'url': self.page.url or parts[1]}

        if action == 'snapshot':
            page = await self.require_page()
            elements = json.loads(await page.evaluate(SNAPSHOT_JS))
            return {'text': format_snapshot(elements or []), 'action': action, 'count': len(elements or [])}

        if action == 'click':
            if len(parts) != 2:
                raise ValueError('usage: click <@ref>')
            element = await self.element(parts[1])
            before_tabs = len(self.browser.tabs)
            await self.page.bring_to_front()
            await element.scroll_into_view()
            await self.page.sleep(0.2)
            await element.mouse_click()
            await self.page.sleep(2)
            if len(self.browser.tabs) > before_tabs:
                self.page = self.browser.tabs[-1]
                await self.page.bring_to_front()
                await self.page.sleep(2)
            return {'text': f'Clicked {parts[1]}\nURL: {self.page.url}', 'action': action, 'url': self.page.url}

        if action in ('fill', 'type'):
            if len(parts) < 3:
                raise ValueError(f'usage: {action} <@ref> <text>')
            element = await self.element(parts[1])
            text = ' '.join(parts[2:])
            await element.focus()
            if action == 'fill':
                await element.clear_input()
            await element.send_keys(text)
            return {'text': f'{action.title()}d {parts[1]}', 'action': action}

        if action == 'select':
            if len(parts) < 3:
                raise ValueError('usage: select <@ref> <value>')
            select_element = await self.element(parts[1])
            wanted = ' '.join(parts[2:])
            options = await select_element.query_selector_all('option')
            match = next((o for o in options if wanted == (o.text_all or '').strip() or wanted == (o.attrs or {}).get('value')), None)
            if not match:
                match = next((o for o in options if wanted.lower() in (o.text_all or '').strip().lower()), None)
            if not match:
                raise ValueError(f'option not found: {wanted}')
            await match.select_option()
            await self.page.sleep(2)
            return {'text': f'Selected "{(match.text_all or wanted).strip()}"', 'action': action}

        if action == 'press':
            if len(parts) != 2:
                raise ValueError('usage: press <key>')
            key_map = {'enter': '\n', 'tab': '\t', 'space': ' ', 'backspace': '\b'}
            key = key_map.get(parts[1].lower(), parts[1])
            page = await self.require_page()
            focused = await page.select(':focus') or await page.select('body')
            await focused.send_keys(key)
            await page.sleep(1)
            return {'text': f'Pressed {parts[1]}', 'action': action}

        if action == 'scroll':
            page = await self.require_page()
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
            page = await self.require_page()
            kind = parts[1].lower()
            if kind == 'url':
                text = page.url
            elif kind == 'title':
                text = await page.evaluate('document.title')
            elif kind == 'text' and len(parts) > 2:
                text = (await self.element(parts[2])).text_all or ''
            elif kind == 'text':
                text = await page.evaluate('document.body.innerText')
            else:
                raise ValueError('usage: get text|url|title [@ref]')
            return {'text': str(text).strip(), 'action': action}

        if action == 'wait':
            page = await self.require_page()
            target = parts[1] if len(parts) > 1 else '1000'
            if target.startswith('@'):
                for _ in range(100):
                    try:
                        await self.element(target)
                        return {'text': f'Element {target} is available', 'action': action}
                    except ValueError:
                        await page.sleep(0.1)
                raise TimeoutError(f'timed out waiting for {target}')
            await page.sleep(int(target) / 1000)
            return {'text': f'Waited {target}ms', 'action': action}

        if action == 'screenshot':
            page = await self.require_page()
            full_page = '--full' in parts[1:]
            output_dir = Path(tempfile.mkdtemp(prefix='pi-nodriver-shot-'))
            output = output_dir / 'screenshot.png'
            await page.save_screenshot(output, format='png', full_page=full_page)
            return {'text': f'Screenshot saved: {output}', 'action': action, 'screenshotPath': str(output)}

        if action == 'close':
            if self.browser is not None:
                self.browser.stop()
            self.browser = None
            self.page = None
            return {'text': 'Browser closed', 'action': action}

        raise ValueError(f'unsupported browser command: {action}')

    async def close(self):
        if self.browser is not None:
            self.browser.stop()
            self.browser = None
            self.page = None


async def main():
    worker = BrowserWorker()
    try:
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            request = json.loads(line)
            try:
                result = await worker.execute(request.get('command', ''))
                response = {'id': request.get('id'), 'ok': True, **result}
            except Exception as error:
                response = {'id': request.get('id'), 'ok': False, 'error': f'{type(error).__name__}: {error}'}
            print(MARKER + json.dumps(response, ensure_ascii=False), flush=True)
    finally:
        await worker.close()


if __name__ == '__main__':
    uc.loop().run_until_complete(main())
