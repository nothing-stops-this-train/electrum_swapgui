#!/usr/bin/env python3
"""Unit tests for the announcement-state machine and the nostr identity.

Covers the plugin-side half of "why can nobody see my swap server?":

  * :func:`announce_state` -- the GUI must never claim to be announcing while
    the announcement path is actually blocked.
  * :func:`has_liquidity_to_announce` -- mirrors the gate in
    ``NostrTransport.publish_offer`` that silently suppresses the offer.
  * the nostr pubkey/npub shown in the Status tab.

No relay, no network, no PyQt6.

Run with:  python3 -m pytest tests/test_diagnostics.py
"""
import os
import sys
import unittest
from unittest import mock

# --- make electrum + the plugin importable ---------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))  # /home/user/electrum_swapgui
_ELECTRUM_SRC = os.environ.get("ELECTRUM_SRC", os.path.join(_PROJECT_ROOT, "electrum"))
_PLUGINS_DIR = os.path.join(os.path.dirname(_HERE), "plugins")
for _p in (_ELECTRUM_SRC, _PLUGINS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from electrum import constants  # noqa: E402
from electrum.submarine_swaps import NostrTransport, MIN_SWAP_AMOUNT_SAT  # noqa: E402
from electrum_aionostr.util import from_nip19  # noqa: E402

from swapserver_gui.swapserver_gui import (  # noqa: E402
    AnnounceState, SwapServerGuiPlugin, SwapServerError, announce_state,
    build_offer_payload, build_offer_tags, has_liquidity_to_announce,
)
from swapserver_gui import nostr_check as nc  # noqa: E402


# A valid compressed pubkey: 33 bytes, the first of which is the prefix that
# both the PoW preimage and the nostr x-only encoding drop.
PUBKEY_33 = bytes([0x02]) + bytes(range(32))
XONLY_HEX = PUBKEY_33[1:].hex()


class _Config:
    def __init__(self, *, relays="", pow_target=30, nonce=0):
        self.SWAPSERVER_PORT = None
        self.NOSTR_RELAYS = relays
        self.SWAPSERVER_FEE_MILLIONTHS = 5000
        self.SWAPSERVER_POW_TARGET = pow_target
        self.SWAPSERVER_ANN_POW_NONCE = nonce
        self.SWAPSERVER_GUI_AUTOSTART = False
        self.SWAPSERVER_GUI_POW_STATE = None


class _SwapManager:
    def __init__(self):
        self.is_server = False
        self.http_server = None
        self.network = None
        self.percentage = None
        self._min_amount = None
        self._max_forward = None
        self._max_reverse = None
        self.mining_fee = None


def _make_plugin(config, *, keypair=None, has_password=False, unlocked=True):
    # BasePlugin registers @hook methods globally; SwapServerGuiPlugin defines
    # none, so nothing leaks into the global hooks table.
    plugin = SwapServerGuiPlugin(mock.MagicMock(), config, "swapserver_gui")
    sm = _SwapManager()
    wallet = mock.MagicMock()
    wallet.lnworker.swap_manager = sm
    wallet.lnworker.nostr_keypair = keypair
    wallet.has_password.return_value = has_password
    wallet.get_unlocked_password.return_value = "pw" if unlocked else None
    plugin.bind_wallet(wallet)
    return plugin, sm


class TestHasLiquidityToAnnounce(unittest.TestCase):
    """Mirror of ``publish_offer``'s gate: announce if EITHER direction fits."""

    def test_forward_alone_is_enough(self):
        self.assertTrue(has_liquidity_to_announce(
            min_amount=20000, max_forward=20000, max_reverse=0))

    def test_reverse_alone_is_enough(self):
        self.assertTrue(has_liquidity_to_announce(
            min_amount=20000, max_forward=0, max_reverse=50000))

    def test_both_below_minimum_suppresses_the_offer(self):
        self.assertFalse(has_liquidity_to_announce(
            min_amount=20000, max_forward=19999, max_reverse=19999))

    def test_boundary_is_inclusive(self):
        # upstream bails on '<', so exactly the minimum is publishable
        self.assertTrue(has_liquidity_to_announce(
            min_amount=20000, max_forward=20000, max_reverse=0))
        self.assertFalse(has_liquidity_to_announce(
            min_amount=20000, max_forward=19999, max_reverse=0))

    def test_none_means_not_yet_computed(self):
        # server_update_pairs has not run: upstream raises TypeError on the
        # comparison and @ignore_exceptions swallows it -> nothing published.
        for kwargs in (dict(min_amount=None, max_forward=1, max_reverse=1),
                       dict(min_amount=20000, max_forward=None, max_reverse=1),
                       dict(min_amount=20000, max_forward=1, max_reverse=None)):
            with self.subTest(**kwargs):
                self.assertFalse(has_liquidity_to_announce(**kwargs))

    def test_matches_the_real_minimum(self):
        self.assertEqual(MIN_SWAP_AMOUNT_SAT, 20_000)


class TestAnnounceState(unittest.TestCase):

    BASE = dict(running=True, nostr_enabled=True, wallet_locked=False,
                pow_grinding=False, min_amount=20000, max_forward=50000,
                max_reverse=50000)

    def state(self, **overrides):
        return announce_state(**{**self.BASE, **overrides})

    def test_healthy_server_announces(self):
        self.assertIs(self.state(), AnnounceState.ANNOUNCING)
        self.assertTrue(self.state().is_announcing)

    def test_no_relays_is_disabled(self):
        self.assertIs(self.state(nostr_enabled=False), AnnounceState.DISABLED)

    def test_stopped_server(self):
        self.assertIs(self.state(running=False), AnnounceState.STOPPED)

    def test_locked_wallet_blocks_everything(self):
        self.assertIs(self.state(wallet_locked=True), AnnounceState.WAITING_UNLOCK)

    def test_pow_grinding(self):
        self.assertIs(self.state(pow_grinding=True), AnnounceState.WAITING_POW)

    def test_no_liquidity_is_not_announcing(self):
        """The reported symptom: everything looks running, nothing is published."""
        self.assertIs(self.state(max_forward=0, max_reverse=0),
                      AnnounceState.NO_LIQUIDITY)
        self.assertFalse(self.state(max_forward=0, max_reverse=0).is_announcing)

    def test_precedence(self):
        # relays first, then running, then unlock, then pow, then liquidity
        self.assertIs(self.state(nostr_enabled=False, running=False,
                                 wallet_locked=True, pow_grinding=True,
                                 max_forward=0, max_reverse=0),
                      AnnounceState.DISABLED)
        self.assertIs(self.state(running=False, wallet_locked=True,
                                 pow_grinding=True, max_forward=0, max_reverse=0),
                      AnnounceState.STOPPED)
        self.assertIs(self.state(wallet_locked=True, pow_grinding=True,
                                 max_forward=0, max_reverse=0),
                      AnnounceState.WAITING_UNLOCK)
        self.assertIs(self.state(pow_grinding=True, max_forward=0, max_reverse=0),
                      AnnounceState.WAITING_POW)

    # -- states the announce loop reports ----------------------------------
    # Each of these used to be indistinguishable from ANNOUNCING, which is the
    # worst possible answer: the operator believes the server is discoverable.

    def test_dead_announce_task(self):
        self.assertIs(self.state(task_dead=True), AnnounceState.TASK_DEAD)

    def test_no_relay_connected(self):
        self.assertIs(self.state(relay_connected=False),
                      AnnounceState.NO_RELAY_CONNECTED)
        self.assertFalse(self.state(relay_connected=False).is_announcing)

    def test_publish_failing(self):
        self.assertIs(self.state(publish_failing=True),
                      AnnounceState.PUBLISH_FAILING)
        self.assertFalse(self.state(publish_failing=True).is_announcing)

    def test_loop_health_defaults_to_the_healthy_answer(self):
        # Callers that predate the announce loop keep the original five-way
        # classification rather than being told the server is broken.
        self.assertIs(self.state(), AnnounceState.ANNOUNCING)

    def test_loop_health_precedence(self):
        # A dead task outranks everything except "not configured / not running":
        # nothing else can be true while nothing is running the loop.
        self.assertIs(self.state(task_dead=True, wallet_locked=True,
                                 pow_grinding=True, relay_connected=False,
                                 publish_failing=True),
                      AnnounceState.TASK_DEAD)
        self.assertIs(self.state(running=False, task_dead=True),
                      AnnounceState.STOPPED)
        # A missing relay is diagnosed before liquidity, because the loop needs
        # a connection before the liquidity gate is ever consulted.
        self.assertIs(self.state(relay_connected=False, max_forward=0,
                                 max_reverse=0, publish_failing=True),
                      AnnounceState.NO_RELAY_CONNECTED)
        # ...and no liquidity before failing publishes, because with nothing to
        # advertise there is no publish to fail.
        self.assertIs(self.state(max_forward=0, max_reverse=0,
                                 publish_failing=True),
                      AnnounceState.NO_LIQUIDITY)


class TestOfferEvent(unittest.TestCase):
    """The announcement we build must stay byte-compatible with upstream's.

    The plugin composes and sends the offer itself (``publish_offer_event``)
    because upstream's ``publish_offer`` cannot report whether a relay accepted
    it.  The price of that is drift risk: if upstream changes the payload or the
    tags, takers stop understanding us.  These tests are the tripwire.
    """

    def _sm(self):
        sm = _SwapManager()
        sm.config = _Config(relays="wss://a,wss://b", nonce=0x1234)
        sm.percentage = 0.5
        sm.mining_fee = 1000
        sm._min_amount = 20000
        sm._max_forward = 150000
        sm._max_reverse = 120000
        return sm

    @staticmethod
    def _upstream_offer_keys():
        """The keys ``NostrTransport.publish_offer`` puts in its content dict."""
        import inspect
        import re
        source = inspect.getsource(NostrTransport.publish_offer)
        body = source.split("offer = {", 1)[1].split("}", 1)[0]
        return set(re.findall(r"'([a-z_]+)':", body))

    def test_payload_keys_match_upstream(self):
        self.assertEqual(set(build_offer_payload(self._sm())),
                         self._upstream_offer_keys())

    def test_payload_values(self):
        payload = build_offer_payload(self._sm())
        self.assertEqual(payload['percentage_fee'], 0.5)
        self.assertIsInstance(payload['percentage_fee'], float)  # <=4.7.1 compat
        self.assertEqual(payload['mining_fee'], 1000)
        self.assertEqual(payload['min_amount'], 20000)
        self.assertEqual(payload['max_forward_amount'], 150000)
        self.assertEqual(payload['max_reverse_amount'], 120000)
        self.assertEqual(payload['relays'], "wss://a,wss://b")
        self.assertEqual(payload['pow_nonce'], hex(0x1234))

    def test_tags_match_what_takers_filter_on(self):
        tags = dict((t[0], t[1]) for t in build_offer_tags(now_ts=1_000_000))
        self.assertEqual(tags['d'], nc.d_tag_for(NostrTransport.NOSTR_EVENT_VERSION))
        self.assertEqual(tags['r'], nc.r_tag_for(constants.net.NET_NAME))
        # upstream's NIP-40 window, unchanged: the expiry gap is fixed by
        # re-announcing sooner, not by claiming a longer life for the event
        self.assertEqual(
            int(tags['expiration']),
            1_000_000 + NostrTransport.OFFER_UPDATE_INTERVAL_SEC + 10)

    def test_republish_interval_stays_inside_the_expiry(self):
        # The whole point of owning the loop: upstream republishes at 600s
        # against a 610s expiry, checked on a 30s tick and stamped after the
        # publish, so the offer is expired on strict relays for part of each
        # cycle. Ours must leave real headroom.
        lifetime = NostrTransport.OFFER_UPDATE_INTERVAL_SEC + 10
        self.assertLess(SwapServerGuiPlugin.REPUBLISH_INTERVAL_SEC, lifetime / 1.5)


class TestNostrIdentity(unittest.TestCase):

    def test_npub_round_trips_to_the_xonly_pubkey(self):
        keypair = mock.Mock(pubkey=PUBKEY_33)
        plugin, _ = _make_plugin(_Config(), keypair=keypair)
        identity = plugin.nostr_identity()
        self.assertIsNotNone(identity)
        pubkey_hex, npub = identity
        # must match NostrTransport.nostr_pubkey == keypair.pubkey.hex()[2:]
        self.assertEqual(pubkey_hex, XONLY_HEX)
        self.assertEqual(pubkey_hex, PUBKEY_33.hex()[2:])
        self.assertTrue(npub.startswith('npub1'))
        self.assertEqual(from_nip19(npub)['object'].hex(), pubkey_hex)

    def test_no_keypair_yields_no_identity(self):
        plugin, _ = _make_plugin(_Config(), keypair=None)
        self.assertIsNone(plugin.nostr_identity())

    def test_match_fields_follow_upstream(self):
        plugin, _ = _make_plugin(_Config())
        fields = plugin.nostr_match_fields()
        self.assertEqual(fields["net_name"], constants.net.NET_NAME)
        self.assertEqual(fields["event_version"], NostrTransport.NOSTR_EVENT_VERSION)
        self.assertEqual(fields["kind"], NostrTransport.USER_STATUS_NIP38)
        self.assertEqual(fields["d_tag"],
                         f"electrum-swapserver-{NostrTransport.NOSTR_EVENT_VERSION}")
        self.assertEqual(fields["r_tag"], f"net:{constants.net.NET_NAME}")


class TestWalletLocked(unittest.TestCase):

    def test_unprotected_wallet_is_never_locked(self):
        plugin, _ = _make_plugin(_Config(), has_password=False, unlocked=False)
        self.assertFalse(plugin.wallet_is_locked())

    def test_protected_and_not_unlocked(self):
        plugin, _ = _make_plugin(_Config(), has_password=True, unlocked=False)
        self.assertTrue(plugin.wallet_is_locked())

    def test_protected_but_unlocked(self):
        plugin, _ = _make_plugin(_Config(), has_password=True, unlocked=True)
        self.assertFalse(plugin.wallet_is_locked())


class TestStatusSnapshot(unittest.TestCase):

    def test_status_exposes_the_diagnostics(self):
        keypair = mock.Mock(pubkey=PUBKEY_33)
        plugin, sm = _make_plugin(
            _Config(relays="wss://a,wss://b", pow_target=30), keypair=keypair)
        sm._min_amount = 20000
        sm._max_forward = 0
        sm._max_reverse = 0
        st = plugin.status()
        self.assertEqual(st["nostr_pubkey"], XONLY_HEX)
        self.assertTrue(st["nostr_npub"].startswith("npub1"))
        self.assertEqual(st["net_name"], constants.net.NET_NAME)
        self.assertEqual(st["min_swap_amount"], MIN_SWAP_AMOUNT_SAT)
        self.assertEqual(st["pow_default_taker_target"], nc.DEFAULT_TAKER_POW_TARGET)
        self.assertEqual(st["pow_bits_achieved"], 0)  # nonce 0 == no work
        # server not running -> STOPPED, regardless of the missing liquidity
        self.assertIs(st["announce_state"], AnnounceState.STOPPED)

    def test_status_has_every_key_the_qt_tab_reads(self):
        # The Qt tests are skipped wherever PyQt6 is absent (CI included), so
        # nothing else catches a status() key the 4s refresh reads but status()
        # stopped providing -- which surfaces as a KeyError every 4 seconds.
        import re
        qt_source = os.path.join(_PLUGINS_DIR, "swapserver_gui", "qt.py")
        with open(qt_source, encoding="utf-8") as f:
            wanted = set(re.findall(r'st\[["\']([a-z_]+)["\']\]', f.read()))
        self.assertTrue(wanted, "found no st[...] lookups; the regex is stale")
        keypair = mock.Mock(pubkey=PUBKEY_33)
        plugin, _ = _make_plugin(_Config(relays="wss://a"), keypair=keypair)
        self.assertEqual(wanted - set(plugin.status()), set())

    def test_no_liquidity_reason_names_the_threshold(self):
        keypair = mock.Mock(pubkey=PUBKEY_33)
        plugin, sm = _make_plugin(_Config(relays="wss://a"), keypair=keypair)
        sm._min_amount = 20000
        sm._max_forward = 0
        sm._max_reverse = 0
        plugin._running = True
        st = plugin.status()
        self.assertIs(st["announce_state"], AnnounceState.NO_LIQUIDITY)
        reason = plugin.announcement_reason(st)
        self.assertIn("20,000 sat", reason)
        self.assertIn("no liquidity", reason.lower())

    def test_every_state_has_a_reason(self):
        plugin, _ = _make_plugin(_Config())
        for state in AnnounceState:
            with self.subTest(state=state):
                st = plugin.status()
                st["announce_state"] = state
                reason = plugin.announcement_reason(st)
                self.assertTrue(reason)
                self.assertTrue(reason.endswith("."))


class TestCheckDiscoverabilityGuards(unittest.TestCase):
    """The button must fail loudly rather than silently doing nothing."""

    def test_no_nostr_key(self):
        plugin, _ = _make_plugin(_Config(relays="wss://a"), keypair=None)
        with self.assertRaises(SwapServerError) as ctx:
            plugin.check_discoverability()
        self.assertIn("nostr key", str(ctx.exception))

    def test_no_relays_configured(self):
        keypair = mock.Mock(pubkey=PUBKEY_33)
        plugin, _ = _make_plugin(_Config(relays=""), keypair=keypair)
        with self.assertRaises(SwapServerError) as ctx:
            plugin.check_discoverability()
        self.assertIn("relay", str(ctx.exception))

    def test_relay_list_is_trimmed(self):
        plugin, _ = _make_plugin(_Config(relays=" wss://a , ,wss://b "))
        self.assertEqual(plugin.relay_list(), ["wss://a", "wss://b"])


if __name__ == '__main__':
    unittest.main()
