"""Minimal Chrome DevTools Protocol client.

One websocket to the browser target carries every tab through flat sessions
(Target.attachToTarget flatten=true). Every call has a timeout, so a wedged
renderer surfaces as CDPTimeout instead of a hang.
"""
import asyncio
import itertools
import json

import websockets


class CDPError(Exception):
    pass


class CDPTimeout(CDPError):
    pass


class CDPClosed(CDPError):
    pass


class Connection:
    def __init__(self):
        self._ws = None
        self._ids = itertools.count(1)
        self._pending = {}
        self._listeners = []  # (method, session or None for browser-level events, callback)
        self._reader = None
        self.closed = False

    async def connect(self, ws_url, timeout=10.0):
        self._ws = await asyncio.wait_for(
            websockets.connect(ws_url, max_size=None, ping_interval=None, open_timeout=timeout, close_timeout=1),
            timeout)
        self._reader = asyncio.create_task(self._read(), name='cdp-reader')

    async def _read(self):
        try:
            async for raw in self._ws:
                message = json.loads(raw)
                if 'id' in message:
                    future = self._pending.pop(message['id'], None)
                    if future is None or future.done():
                        continue
                    if 'error' in message:
                        error = message['error']
                        future.set_exception(CDPError(f"{error.get('message')} ({error.get('code')})"))
                    else:
                        future.set_result(message.get('result', {}))
                    continue
                method, session = message.get('method'), message.get('sessionId')
                for wanted, wanted_session, callback in tuple(self._listeners):
                    if wanted == method and wanted_session in (None, session):
                        try:
                            callback(message.get('params', {}))
                        except Exception:
                            pass
        except Exception:
            pass
        finally:
            self.closed = True
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(CDPClosed('browser connection closed'))
            self._pending.clear()

    def on(self, method, callback, session=None):
        """Call callback(params) for every `method` event (of one tab session, if given)."""
        self._listeners.append((method, session, callback))

    def drop_session(self, session):
        self._listeners = [entry for entry in self._listeners if entry[1] != session]

    async def send(self, method, params=None, *, session=None, timeout=10.0):
        if self.closed or self._ws is None:
            raise CDPClosed('browser connection closed')
        message_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        message = {'id': message_id, 'method': method, 'params': params or {}}
        if session:
            message['sessionId'] = session
        try:
            async with asyncio.timeout(timeout):
                await self._ws.send(json.dumps(message))
                return await future
        except TimeoutError:
            raise CDPTimeout(f'{method} timed out after {timeout:g}s') from None
        except websockets.ConnectionClosed:
            raise CDPClosed('browser connection closed') from None
        finally:
            self._pending.pop(message_id, None)

    async def close(self):
        self.closed = True
        if self._ws is not None:
            try:
                await asyncio.wait_for(self._ws.close(), 2)
            except Exception:
                pass
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
