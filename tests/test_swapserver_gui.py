#!/usr/bin/env python3
"""Unit tests for the swapserver_gui plugin's server-lifecycle layer.

These tests exercise the non-Qt module (``swapserver_gui.swapserver_gui``) with
a real background asyncio loop but fully mocked config / wallet / swap-manager,
so they do not touch the network, aiohttp, or PyQt6.

Run with:  python3 -m pytest tests/test_swapserver_gui.py
(or unittest). ELECTRUM_SRC and the plugin dir are added to sys.path below.
"""
import asyncio
import os
import sys
import threading
import time
import unittest
from unittest import mock

# --- make electrum + the plugin importable ---------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))  # /home/user/electrum_swapgui
_ELECTRUM_SRC = os.environ.get("ELECTRUM_SRC", os.path.join(_PROJECT_ROOT, "electrum"))
_PLUGINS_DIR = os.path.join(os.path.dirname(_HERE), "plugins")
for p in (_ELECTRUM_SRC, _PLUGINS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from swapserver_gui.swapserver_gui import (  # noqa: E402
    SwapServerGuiPlugin, SwapServerError, ManagedHttpSwapServer,
    get_swap_summary,
)


class _Config:
    """Minimal stand-in for SimpleConfig with the attributes the plugin reads."""
    def __init__(self, *, port=None, relays="", fee=5000, pow_target=30):
        self.SWAPSERVER_PORT = port
        self.NOSTR_RELAYS = relays
        self.SWAPSERVER_FEE_MILLIONTHS = fee
        self.SWAPSERVER_POW_TARGET = pow_target
        self.SWAPSERVER_GUI_AUTOSTART = False


class _SwapManager:
    def __init__(self):
        self.is_server = False
        self.http_server = None
        self.percentage = None
        self._min_amount = 20000
        self._max_forward = None
        self._max_reverse = None
        self.mining_fee = None
        self.nostr_started = threading.Event()
        self.nostr_cancelled = threading.Event()
        self.pairs_updates = 0
        self.pow_calls = 0
        # by default there is no liquidity to advertise; tests that exercise
        # publish_now set _max_forward/_max_reverse (or rely on server_update_pairs).
        self._advertise_liquidity = False

    async def run_nostr_server(self):
        self.nostr_started.set()
        try:
            await asyncio.Event().wait()  # block forever until cancelled
        except asyncio.CancelledError:
            self.nostr_cancelled.set()
            raise

    async def set_nostr_proof_of_work(self):
        self.pow_calls += 1

    def server_update_pairs(self):
        self.pairs_updates += 1
        self.percentage = 0.5
        if self._advertise_liquidity:
            self._max_forward = 100000
            self._max_reverse = 100000


def _make_wallet(sm, *, nostr_keypair=None):
    wallet = mock.MagicMock()
    wallet.lnworker.swap_manager = sm
    wallet.lnworker.nostr_keypair = nostr_keypair
    wallet.has_password.return_value = False
    return wallet


class _LoopThread:
    """Runs a real asyncio loop in a background thread for the duration of a test."""
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def __enter__(self):
        self.thread.start()
        return self.loop

    def __exit__(self, *exc):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()


def _make_plugin(config):
    parent = mock.MagicMock()
    # SwapServerGuiPlugin -> BasePlugin registers @hook methods globally; this
    # class defines none, so nothing leaks into the global hooks table.
    return SwapServerGuiPlugin(parent, config, "swapserver_gui")


class CanRunTests(unittest.TestCase):
    def test_requires_wallet(self):
        p = _make_plugin(_Config(port=5455))
        self.assertIsNotNone(p.can_run())  # no wallet bound yet

    def test_requires_some_transport(self):
        p = _make_plugin(_Config(port=None, relays=""))
        p.bind_wallet(_make_wallet(_SwapManager()))
        self.assertIsNotNone(p.can_run())  # neither http nor nostr configured

    def test_ok_with_port(self):
        p = _make_plugin(_Config(port=5455))
        p.bind_wallet(_make_wallet(_SwapManager()))
        self.assertIsNone(p.can_run())

    def test_ok_with_relays(self):
        p = _make_plugin(_Config(relays="wss://a,wss://b"))
        p.bind_wallet(_make_wallet(_SwapManager()))
        self.assertIsNone(p.can_run())


class NostrLifecycleTests(unittest.TestCase):
    def test_start_stop_nostr_only(self):
        sm = _SwapManager()
        config = _Config(port=None, relays="wss://relay.one,wss://relay.two")
        p = _make_plugin(config)
        p.bind_wallet(_make_wallet(sm))
        with _LoopThread() as loop:
            with mock.patch("swapserver_gui.swapserver_gui.get_asyncio_loop", return_value=loop):
                p.start_server()
                self.assertTrue(p.is_running())
                self.assertTrue(sm.is_server)
                self.assertTrue(sm.nostr_started.wait(timeout=5))
                # status reflects nostr transport
                st = p.status()
                self.assertTrue(st["nostr_enabled"])
                self.assertEqual(st["nostr_relay_count"], 2)
                self.assertFalse(st["http_enabled"])

                p.stop_server()
                self.assertTrue(sm.nostr_cancelled.wait(timeout=5))
                self.assertFalse(p.is_running())
                self.assertFalse(sm.is_server)

    def test_start_is_idempotent(self):
        sm = _SwapManager()
        p = _make_plugin(_Config(relays="wss://relay.one"))
        p.bind_wallet(_make_wallet(sm))
        with _LoopThread() as loop:
            with mock.patch("swapserver_gui.swapserver_gui.get_asyncio_loop", return_value=loop):
                p.start_server()
                self.assertTrue(sm.nostr_started.wait(timeout=5))
                first_task = p._nostr_fut
                p.start_server()  # no-op
                self.assertIs(p._nostr_fut, first_task)
                p.stop_server()
                self.assertTrue(sm.nostr_cancelled.wait(timeout=5))

    def test_start_raises_when_unconfigured(self):
        p = _make_plugin(_Config(port=None, relays=""))
        p.bind_wallet(_make_wallet(_SwapManager()))
        with self.assertRaises(SwapServerError):
            p.start_server()

    def test_restart_does_not_block_gui_thread_when_loop_busy(self):
        # Regression: start_server/stop_server must never .result() on the caller
        # (GUI) thread. Previously a restart while the asyncio loop was busy (e.g.
        # generating the nostr announcement PoW) blocked ~10s and raised
        # TimeoutError, crashing the GUI on "Save settings".
        sm = _SwapManager()
        p = _make_plugin(_Config(relays="wss://relay.one"))
        p.bind_wallet(_make_wallet(sm))
        with _LoopThread() as loop:
            with mock.patch("swapserver_gui.swapserver_gui.get_asyncio_loop", return_value=loop):
                p.start_server()
                self.assertTrue(sm.nostr_started.wait(timeout=5))
                # Occupy the loop so it cannot service new work for ~3s.
                loop.call_soon_threadsafe(lambda: time.sleep(2))
                t0 = time.monotonic()
                p.stop_server()          # must not block
                p.start_server()         # must not block or raise TimeoutError
                elapsed = time.monotonic() - t0
                self.assertLess(elapsed, 1.0, f"restart blocked the caller for {elapsed:.2f}s")
                self.assertTrue(p.is_running())
                p.stop_server()


class HttpLifecycleTests(unittest.TestCase):
    def test_http_server_created_and_stopped(self):
        sm = _SwapManager()
        config = _Config(port=5455, relays="")
        p = _make_plugin(config)
        p.bind_wallet(_make_wallet(sm))

        started = threading.Event()
        stopped = threading.Event()

        class _FakeHttp:
            def __init__(self, cfg, wallet):
                self.site = None
            async def run(self):
                self.site = object()
                started.set()
            async def stop(self):
                self.site = None
                stopped.set()

        with _LoopThread() as loop:
            with mock.patch("swapserver_gui.swapserver_gui.get_asyncio_loop", return_value=loop), \
                 mock.patch("swapserver_gui.swapserver_gui.ManagedHttpSwapServer", _FakeHttp):
                p.start_server()
                self.assertTrue(started.wait(timeout=5))
                self.assertIsNotNone(sm.http_server)
                p.stop_server()
                self.assertTrue(stopped.wait(timeout=5))
                self.assertIsNone(sm.http_server)
                self.assertFalse(p.is_running())

    def test_managed_http_subclasses_upstream(self):
        # guard against upstream renaming the base class / route handlers
        from electrum.plugins.swapserver.server import HttpSwapServer
        self.assertTrue(issubclass(ManagedHttpSwapServer, HttpSwapServer))
        for handler in ("get_pairs", "create_swap", "create_normal_swap", "add_swap_invoice"):
            self.assertTrue(hasattr(ManagedHttpSwapServer, handler))


class RequestPairsUpdateTests(unittest.TestCase):
    def test_update_scheduled_when_running(self):
        sm = _SwapManager()
        p = _make_plugin(_Config(relays="wss://relay.one"))
        p.bind_wallet(_make_wallet(sm))
        with _LoopThread() as loop:
            with mock.patch("swapserver_gui.swapserver_gui.get_asyncio_loop", return_value=loop):
                p.start_server()
                p.request_pairs_update()
                # give the loop a moment to run the scheduled callback
                deadline = time.time() + 5
                while sm.pairs_updates == 0 and time.time() < deadline:
                    time.sleep(0.05)
                self.assertGreaterEqual(sm.pairs_updates, 1)
                p.stop_server()
                self.assertTrue(sm.nostr_cancelled.wait(timeout=5))

    def test_update_noop_when_stopped(self):
        sm = _SwapManager()
        p = _make_plugin(_Config(relays="wss://relay.one"))
        p.bind_wallet(_make_wallet(sm))
        with _LoopThread() as loop:
            with mock.patch("swapserver_gui.swapserver_gui.get_asyncio_loop", return_value=loop):
                p.request_pairs_update()  # not running -> nothing scheduled
                time.sleep(0.2)
                self.assertEqual(sm.pairs_updates, 0)


class PublishNowTests(unittest.TestCase):
    @staticmethod
    def _fake_transport_cls(published, *, connect=True):
        class _FakeTransport:
            def __init__(self, cfg, sm_, keypair):
                self.connect_timeout = 1
                self.is_connected = asyncio.Event()
                if connect:
                    self.is_connected.set()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def publish_offer(self, sm_):
                published.set()
        return _FakeTransport

    def test_start_server_publishes_immediately(self):
        # Regression: a freshly-started server must announce promptly instead of
        # waiting for run_nostr_server's first OFFER_UPDATE_INTERVAL_SEC (~10min)
        # tick, otherwise takers see "no swap providers found" right after boot.
        sm = _SwapManager()
        sm._advertise_liquidity = True
        p = _make_plugin(_Config(relays="wss://relay.one"))
        p.bind_wallet(_make_wallet(sm, nostr_keypair=object()))
        published = threading.Event()
        with _LoopThread() as loop:
            with mock.patch("swapserver_gui.swapserver_gui.get_asyncio_loop", return_value=loop), \
                 mock.patch("swapserver_gui.swapserver_gui.NostrTransport",
                            self._fake_transport_cls(published)):
                p.start_server()
                self.assertTrue(published.wait(timeout=5))
                self.assertGreaterEqual(sm.pow_calls, 1)  # PoW ensured before announce
                p.stop_server()

    def test_publish_now_waits_for_liquidity(self):
        # No liquidity yet -> must not announce until server_update_pairs reports
        # some (mirrors channels being funded shortly after the server starts).
        sm = _SwapManager()  # _advertise_liquidity stays False initially
        p = _make_plugin(_Config(relays="wss://relay.one"))
        p.bind_wallet(_make_wallet(sm, nostr_keypair=object()))
        published = threading.Event()
        with _LoopThread() as loop:
            with mock.patch("swapserver_gui.swapserver_gui.get_asyncio_loop", return_value=loop), \
                 mock.patch("swapserver_gui.swapserver_gui.NostrTransport",
                            self._fake_transport_cls(published)), \
                 mock.patch.object(SwapServerGuiPlugin, "PUBLISH_NOW_POLL_SEC", 0.05):
                p.start_server()
                self.assertFalse(published.wait(timeout=1))  # no liquidity -> no announce
                sm._advertise_liquidity = True                # channels get funded
                self.assertTrue(published.wait(timeout=5))    # now it announces
                p.stop_server()

    def test_publish_now_noop_when_stopped(self):
        sm = _SwapManager()
        p = _make_plugin(_Config(relays="wss://relay.one"))
        p.bind_wallet(_make_wallet(sm, nostr_keypair=object()))
        self.assertIsNone(p.publish_now())  # not running

    def test_publish_now_noop_without_keypair(self):
        sm = _SwapManager()
        sm._advertise_liquidity = True
        p = _make_plugin(_Config(relays="wss://relay.one"))
        p.bind_wallet(_make_wallet(sm, nostr_keypair=None))
        with _LoopThread() as loop:
            with mock.patch("swapserver_gui.swapserver_gui.get_asyncio_loop", return_value=loop):
                p.start_server()  # must not spawn a publish without a keypair
                self.assertIsNone(p._publish_now_fut)
                p.stop_server()


class SummaryTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(
            get_swap_summary([]),
            {'num_swaps': 0, 'overall_return_sat': 0, 'swaps_per_day': 0.0},
        )

    def test_aggregates_and_rate(self):
        day = 86400
        history = [
            {'return_sat': 100, 'timestamp': 0, 'date': 'x', 'label': 'a'},
            {'return_sat': -30, 'timestamp': day, 'date': 'y', 'label': 'b'},
            {'return_sat': 50, 'timestamp': 2 * day, 'date': 'z', 'label': 'c'},
        ]
        summary = get_swap_summary(history)
        self.assertEqual(summary['num_swaps'], 3)
        self.assertEqual(summary['overall_return_sat'], 120)
        # 3 swaps over 2 days
        self.assertEqual(summary['swaps_per_day'], 1.5)


if __name__ == "__main__":
    unittest.main()
