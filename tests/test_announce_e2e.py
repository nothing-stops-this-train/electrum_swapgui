#!/usr/bin/env python3
"""End-to-end tests for the announce loop, against a real relay.

Everything above the socket is real: the plugin's own ``start_server``, its
announce loop, Electrum's ``NostrTransport``, ``electrum_aionostr``, real
secp256k1 signing, and the minimal NIP-01 relay from ``fake_relay.py`` hosted in
this process.  Only the wallet, the swap manager and the network object are
stubbed, because announcing needs none of them to be real.

The three properties under test are the ones the plugin took the announce loop
over for (see ANNOUNCE LOOP in swapserver_gui.py):

  * a started server puts an offer on the relay that a taker's own filter finds;
  * it keeps re-announcing, so the offer never outlives its NIP-40 expiration;
  * a relay that was unreachable at start-up is picked up when it comes back.
    Upstream's ``run_nostr_server`` blocks on ``is_connected`` with no timeout
    and ``aionostr.Manager.connect`` permanently drops relays that missed its
    single attempt, so upstream stays silent for the rest of the process. This
    is the reported bug, reproduced end to end.

Run with:  python3 -m pytest tests/test_announce_e2e.py
"""
import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from typing import Any, Dict, List, Optional
from unittest import mock

# --- make electrum + the plugin importable ---------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))  # /home/user/electrum_swapgui
_ELECTRUM_SRC = os.environ.get("ELECTRUM_SRC", os.path.join(_PROJECT_ROOT, "electrum"))
_PLUGINS_DIR = os.path.join(os.path.dirname(_HERE), "plugins")
for _p in (_ELECTRUM_SRC, _PLUGINS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import electrum.submarine_swaps as submarine_swaps  # noqa: E402
from electrum.submarine_swaps import NostrTransport  # noqa: E402
from electrum_aionostr.key import PrivateKey  # noqa: E402

from swapserver_gui import nostr_check as nc  # noqa: E402
from swapserver_gui.nostr_check import CheckStatus  # noqa: E402
from swapserver_gui.swapserver_gui import (  # noqa: E402
    AnnounceState, SwapServerGuiPlugin,
)

from fake_relay import FakeRelay  # noqa: E402

KIND = NostrTransport.USER_STATUS_NIP38
POW_TARGET = 8  # small enough to grind inline, real enough to be verified


def _free_port() -> int:
    """Reserve and release a port, so a test can bind it again later."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Keypair:
    """What ``lnworker.nostr_keypair`` provides: a 33-byte compressed pubkey."""

    def __init__(self) -> None:
        privkey = PrivateKey()
        self.privkey: bytes = bytes.fromhex(privkey.hex())
        # NostrTransport uses keypair.pubkey.hex()[2:], i.e. x-only with the
        # compressed prefix dropped; nostr keys are always even-Y, hence 0x02.
        self.pubkey: bytes = bytes([0x02]) + bytes.fromhex(privkey.public_key.hex())

    @property
    def xonly_hex(self) -> str:
        return self.pubkey.hex()[2:]


class _Config:
    def __init__(self, *, relays: str, path: str) -> None:
        self.SWAPSERVER_PORT = None
        self.NOSTR_RELAYS = relays
        self.SWAPSERVER_FEE_MILLIONTHS = 5000
        self.SWAPSERVER_POW_TARGET = POW_TARGET
        self.SWAPSERVER_ANN_POW_NONCE = 0
        self.SWAPSERVER_GUI_AUTOSTART = False
        self.SWAPSERVER_GUI_POW_STATE = None
        self.path = path  # NostrTransport reads recent_swapserver_relays from it


class _Network:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.proxy = None
        self.asyncio_loop = loop


class _SwapManager:
    """Only the parts the announce path reads."""

    def __init__(self, config: _Config, network: _Network) -> None:
        self.config = config
        self.network = network
        self.is_server = False
        self.http_server = None
        self.percentage = 0.5
        self._min_amount = 20000
        self._max_forward = 150000
        self._max_reverse = 120000
        self.mining_fee = 1000

    def server_update_pairs(self) -> None:
        pass


class _LoopThread:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def __enter__(self) -> asyncio.AbstractEventLoop:
        self.thread.start()
        t0 = time.monotonic()
        while not self.loop.is_running():
            time.sleep(0.01)
            if time.monotonic() - t0 > 5:
                raise RuntimeError("loop would not start")
        return self.loop

    def __exit__(self, *exc: Any) -> None:
        # Cancel and drain first: aionostr keeps websocket readers and queue
        # waiters alive, and closing the loop underneath them turns every one of
        # them into an unraisable-exception warning.
        async def _drain() -> None:
            tasks = [t for t in asyncio.all_tasks(self.loop)
                     if t is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            asyncio.run_coroutine_threadsafe(_drain(), self.loop).result(timeout=10)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()


class _Rig:
    """A plugin wired to a real transport, with the asyncio loop patched in."""

    def __init__(self, testcase: unittest.TestCase, *, relay_url: str,
                 patches: Any = ()) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        testcase.addCleanup(self.tmpdir.cleanup)
        self.keypair = _Keypair()
        self.config = _Config(relays=relay_url, path=self.tmpdir.name)
        self.extra_patches = list(patches)
        self._started: List[Any] = []
        self.loop_thread = _LoopThread()

    def __enter__(self) -> 'SwapServerGuiPlugin':
        loop = self.loop_thread.__enter__()
        self.network = _Network(loop)
        self.sm = _SwapManager(self.config, self.network)
        wallet = mock.MagicMock()
        wallet.lnworker.swap_manager = self.sm
        wallet.lnworker.nostr_keypair = self.keypair
        wallet.has_password.return_value = False
        self.plugin = SwapServerGuiPlugin(mock.MagicMock(), self.config, "swapserver_gui")
        self.plugin.bind_wallet(wallet)
        patches = [
            mock.patch("swapserver_gui.swapserver_gui.get_asyncio_loop", return_value=loop),
            # NostrTransport.get_relay_manager asserts it runs on Electrum's loop
            mock.patch.object(submarine_swaps, "get_asyncio_loop", return_value=loop),
            *self.extra_patches,
        ]
        for ctx in patches:
            ctx.start()
            self._started.append(ctx)
        return self.plugin

    def __exit__(self, *exc: Any) -> None:
        try:
            self.plugin.stop_server()
        finally:
            for ctx in reversed(self._started):
                ctx.stop()
            self.loop_thread.__exit__()

    def run_check(self, relays: List[str]) -> Any:
        """The discoverability check the GUI button runs, as a taker would."""
        coro = nc.run_discovery_check(
            relays=relays,
            pubkey_hex=self.keypair.xonly_hex,
            npub=self.plugin.nostr_identity()[1],
            net_name=self.plugin.nostr_match_fields()["net_name"],
            event_version=NostrTransport.NOSTR_EVENT_VERSION,
            kind=KIND,
            taker_pow_target=POW_TARGET,
            pow_bits_fn=__import__("swapserver_gui.pow", fromlist=["pow"]).pow_bits,
            network=None,
            connect_timeout=5,
            query_timeout=5.0,
        )
        return asyncio.run_coroutine_threadsafe(
            coro, self.loop_thread.loop).result(timeout=30)


def _offers(relay: FakeRelay) -> List[Dict[str, Any]]:
    return [e for e in relay.events if e.get('kind') == KIND]


def _wait_for_offers(relay: FakeRelay, count: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while len(_offers(relay)) < count and time.time() < deadline:
        time.sleep(0.05)
    return len(_offers(relay)) >= count


class _RelayThread:
    """Hosts a FakeRelay on its own loop, so tests can start/stop it freely."""

    def __init__(self, *, port: int = 0) -> None:
        self.relay = FakeRelay(port=port)
        self.loop_thread = _LoopThread()

    def __enter__(self) -> FakeRelay:
        loop = self.loop_thread.__enter__()
        asyncio.run_coroutine_threadsafe(self.relay.start(), loop).result(timeout=10)
        return self.relay

    def __exit__(self, *exc: Any) -> None:
        try:
            asyncio.run_coroutine_threadsafe(
                self.relay.stop(), self.loop_thread.loop).result(timeout=10)
        except Exception:
            pass
        self.loop_thread.__exit__()


class AnnounceE2ETests(unittest.TestCase):

    def test_started_server_becomes_discoverable(self):
        with _RelayThread() as relay:
            rig = _Rig(self, relay_url=relay.url)
            with rig as plugin:
                plugin.start_server()
                self.assertTrue(_wait_for_offers(relay, 1),
                                "the server never announced")
                # the offer must carry what a taker parses...
                content = json.loads(_offers(relay)[0]['content'])
                self.assertEqual(content['min_amount'], 20000)
                self.assertEqual(content['max_forward_amount'], 150000)
                self.assertEqual(content['percentage_fee'], 0.5)
                # ...and the plugin must know it actually landed
                st = plugin.status()
                self.assertIsNotNone(st["last_publish_success_at"])
                self.assertEqual(st["last_publish_note"], "accepted by a relay")
                self.assertIs(st["announce_state"], AnnounceState.ANNOUNCING)
                # The event is composed by the plugin rather than by upstream's
                # publish_offer, so the check that matters is whether a taker's
                # own filter still matches it.
                report = rig.run_check([relay.url])
                self.assertEqual([r.status for r in report.results],
                                 [CheckStatus.DISCOVERABLE],
                                 [r.detail for r in report.results])


class RepublishE2ETests(unittest.TestCase):

    def test_the_offer_is_refreshed_before_it_expires(self):
        # The reported symptom: a long-running server stops being found because
        # the announcement has a ~10 minute NIP-40 expiration.
        patches = [
            mock.patch.object(SwapServerGuiPlugin, "LIQUIDITY_POLL_SEC", 0.2),
            mock.patch.object(SwapServerGuiPlugin, "REPUBLISH_INTERVAL_SEC", 0.5),
        ]
        with _RelayThread() as relay:
            with _Rig(self, relay_url=relay.url, patches=patches) as plugin:
                plugin.start_server()
                self.assertTrue(_wait_for_offers(relay, 3),
                                f"only {len(_offers(relay))} announcement(s) went out; "
                                f"the offer would have expired")
                first, last = _offers(relay)[0], _offers(relay)[-1]
                # each re-announcement carries a fresh expiration, which is what
                # keeps the offer alive on relays that enforce NIP-40
                def expiry(event):
                    return int(dict((t[0], t[1]) for t in event['tags'])['expiration'])
                self.assertGreaterEqual(expiry(last), expiry(first))
                self.assertGreater(last['created_at'], 0)


class RelayRecoveryE2ETests(unittest.TestCase):

    def test_a_relay_that_was_down_at_startup_is_picked_up(self):
        # Upstream cannot recover from this at all: run_nostr_server waits on
        # is_connected forever, and Manager.connect has already dropped the relay
        # from its list. The plugin must rebuild the transport until it works.
        port = _free_port()
        url = f"ws://127.0.0.1:{port}"
        patches = [
            mock.patch.object(SwapServerGuiPlugin, "TRANSPORT_RETRY_MIN_SEC", 0.5),
            mock.patch.object(SwapServerGuiPlugin, "TRANSPORT_RETRY_MAX_SEC", 1),
            mock.patch.object(SwapServerGuiPlugin, "LIQUIDITY_POLL_SEC", 0.5),
        ]
        with _Rig(self, relay_url=url, patches=patches) as plugin:
            plugin.start_server()
            # nothing is reachable yet, and the plugin must say so rather than
            # claiming to announce
            deadline = time.time() + 30
            state = None
            while time.time() < deadline:
                state = plugin.status()["announce_state"]
                if state is AnnounceState.NO_RELAY_CONNECTED:
                    break
                time.sleep(0.05)
            self.assertIs(state, AnnounceState.NO_RELAY_CONNECTED)
            self.assertIsNone(plugin.status()["last_publish_success_at"])

            # the relay comes back on the address the server was configured with
            with _RelayThread(port=port) as relay:
                self.assertTrue(_wait_for_offers(relay, 1),
                                "never recovered after the relay came back")
                self.assertIsNotNone(plugin.status()["last_publish_success_at"])
                self.assertIs(plugin.status()["announce_state"],
                              AnnounceState.ANNOUNCING)


if __name__ == "__main__":
    unittest.main()
