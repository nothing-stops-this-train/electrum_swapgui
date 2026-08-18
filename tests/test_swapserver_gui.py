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

from electrum.submarine_swaps import NostrTransport as _RealNostrTransport  # noqa: E402

from swapserver_gui.swapserver_gui import (  # noqa: E402
    AnnounceState, SwapServerGuiPlugin, SwapServerError, ManagedHttpSwapServer,
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
        self.pairs_updates = 0
        self.pow_calls = 0
        self.upstream_nostr_calls = 0
        # by default there is no liquidity to advertise; tests that exercise the
        # announce loop set _max_forward/_max_reverse (or flip the flag below).
        self._advertise_liquidity = False

    async def run_nostr_server(self):
        # The plugin owns the announce loop now (see ANNOUNCE LOOP in
        # swapserver_gui.py). Reaching this would reintroduce every failure mode
        # that motivated taking it over, so tests assert it stays at zero.
        self.upstream_nostr_calls += 1
        await asyncio.Event().wait()

    async def set_nostr_proof_of_work(self):
        self.pow_calls += 1

    def server_update_pairs(self):
        self.pairs_updates += 1
        self.percentage = 0.5
        if self._advertise_liquidity:
            self._max_forward = 100000
            self._max_reverse = 100000


async def _never_finishing_pow(self):
    """Stand-in for ensure_pow_nonce that never opens the gate (still cancellable)."""
    await asyncio.Event().wait()


class _TransportRecorder:
    """Builds NostrTransport stand-ins and records what the announce loop did.

    The loop is expected to *discard and rebuild* transports (that is the only
    way to recover a relay list aionostr has pruned), so the interesting
    assertions are about how many were built and whether each connected.
    """

    def __init__(self, *, connect=True, connect_timeout=0.2):
        self.connect = connect
        self.connect_timeout = connect_timeout
        self.built = 0
        self.entered = threading.Event()
        self.torn_down = threading.Event()
        self.transports = []

    def cls(self):
        rec = self

        class _FakeTransport:
            # the plugin also reads these off the class (nostr_match_fields,
            # build_offer_tags), so they must carry upstream's real values
            NOSTR_EVENT_VERSION = _RealNostrTransport.NOSTR_EVENT_VERSION
            USER_STATUS_NIP38 = _RealNostrTransport.USER_STATUS_NIP38
            OFFER_UPDATE_INTERVAL_SEC = _RealNostrTransport.OFFER_UPDATE_INTERVAL_SEC

            def __init__(self, config, sm, keypair):
                self.connect_timeout = rec.connect_timeout
                self.is_connected = asyncio.Event()
                # what publish_offer_event would reach for
                self.relay_manager = object()
                self.nostr_private_key = "nsec1test"
                self.stopped = False
                rec.built += 1
                rec.transports.append(self)

            async def __aenter__(self):
                if rec.connect:
                    self.is_connected.set()
                rec.entered.set()
                return self

            async def __aexit__(self, *exc):
                self.stopped = True
                rec.torn_down.set()
                return False

            async def stop(self):
                self.stopped = True

        return _FakeTransport

    def wait_entered(self, timeout=5):
        return self.entered.wait(timeout=timeout)


class _PublishRecorder:
    """Stand-in for publish_offer_event: records calls, can fail on demand."""

    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = 0
        self.published = threading.Event()

    async def __call__(self, transport, sm):
        self.calls += 1
        self.published.set()
        if self.fail:
            raise asyncio.TimeoutError("no relay acknowledged")
        return f"event-{self.calls}"

    def wait(self, timeout=5):
        return self.published.wait(timeout=timeout)

    def wait_for_calls(self, n, timeout=5):
        deadline = time.time() + timeout
        while self.calls < n and time.time() < deadline:
            time.sleep(0.02)
        return self.calls >= n


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
        # Let cancelled tasks unwind before the loop goes away, otherwise
        # asyncio logs "Task was destroyed but it is pending". Draining via a
        # probe (rather than a fixed sleep) also covers tests that deliberately
        # keep the loop busy for a while.
        try:
            asyncio.run_coroutine_threadsafe(asyncio.sleep(0.1), self.loop).result(timeout=5)
        except Exception:
            pass
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
        sm._advertise_liquidity = True
        config = _Config(port=None, relays="wss://relay.one,wss://relay.two")
        p = _make_plugin(config)
        p.bind_wallet(_make_wallet(sm, nostr_keypair=object()))
        rec, pub = _TransportRecorder(), _PublishRecorder()
        with _LoopThread() as loop:
            with mock.patch("swapserver_gui.swapserver_gui.get_asyncio_loop", return_value=loop), \
                 mock.patch("swapserver_gui.swapserver_gui.NostrTransport", rec.cls()), \
                 mock.patch("swapserver_gui.swapserver_gui.publish_offer_event", pub):
                p.start_server()
                self.assertTrue(p.is_running())
                self.assertTrue(sm.is_server)
                self.assertTrue(rec.wait_entered())
                # status reflects nostr transport
                st = p.status()
                self.assertTrue(st["nostr_enabled"])
                self.assertEqual(st["nostr_relay_count"], 2)
                self.assertFalse(st["http_enabled"])

                p.stop_server()
                self.assertTrue(rec.torn_down.wait(timeout=5))
                self.assertFalse(p.is_running())
                self.assertFalse(sm.is_server)
        self.assertEqual(sm.upstream_nostr_calls, 0)

    def test_start_is_idempotent(self):
        sm = _SwapManager()
        p = _make_plugin(_Config(relays="wss://relay.one"))
        p.bind_wallet(_make_wallet(sm, nostr_keypair=object()))
        rec = _TransportRecorder()
        with _LoopThread() as loop:
            with mock.patch("swapserver_gui.swapserver_gui.get_asyncio_loop", return_value=loop), \
                 mock.patch("swapserver_gui.swapserver_gui.NostrTransport", rec.cls()):
                p.start_server()
                self.assertTrue(rec.wait_entered())
                first_task = p._nostr_fut
                p.start_server()  # no-op
                self.assertIs(p._nostr_fut, first_task)
                p.stop_server()
                self.assertTrue(rec.torn_down.wait(timeout=5))

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
        p.bind_wallet(_make_wallet(sm, nostr_keypair=object()))
        rec = _TransportRecorder()
        with _LoopThread() as loop:
            with mock.patch("swapserver_gui.swapserver_gui.get_asyncio_loop", return_value=loop), \
                 mock.patch("swapserver_gui.swapserver_gui.NostrTransport", rec.cls()):
                p.start_server()
                self.assertTrue(rec.wait_entered())
                # Occupy the loop so it cannot service new work for ~3s.
                loop.call_soon_threadsafe(lambda: time.sleep(2))
                t0 = time.monotonic()
                p.stop_server()          # must not block
                p.start_server()         # must not block or raise TimeoutError
                elapsed = time.monotonic() - t0
                self.assertLess(elapsed, 1.0, f"restart blocked the caller for {elapsed:.2f}s")
                self.assertTrue(p.is_running())
                p.stop_server()

    def test_start_clears_bookkeeping_from_the_previous_run(self):
        # A restart must not carry the previous run's "last announcement" over:
        # reporting a stale timestamp as if it belonged to the running server is
        # the reporting bug this whole loop was written to kill.
        sm = _SwapManager()
        p = _make_plugin(_Config(relays="wss://relay.one"))
        p.bind_wallet(_make_wallet(sm, nostr_keypair=object()))
        rec = _TransportRecorder()
        with _LoopThread() as loop:
            with mock.patch("swapserver_gui.swapserver_gui.get_asyncio_loop", return_value=loop), \
                 mock.patch("swapserver_gui.swapserver_gui.NostrTransport", rec.cls()):
                p.start_server()
                self.assertTrue(rec.wait_entered())
                p._last_publish_attempt_at = 1.0
                p._last_publish_success_at = 2.0
                p._last_publish_note = "from the previous run"
                p._consecutive_publish_failures = 7
                p.stop_server()
                p.start_server()
                st = p.status()
                self.assertIsNone(st["last_publish_attempt_at"])
                self.assertIsNone(st["last_publish_success_at"])
                self.assertIsNone(st["last_publish_note"])
                self.assertEqual(st["consecutive_publish_failures"], 0)
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

    def test_update_noop_when_stopped(self):
        sm = _SwapManager()
        p = _make_plugin(_Config(relays="wss://relay.one"))
        p.bind_wallet(_make_wallet(sm))
        with _LoopThread() as loop:
            with mock.patch("swapserver_gui.swapserver_gui.get_asyncio_loop", return_value=loop):
                p.request_pairs_update()  # not running -> nothing scheduled
                time.sleep(0.2)
                self.assertEqual(sm.pairs_updates, 0)


class AnnounceLoopTests(unittest.TestCase):
    """The loop that replaced ``SwapManager.run_nostr_server``.

    Everything here is about the one property the old code could not offer: the
    offer keeps going out, and when it does not, the plugin knows.
    """

    def _run(self, *, sm, config, keypair=object(), recorder=None, publisher=None,
             patches=(), body=None):
        """Start the server with the announce path faked out, run ``body``, stop."""
        p = _make_plugin(config)
        p.bind_wallet(_make_wallet(sm, nostr_keypair=keypair))
        rec = recorder if recorder is not None else _TransportRecorder()
        pub = publisher if publisher is not None else _PublishRecorder()
        with _LoopThread() as loop:
            stack = [
                mock.patch("swapserver_gui.swapserver_gui.get_asyncio_loop", return_value=loop),
                mock.patch("swapserver_gui.swapserver_gui.NostrTransport", rec.cls()),
                mock.patch("swapserver_gui.swapserver_gui.publish_offer_event", pub),
                *patches,
            ]
            for ctx in stack:
                ctx.start()
            try:
                p.start_server()
                body(p, rec, pub)
            finally:
                p.stop_server()
                for ctx in reversed(stack):
                    ctx.stop()
        return p

    # ------------------------------------------------------- announcing at all
    def test_announces_as_soon_as_the_transport_connects(self):
        # A freshly-started server must announce promptly rather than waiting a
        # full tick, otherwise takers see "no swap providers found" after boot.
        sm = _SwapManager()
        sm._advertise_liquidity = True

        def body(p, rec, pub):
            self.assertTrue(pub.wait())
            self.assertEqual(p.status()["announce_state"], AnnounceState.ANNOUNCING)
            self.assertIsNotNone(p.status()["last_publish_success_at"])

        self._run(sm=sm, config=_Config(relays="wss://relay.one"), body=body)
        # The plugin owns the proof-of-work (see pow.py): reaching upstream's
        # set_nostr_proof_of_work would route into gen_nostr_ann_pow, which
        # wedges the event loop thread if cancelled mid-grind.
        self.assertEqual(sm.pow_calls, 0)
        self.assertEqual(sm.upstream_nostr_calls, 0)

    def test_waits_for_pow_gate(self):
        # An announcement made before the PoW is ready carries a nonce takers
        # reject, so nothing may be published until the gate opens.
        sm = _SwapManager()
        sm._advertise_liquidity = True

        def body(p, rec, pub):
            self.assertFalse(pub.wait(timeout=1.5),
                             "announced before the proof-of-work was ready")

        self._run(sm=sm, config=_Config(relays="wss://relay.one"),
                  patches=[mock.patch.object(SwapServerGuiPlugin, "ensure_pow_nonce",
                                             _never_finishing_pow)],
                  body=body)

    def test_waits_for_liquidity_then_announces(self):
        # No liquidity yet -> nothing published until server_update_pairs reports
        # some (mirrors channels being funded shortly after the server starts).
        sm = _SwapManager()  # _advertise_liquidity stays False initially

        def body(p, rec, pub):
            self.assertFalse(pub.wait(timeout=1))
            self.assertEqual(p.status()["announce_state"], AnnounceState.NO_LIQUIDITY)
            sm._advertise_liquidity = True   # channels get funded
            self.assertTrue(pub.wait(timeout=5))

        self._run(sm=sm, config=_Config(relays="wss://relay.one"),
                  patches=[mock.patch.object(SwapServerGuiPlugin, "LIQUIDITY_POLL_SEC", 0.05)],
                  body=body)

    def test_no_keypair_never_builds_a_transport(self):
        sm = _SwapManager()
        sm._advertise_liquidity = True

        def body(p, rec, pub):
            time.sleep(0.3)
            self.assertEqual(rec.built, 0)
            self.assertEqual(pub.calls, 0)

        self._run(sm=sm, config=_Config(relays="wss://relay.one"), keypair=None,
                  body=body)

    # ------------------------------------------------------------ re-announcing
    def test_republishes_on_its_own_schedule(self):
        # The reported bug: a server that has been up for a long time must keep
        # re-announcing, because the offer carries a ~10 minute NIP-40 expiry.
        sm = _SwapManager()
        sm._advertise_liquidity = True

        def body(p, rec, pub):
            self.assertTrue(pub.wait_for_calls(3, timeout=5),
                            f"only announced {pub.calls} time(s); the offer would expire")
            # ...all on one transport: re-announcing must not churn connections.
            self.assertEqual(rec.built, 1)

        self._run(sm=sm, config=_Config(relays="wss://relay.one"),
                  patches=[
                      mock.patch.object(SwapServerGuiPlugin, "LIQUIDITY_POLL_SEC", 0.05),
                      mock.patch.object(SwapServerGuiPlugin, "REPUBLISH_INTERVAL_SEC", 0.1),
                  ],
                  body=body)

    def test_republishes_immediately_when_the_offer_changes(self):
        # Upstream's rule, kept: an offer that no longer matches reality is worse
        # than a slightly late one.
        sm = _SwapManager()
        sm._advertise_liquidity = True

        def body(p, rec, pub):
            self.assertTrue(pub.wait_for_calls(1))
            before = pub.calls
            sm.mining_fee = 12345          # liquidity/fee change
            self.assertTrue(pub.wait_for_calls(before + 1, timeout=5))

        self._run(sm=sm, config=_Config(relays="wss://relay.one"),
                  patches=[
                      mock.patch.object(SwapServerGuiPlugin, "LIQUIDITY_POLL_SEC", 0.05),
                      mock.patch.object(SwapServerGuiPlugin, "REPUBLISH_INTERVAL_SEC", 600),
                  ],
                  body=body)

    def test_publish_now_wakes_the_loop(self):
        sm = _SwapManager()
        sm._advertise_liquidity = True

        def body(p, rec, pub):
            self.assertTrue(pub.wait_for_calls(1))
            before = pub.calls
            p.publish_now()
            self.assertTrue(pub.wait_for_calls(before + 1, timeout=5))
            # ...and without building a second transport, which used to mean two
            # NostrTransports handling the same pubkey's DMs at once.
            self.assertEqual(rec.built, 1)

        self._run(sm=sm, config=_Config(relays="wss://relay.one"),
                  patches=[
                      mock.patch.object(SwapServerGuiPlugin, "LIQUIDITY_POLL_SEC", 30),
                      mock.patch.object(SwapServerGuiPlugin, "REPUBLISH_INTERVAL_SEC", 600),
                  ],
                  body=body)

    def test_publish_now_noop_when_stopped(self):
        sm = _SwapManager()
        p = _make_plugin(_Config(relays="wss://relay.one"))
        p.bind_wallet(_make_wallet(sm, nostr_keypair=object()))
        self.assertIsNone(p.publish_now())  # not running, and must not raise

    # --------------------------------------------------------------- recovery
    def test_unreachable_relays_are_retried_with_a_new_transport(self):
        # aionostr's Manager.connect drops relays that missed its single attempt
        # and never retries, so the only cure is a brand-new transport. Upstream
        # instead waits forever on is_connected and never announces again.
        sm = _SwapManager()
        sm._advertise_liquidity = True
        rec = _TransportRecorder(connect=False, connect_timeout=0.05)

        def body(p, rec_, pub):
            deadline = time.time() + 5
            while rec_.built < 2 and time.time() < deadline:
                time.sleep(0.02)
            self.assertGreaterEqual(rec_.built, 2,
                                    "gave up instead of rebuilding the transport")
            self.assertEqual(p.status()["announce_state"],
                             AnnounceState.NO_RELAY_CONNECTED)

        self._run(sm=sm, config=_Config(relays="wss://relay.one"), recorder=rec,
                  patches=[mock.patch.object(SwapServerGuiPlugin,
                                             "TRANSPORT_RETRY_MIN_SEC", 0.05)],
                  body=body)

    def test_unacknowledged_publishes_are_reported_not_hidden(self):
        # Upstream's publish_offer is @ignore_exceptions and swallows
        # TimeoutError internally, so a server nobody accepts looks identical to
        # a healthy one. Recycling is disabled here so the state stays put long
        # enough to assert on.
        sm = _SwapManager()
        sm._advertise_liquidity = True
        pub = _PublishRecorder(fail=True)

        def body(p, rec, pub_):
            self.assertTrue(pub_.wait_for_calls(2, timeout=5))
            st = p.status()
            self.assertEqual(st["announce_state"], AnnounceState.PUBLISH_FAILING)
            self.assertIsNone(st["last_publish_success_at"])
            self.assertIsNotNone(st["last_publish_attempt_at"])
            self.assertIn("TimeoutError", st["last_publish_note"])
            self.assertGreaterEqual(st["consecutive_publish_failures"], 2)

        self._run(sm=sm, config=_Config(relays="wss://relay.one"), publisher=pub,
                  patches=[
                      mock.patch.object(SwapServerGuiPlugin, "LIQUIDITY_POLL_SEC", 0.05),
                      mock.patch.object(SwapServerGuiPlugin, "REPUBLISH_INTERVAL_SEC", 0.05),
                      mock.patch.object(SwapServerGuiPlugin,
                                        "PUBLISH_FAILURES_BEFORE_RECYCLE", 10_000),
                  ],
                  body=body)

    def test_repeated_publish_failures_recycle_the_transport(self):
        sm = _SwapManager()
        sm._advertise_liquidity = True
        pub = _PublishRecorder(fail=True)

        def body(p, rec, pub_):
            deadline = time.time() + 5
            while rec.built < 2 and time.time() < deadline:
                time.sleep(0.02)
            self.assertGreaterEqual(rec.built, 2,
                                    "kept using a transport no relay answers")

        self._run(sm=sm, config=_Config(relays="wss://relay.one"), publisher=pub,
                  patches=[
                      mock.patch.object(SwapServerGuiPlugin, "LIQUIDITY_POLL_SEC", 0.05),
                      mock.patch.object(SwapServerGuiPlugin, "REPUBLISH_INTERVAL_SEC", 0.05),
                      mock.patch.object(SwapServerGuiPlugin, "TRANSPORT_RETRY_MIN_SEC", 0.05),
                  ],
                  body=body)

    def test_a_transport_that_cannot_be_built_does_not_kill_the_loop(self):
        # Building a NostrTransport reads sm.network and the config, either of
        # which can be missing while a wallet is still loading. An exception
        # there must not end the loop -- a server whose announce task exited
        # silently is the state all of this exists to prevent.
        sm = _SwapManager()
        sm._advertise_liquidity = True
        rec = _TransportRecorder()
        real_cls = rec.cls()
        attempts = []

        class _ExplodingOnce(real_cls):
            def __init__(self, config, sm_, keypair):
                attempts.append(1)
                if len(attempts) == 1:
                    raise AttributeError("no attribute 'network'")
                super().__init__(config, sm_, keypair)

        def body(p, rec_, pub):
            self.assertTrue(pub.wait(timeout=5),
                            "the loop died on the first transport failure")
            self.assertGreaterEqual(len(attempts), 2)
            self.assertFalse(p.nostr_task_failed())

        self._run(sm=sm, config=_Config(relays="wss://relay.one"), recorder=rec,
                  patches=[
                      mock.patch("swapserver_gui.swapserver_gui.NostrTransport",
                                 _ExplodingOnce),
                      mock.patch.object(SwapServerGuiPlugin,
                                        "TRANSPORT_RETRY_MIN_SEC", 0.05),
                  ],
                  body=body)

    def test_a_dead_announce_task_is_visible_and_restarted(self):
        sm = _SwapManager()
        sm._advertise_liquidity = True

        def body(p, rec, pub):
            self.assertTrue(pub.wait())
            # Kill the loop the way an unabsorbed error would.
            async def _die():
                raise RuntimeError("boom")
            p._cancel_fut(p._nostr_fut)
            p._nostr_fut = p._spawn(_die(), "nostr")
            deadline = time.time() + 5
            while not p.nostr_task_failed() and time.time() < deadline:
                time.sleep(0.02)
            self.assertTrue(p.nostr_task_failed())
            self.assertEqual(p.status()["announce_state"], AnnounceState.TASK_DEAD)
            before = rec.built
            p.supervise()
            deadline = time.time() + 5
            while rec.built <= before and time.time() < deadline:
                time.sleep(0.02)
            self.assertGreater(rec.built, before, "supervise did not restart the loop")
            self.assertFalse(p.nostr_task_failed())

        self._run(sm=sm, config=_Config(relays="wss://relay.one"), body=body)

    def test_supervise_is_throttled(self):
        # supervise() is driven by a 4s GUI timer; a failure that reproduces on
        # every start must not be retried several times a minute forever.
        sm = _SwapManager()
        sm._advertise_liquidity = True

        def body(p, rec, pub):
            self.assertTrue(pub.wait())

            async def _die():
                raise RuntimeError("boom")

            for _ in range(3):
                p._cancel_fut(p._nostr_fut)
                p._nostr_fut = p._spawn(_die(), "nostr")
                deadline = time.time() + 5
                while not p.nostr_task_failed() and time.time() < deadline:
                    time.sleep(0.02)
                p.supervise()
            # only the first of the three restarts is allowed through
            self.assertTrue(p.nostr_task_failed())

        self._run(sm=sm, config=_Config(relays="wss://relay.one"),
                  patches=[mock.patch.object(SwapServerGuiPlugin,
                                             "NOSTR_RESTART_MIN_INTERVAL_SEC", 60)],
                  body=body)

    def test_supervise_is_a_noop_while_healthy(self):
        sm = _SwapManager()
        sm._advertise_liquidity = True

        def body(p, rec, pub):
            self.assertTrue(pub.wait())
            fut = p._nostr_fut
            for _ in range(3):
                p.supervise()
            self.assertIs(p._nostr_fut, fut)
            self.assertEqual(rec.built, 1)

        self._run(sm=sm, config=_Config(relays="wss://relay.one"), body=body)


class SummaryTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(
            get_swap_summary([]),
            {'num_swaps': 0, 'overall_return_sat': 0, 'swaps_per_day': 0.0,
             'num_batched': 0, 'num_incomplete': 0, 'num_unattributed': 0},
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
        self.assertEqual(summary['num_batched'], 0)

    def test_counts_batched_entries(self):
        history = [
            {'return_sat': 100, 'timestamp': 0, 'date': 'x', 'label': 'a'},
            {'return_sat': 50, 'timestamp': 10, 'date': 'y', 'label': 'b',
             'batched_with': 1},
        ]
        summary = get_swap_summary(history)
        self.assertEqual(summary['num_swaps'], 2)
        self.assertEqual(summary['num_batched'], 1)
        # every row contributes its own value; sharing a transaction with
        # another swap no longer makes a row's value approximate.
        self.assertEqual(summary['overall_return_sat'], 150)


if __name__ == "__main__":
    unittest.main()
