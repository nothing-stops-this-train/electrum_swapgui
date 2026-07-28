#!/usr/bin/env python3
"""Unit tests for the discoverability rule engine (``swapserver_gui.nostr_check``).

These pin the plugin's model of *why a taker rejects an offer* to what
``NostrTransport._get_pairs_loop`` in electrum/submarine_swaps.py actually does.
If upstream changes the filter or the order of its checks, these fail loudly --
which is the point: a wrong diagnosis is worse than none.

No relay, no network, no PyQt6.

Run with:  python3 -m pytest tests/test_nostr_check.py
"""
import json
import os
import sys
import time
import unittest

# --- make electrum + the plugin importable ---------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))  # /home/user/electrum_swapgui
_ELECTRUM_SRC = os.environ.get("ELECTRUM_SRC", os.path.join(_PROJECT_ROOT, "electrum"))
_PLUGINS_DIR = os.path.join(os.path.dirname(_HERE), "plugins")
for _p in (_ELECTRUM_SRC, _PLUGINS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from swapserver_gui import nostr_check as nc  # noqa: E402
from swapserver_gui import pow as swap_pow  # noqa: E402
from swapserver_gui.nostr_check import CheckStatus, EventView  # noqa: E402


NET = "signet"
VERSION = 5
PUBKEY = "11" * 32  # x-only, valid hex


def find_nonce(pubkey_hex: str, bits: int) -> int:
    """Smallest nonce giving at least ``bits`` of work. Only usable for small bits."""
    pubkey = bytes.fromhex(pubkey_hex)
    nonce = 1
    while swap_pow.pow_bits(pubkey, nonce) < bits:
        nonce += 1
    return nonce


def make_content(*, pow_nonce: int, **overrides) -> str:
    """The offer payload as ``NostrTransport.publish_offer`` builds it."""
    offer = {
        'percentage_fee': 0.5,
        'mining_fee': 1000,
        'min_amount': 20000,
        'max_forward_amount': 150000,
        'max_reverse_amount': 120000,
        'relays': 'wss://relay.example.com',
        'pow_nonce': hex(pow_nonce),
    }
    offer.update(overrides)
    for key in [k for k, v in offer.items() if v is _MISSING]:
        del offer[key]
    return json.dumps(offer)


_MISSING = object()


def make_view(*, pow_bits: int = 8, net: str = NET, version: int = VERSION,
              created_at=None, tags=None, content=None, pubkey: str = PUBKEY) -> EventView:
    if created_at is None:
        created_at = int(time.time())
    if tags is None:
        tags = [['d', f'electrum-swapserver-{version}'],
                ['r', f'net:{net}'],
                ['expiration', str(created_at + 610)]]
    if content is None:
        content = make_content(pow_nonce=find_nonce(pubkey, pow_bits))
    return EventView(pubkey=pubkey, created_at=created_at, tags=tags, content=content)


def evaluate(view, *, taker_pow_target=8, net=NET, version=VERSION, now_ts=None):
    return nc.evaluate_offer_event(
        view, net_name=net, event_version=version,
        taker_pow_target=taker_pow_target, pow_bits_fn=swap_pow.pow_bits,
        now_ts=now_ts)


class TestEvaluateOfferEvent(unittest.TestCase):

    def test_valid_offer_is_discoverable(self):
        verdict = evaluate(make_view(pow_bits=8), taker_pow_target=8)
        self.assertEqual(verdict.status, CheckStatus.DISCOVERABLE)
        self.assertTrue(verdict.is_ok)
        self.assertGreaterEqual(verdict.pow_bits, 8)

    def test_wrong_event_version_is_named(self):
        # a taker on Electrum v5 looking at a v4 announcement
        verdict = evaluate(make_view(version=4))
        self.assertEqual(verdict.status, CheckStatus.WRONG_VERSION)
        self.assertIn("electrum-swapserver-4", verdict.detail)
        self.assertIn("electrum-swapserver-5", verdict.detail)

    def test_wrong_network_is_named(self):
        # the exact reported symptom: a signet server nobody can find.
        # 'mutinynet' is its own NET_NAME upstream, not an alias for signet.
        verdict = evaluate(make_view(net="mutinynet"), net="signet")
        self.assertEqual(verdict.status, CheckStatus.WRONG_NET)
        self.assertIn("net:mutinynet", verdict.detail)
        self.assertIn("net:signet", verdict.detail)

    def test_offer_older_than_an_hour_is_stale(self):
        now = int(time.time())
        verdict = evaluate(make_view(created_at=now - 3601), now_ts=now)
        self.assertEqual(verdict.status, CheckStatus.STALE)

    def test_offer_exactly_one_hour_old_still_passes(self):
        # upstream compares with '<', so the boundary itself is accepted
        now = int(time.time())
        verdict = evaluate(make_view(created_at=now - 3600), now_ts=now)
        self.assertEqual(verdict.status, CheckStatus.DISCOVERABLE)

    def test_clock_skew_into_the_future_is_reported(self):
        now = int(time.time())
        verdict = evaluate(make_view(created_at=now + 7200), now_ts=now)
        self.assertEqual(verdict.status, CheckStatus.STALE)
        self.assertIn("clock", verdict.detail)

    def test_pow_below_taker_target_is_rejected(self):
        verdict = evaluate(make_view(pow_bits=4), taker_pow_target=16)
        self.assertEqual(verdict.status, CheckStatus.LOW_POW)
        self.assertIn("16", verdict.detail)
        self.assertLess(verdict.pow_bits, 16)

    def test_pow_equal_to_taker_target_is_accepted(self):
        # upstream rejects with '<', so meeting the target exactly is fine
        nonce = find_nonce(PUBKEY, 8)
        bits = swap_pow.pow_bits(bytes.fromhex(PUBKEY), nonce)
        verdict = evaluate(make_view(pow_bits=8), taker_pow_target=bits)
        self.assertEqual(verdict.status, CheckStatus.DISCOVERABLE)

    def test_malformed_content_is_unparseable(self):
        self.assertEqual(evaluate(make_view(content="not json")).status,
                         CheckStatus.UNPARSEABLE)
        self.assertEqual(evaluate(make_view(content='"a string"')).status,
                         CheckStatus.UNPARSEABLE)

    def test_three_element_tag_kills_the_whole_event(self):
        # upstream builds {k: v for k, v in event.tags}, which raises on a tag
        # that is not a 2-tuple and drops the event inside a bare except.
        now = int(time.time())
        tags = [['d', f'electrum-swapserver-{VERSION}'],
                ['r', f'net:{NET}'],
                ['e', 'id', 'wss://relay']]
        verdict = evaluate(make_view(created_at=now, tags=tags))
        self.assertEqual(verdict.status, CheckStatus.UNPARSEABLE)
        self.assertIn("two elements", verdict.detail)

    def test_non_hex_pow_nonce_is_unparseable(self):
        content = make_content(pow_nonce=1).replace('"0x1"', '"zzz"')
        self.assertEqual(evaluate(make_view(content=content)).status,
                         CheckStatus.UNPARSEABLE)

    def test_missing_payload_fields_are_unparseable(self):
        for field in ('percentage_fee', 'mining_fee', 'min_amount',
                      'max_forward_amount', 'max_reverse_amount', 'relays'):
            with self.subTest(field=field):
                content = make_content(pow_nonce=find_nonce(PUBKEY, 8),
                                       **{field: _MISSING})
                verdict = evaluate(make_view(content=content))
                self.assertEqual(verdict.status, CheckStatus.UNPARSEABLE)

    def test_check_order_matches_upstream(self):
        """Version is blamed before network, network before staleness, and both
        before proof of work -- the order _get_pairs_loop uses."""
        now = int(time.time())
        # wrong version AND wrong net AND stale AND low pow -> version wins
        view = make_view(version=4, net="mutinynet", created_at=now - 99999,
                         pow_bits=1)
        self.assertEqual(evaluate(view, now_ts=now, taker_pow_target=30).status,
                         CheckStatus.WRONG_VERSION)
        # wrong net AND stale AND low pow -> net wins
        view = make_view(net="mutinynet", created_at=now - 99999, pow_bits=1)
        self.assertEqual(evaluate(view, now_ts=now, taker_pow_target=30).status,
                         CheckStatus.WRONG_NET)
        # stale AND low pow -> stale wins
        view = make_view(created_at=now - 99999, pow_bits=1)
        self.assertEqual(evaluate(view, now_ts=now, taker_pow_target=30).status,
                         CheckStatus.STALE)


class TestTagDict(unittest.TestCase):

    def test_first_value_wins_per_name(self):
        view = EventView(pubkey=PUBKEY, created_at=0,
                         tags=[['d', 'one'], ['r', 'two']], content="{}")
        self.assertEqual(view.tag_dict(), {'d': 'one', 'r': 'two'})

    def test_bad_arity_returns_none(self):
        for tags in ([['d']], [['d', 'a', 'b']], [[]]):
            with self.subTest(tags=tags):
                view = EventView(pubkey=PUBKEY, created_at=0, tags=tags, content="{}")
                self.assertIsNone(view.tag_dict())

    def test_no_tags_is_an_empty_dict_not_none(self):
        view = EventView(pubkey=PUBKEY, created_at=0, tags=[], content="{}")
        self.assertEqual(view.tag_dict(), {})


class TestQueries(unittest.TestCase):
    """The taker filter must stay byte-identical to upstream's."""

    def test_taker_query_shape(self):
        now = 1_700_000_000
        query = nc.taker_query(kind=30315, net_name="signet",
                               event_version=5, now_ts=now)
        self.assertEqual(query, {
            "kinds": [30315],
            "limit": 10,
            "#d": ["electrum-swapserver-5"],
            "#r": ["net:signet"],
            "since": now - 3600,
        })

    def test_author_query_is_tag_agnostic(self):
        # query A must NOT filter on d/r: its whole job is to find our event
        # even when those tags are the thing that is wrong.
        query = nc.author_query(PUBKEY, kind=30315, now_ts=1_700_000_000)
        self.assertEqual(query["authors"], [PUBKEY])
        self.assertNotIn("#d", query)
        self.assertNotIn("#r", query)

    def test_tag_helpers(self):
        self.assertEqual(nc.d_tag_for(5), "electrum-swapserver-5")
        self.assertEqual(nc.r_tag_for("signet"), "net:signet")


class TestReport(unittest.TestCase):

    def _report(self, results, *, taker_pow_target=30):
        report = nc.DiscoveryReport(
            pubkey_hex=PUBKEY, npub="npub1...", net_name=NET,
            event_version=VERSION, kind=30315,
            taker_pow_target=taker_pow_target)
        report.results = results
        return report

    def test_headline_counts(self):
        ok = nc.RelayResult("wss://a", CheckStatus.DISCOVERABLE, "", pow_bits=30)
        bad = nc.RelayResult("wss://b", CheckStatus.NO_EVENT, "")
        self.assertIn("no relays", self._report([]).headline().lower())
        self.assertIn("Discoverable on 1 of 2", self._report([ok, bad]).headline())
        self.assertIn("Not discoverable on any", self._report([bad]).headline())

    def test_low_pow_warns_even_when_discoverable(self):
        """The trap: our own low target accepts an offer stock takers reject."""
        ok = nc.RelayResult("wss://a", CheckStatus.DISCOVERABLE, "", pow_bits=12)
        report = self._report([ok], taker_pow_target=12)
        self.assertEqual(report.ok_count, 1)
        warnings = " ".join(report.warnings())
        self.assertIn("12 bits", warnings)
        self.assertIn(str(nc.DEFAULT_TAKER_POW_TARGET), warnings)

    def test_no_warning_when_pow_meets_the_default(self):
        ok = nc.RelayResult("wss://a", CheckStatus.DISCOVERABLE, "", pow_bits=30)
        self.assertEqual(self._report([ok], taker_pow_target=30).warnings(), [])

    def test_crowded_out_is_called_out(self):
        crowded = nc.RelayResult("wss://a", CheckStatus.CROWDED_OUT, "", pow_bits=30)
        warnings = " ".join(self._report([crowded]).warnings())
        self.assertIn("busy relay", warnings)

    def test_best_pow_bits_ignores_unknowns(self):
        results = [nc.RelayResult("wss://a", CheckStatus.UNREACHABLE, ""),
                   nc.RelayResult("wss://b", CheckStatus.DISCOVERABLE, "", pow_bits=21)]
        self.assertEqual(self._report(results).best_pow_bits, 21)
        self.assertIsNone(self._report([results[0]]).best_pow_bits)


class TestFormatAge(unittest.TestCase):

    def test_units(self):
        self.assertEqual(nc.format_age(5), "5s")
        self.assertEqual(nc.format_age(600), "10 min")
        self.assertEqual(nc.format_age(7200), "2h")
        self.assertEqual(nc.format_age(3 * 86400), "3d")
        self.assertEqual(nc.format_age(-1), "just now")


if __name__ == '__main__':
    unittest.main()
