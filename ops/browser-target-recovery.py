"""Explicit target-only recovery. Never navigate or restart shared Chrome.
Invoked only by resolve-browser.sh --recover-targets with exact operator-authorized IDs.
"""
import argparse
import asyncio
import json
from pathlib import Path
import re
import urllib.request
from urllib.parse import urlparse


async def rpc(url, method, params=None):
    import websockets
    async with asyncio.timeout(2):
        async with websockets.connect(url, open_timeout=1, max_size=2_000_000) as ws:
            await ws.send(json.dumps({'id': 1, 'method': method, 'params': params or {}}))
            while True:
                reply = json.loads(await ws.recv())
                if reply.get('id') == 1:
                    if 'error' in reply:
                        raise RuntimeError(reply['error'])
                    return reply.get('result', {})


async def recover_target(target, browser_url, call=rpc):
    url = target['webSocketDebuggerUrl']
    probe = {'expression': '1', 'returnByValue': True, 'timeout': 500}
    try:
        await call(url, 'Runtime.evaluate', probe)
        return {'id': target['id'], 'status': 'healthy'}
    except (TimeoutError, RuntimeError, OSError):
        pass
    try:
        await call(url, 'Runtime.terminateExecution')
    except (TimeoutError, RuntimeError, OSError):
        pass
    try:
        await call(url, 'Runtime.evaluate', probe)
        return {'id': target['id'], 'status': 'recovered'}
    except (TimeoutError, RuntimeError, OSError):
        result = await call(browser_url, 'Target.closeTarget', {'targetId': target['id']})
        if result.get('success') is not True:
            raise RuntimeError('target close failed')
        return {'id': target['id'], 'status': 'closed_hung_target'}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int)
    parser.add_argument('targets', nargs='+')
    args = parser.parse_args()
    if any(not re.fullmatch(r'[A-Fa-f0-9]{32}', target) for target in args.targets):
        parser.error('exact 32-hex CDP target IDs required')
    port = args.port if args.port is not None else int(
        (Path.home() / '.pi/agent/nodriver-profile/DevToolsActivePort').read_text().splitlines()[0])
    if not 1 <= port <= 65535:
        parser.error('invalid local CDP port')
    base = f'http://127.0.0.1:{port}'
    def fetch(endpoint):
        with urllib.request.urlopen(base + endpoint, timeout=2) as response:
            return json.load(response)
    version, targets = fetch('/json/version'), fetch('/json/list')
    browser_url = version['webSocketDebuggerUrl']
    by_id = {target['id']: target for target in targets}
    selected = []
    for target_id in dict.fromkeys(args.targets):
        if target_id not in by_id:
            print(json.dumps({'id': target_id, 'status': 'already_absent'}))
            continue
        target = by_id[target_id]
        if target['type'] != 'page' or urlparse(target['url']).hostname != 'console.cloud.google.com':
            raise ValueError('recovery restricted to selected Google Cloud page targets')
        selected.append(target)
    for ws_url in [browser_url] + [target['webSocketDebuggerUrl'] for target in selected]:
        parsed = urlparse(ws_url)
        if parsed.scheme != 'ws' or parsed.hostname != '127.0.0.1' or parsed.port != port:
            raise ValueError('unexpected non-local debugging endpoint')
    for result in await asyncio.gather(*(recover_target(target, browser_url) for target in selected)):
        print(json.dumps(result))
    print(json.dumps({'remaining_pages': sum(t['type'] == 'page' for t in fetch('/json/list')), 'shared_browser_restarted': False}))


if __name__ == '__main__':
    asyncio.run(main())
