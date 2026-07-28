#!/usr/bin/env python3
"""A minimal in-process nostr relay, just enough of NIP-01 for the e2e tests.

Why not the real thing: ``contrib/regtest_demo/run_demo.sh`` installs the
``nostr-relay`` package into its own venv for manual review, but CI has no such
venv and no network.  aiohttp is already an Electrum dependency and speaks
WebSocket on both ends, so we can host a real relay in the test process and let
the real ``electrum_aionostr`` client talk to it over a real socket.  Only the
storage layer is fake; the wire protocol, the event signing and the signature
verification on receipt are genuine.

Implements:
  * ``["EVENT", <event>]``            -> stores, replies ``["OK", id, true, ""]``
  * ``["REQ", <sub_id>, <filters>…]`` -> matching ``["EVENT", sub_id, <event>]``
                                         then ``["EOSE", sub_id]``
  * ``["CLOSE", <sub_id>]``           -> ignored (we never stream live events)

Filters support ``ids``, ``authors``, ``kinds``, ``since``, ``until``,
``limit`` and single-letter tag queries (``#d``, ``#r``, ``#p``…).  As NIP-01
requires, ``limit`` keeps the *newest* events, which is what lets the tests
reproduce a busy relay pushing an offer out of a taker's page.
"""
import asyncio
import json
from typing import Any, Dict, List, Optional

from aiohttp import web, WSMsgType


def event_matches(event: Dict[str, Any], filt: Dict[str, Any]) -> bool:
    """NIP-01 filter semantics: every condition must hold (AND across keys)."""
    if 'ids' in filt and event['id'] not in filt['ids']:
        return False
    if 'authors' in filt and event['pubkey'] not in filt['authors']:
        return False
    if 'kinds' in filt and event['kind'] not in filt['kinds']:
        return False
    if 'since' in filt and event['created_at'] < filt['since']:
        return False
    if 'until' in filt and event['created_at'] > filt['until']:
        return False
    for key, wanted in filt.items():
        if not (key.startswith('#') and len(key) == 2):
            continue
        name = key[1]
        values = {t[1] for t in event.get('tags', []) if len(t) >= 2 and t[0] == name}
        if not values.intersection(wanted):
            return False
    return True


class FakeRelay:
    """A relay bound to an ephemeral localhost port. Use as an async context."""

    def __init__(self, *, accept_writes: bool = True) -> None:
        self.events: List[Dict[str, Any]] = []
        self.accept_writes = accept_writes
        self.reqs: List[List[Any]] = []  # every REQ we were sent, for assertions
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self.url: Optional[str] = None

    async def __aenter__(self) -> 'FakeRelay':
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()

    async def start(self) -> str:
        app = web.Application()
        app.add_routes([web.get('/', self._handle)])
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host='127.0.0.1', port=0)
        await self._site.start()
        port = self._site._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        return self.url

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._site = None

    def store(self, event: Dict[str, Any]) -> None:
        self.events.append(event)

    def query(self, filt: Dict[str, Any]) -> List[Dict[str, Any]]:
        hits = [e for e in self.events if event_matches(e, filt)]
        # NIP-01: with a limit, a relay returns the *most recent* matches.
        hits.sort(key=lambda e: e['created_at'], reverse=True)
        limit = filt.get('limit')
        if isinstance(limit, int) and limit >= 0:
            hits = hits[:limit]
        return hits

    async def _handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                message = json.loads(msg.data)
            except ValueError:
                continue
            if not isinstance(message, list) or not message:
                continue
            verb = message[0]
            if verb == 'EVENT' and len(message) >= 2:
                event = message[1]
                if self.accept_writes:
                    self.store(event)
                    await ws.send_str(json.dumps(["OK", event['id'], True, ""]))
                else:
                    await ws.send_str(json.dumps(
                        ["OK", event['id'], False, "blocked: test relay is read-only"]))
            elif verb == 'REQ' and len(message) >= 3:
                sub_id = message[1]
                filters = message[2:]
                self.reqs.append(message)
                sent = []
                for filt in filters:
                    for event in self.query(filt):
                        if event['id'] in sent:
                            continue
                        sent.append(event['id'])
                        await ws.send_str(json.dumps(["EVENT", sub_id, event]))
                await ws.send_str(json.dumps(["EOSE", sub_id]))
            elif verb == 'CLOSE':
                pass
        return ws
