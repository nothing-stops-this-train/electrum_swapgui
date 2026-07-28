#!/usr/bin/env python3
"""End-to-end tests for the discoverability check, against a real relay.

The relay is the minimal NIP-01 server in ``fake_relay.py``, hosted in this
process, but everything above the socket is real: offers are published with
``electrum_aionostr._add_event`` exactly as ``NostrTransport.publish_offer``
does, they are signed and their signatures are verified by the client on
receipt, and the check under test is the one the GUI button calls.

The point of these tests is that the check must not just say "not found" -- it
has to name the field that is wrong, because that is the whole reason it exists.

Run with:  python3 -m pytest tests/test_discovery_e2e.py
"""
import asyncio
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

import electrum_aionostr as aionostr  # noqa: E402
from electrum_aionostr.key import PrivateKey  # noqa: E402
from electrum_aionostr.util import to_nip19  # noqa: E402

from swapserver_gui import nostr_check as nc  # noqa: E402
from swapserver_gui import pow as swap_pow  # noqa: E402
from swapserver_gui.nostr_check import CheckStatus  # noqa: E402

from fake_relay import FakeRelay  # noqa: E402


KIND = 30315          # NostrTransport.USER_STATUS_NIP38
VERSION = 5           # NostrTransport.NOSTR_EVENT_VERSION
NET = "signet"        # the chain the reported problem was on
POW_BITS = 8          # small enough to grind inline, real enough to be checked


def find_nonce(pubkey_hex: str, bits: int) -> int:
    pubkey = bytes.fromhex(pubkey_hex)
    nonce = 1
    while swap_pow.pow_bits(pubkey, nonce) < bits:
        nonce += 1
    return nonce


class _Server:
    """A swap server identity plus the offer it announces."""

    def __init__(self, *, pow_bits: int = POW_BITS) -> None:
        self.privkey = PrivateKey()
        self.nsec = to_nip19('nsec', self.privkey.hex())
        self.pubkey_hex = self.privkey.public_key.hex()
        self.npub = to_nip19('npub', self.pubkey_hex)
        self.pow_nonce = find_nonce(self.pubkey_hex, pow_bits)

    def offer_content(self, **overrides) -> str:
        offer = {
            'percentage_fee': 0.5,
            'mining_fee': 1000,
            'min_amount': 20000,
            'max_forward_amount': 150000,
            'max_reverse_amount': 120000,
            'relays': 'wss://relay.example.com',
            'pow_nonce': hex(self.pow_nonce),
        }
        offer.update(overrides)
        return json.dumps(offer)

    def tags(self, *, net: str = NET, version: int = VERSION, created_at: int):
        # identical to publish_offer's tag list
        return [['d', f'electrum-swapserver-{version}'],
                ['r', f'net:{net}'],
                ['expiration', str(created_at + 610)]]


async def publish(relay_url: str, server: _Server, *, net: str = NET,
                  version: int = VERSION, created_at=None, content=None) -> None:
    """Announce an offer the way the swap server does."""
    if created_at is None:
        created_at = int(time.time())
    manager = aionostr.Manager([relay_url], private_key=server.nsec, connect_timeout=5)
    await manager.connect()
    try:
        await aionostr._add_event(
            manager,
            kind=KIND,
            tags=server.tags(net=net, version=version, created_at=created_at),
            content=content if content is not None else server.offer_content(),
            created_at=created_at,
            private_key=server.nsec)
    finally:
        await manager.close()


async def run_check(relays, server: _Server, *, taker_pow_target: int = POW_BITS,
                    net: str = NET, version: int = VERSION):
    return await nc.run_discovery_check(
        relays=relays,
        pubkey_hex=server.pubkey_hex,
        npub=server.npub,
        net_name=net,
        event_version=version,
        kind=KIND,
        taker_pow_target=taker_pow_target,
        pow_bits_fn=swap_pow.pow_bits,
        network=None,
        connect_timeout=5,
        query_timeout=5.0,
    )


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=60))


class DiscoveryE2ETests(unittest.TestCase):

    def test_published_offer_is_discoverable(self):
        async def main():
            async with FakeRelay() as relay:
                server = _Server()
                await publish(relay.url, server)
                report = await run_check([relay.url], server)
                self.assertEqual(len(report.results), 1)
                result = report.results[0]
                self.assertEqual(result.status, CheckStatus.DISCOVERABLE, result.detail)
                self.assertEqual(report.ok_count, 1)
                self.assertIn("Discoverable on 1 of 1", report.headline())
                self.assertGreaterEqual(result.pow_bits, POW_BITS)
                self.assertIsNotNone(result.event_age_sec)
                # the relay really was asked the taker's question
                filters = [f for req in relay.reqs for f in req[2:]]
                self.assertTrue(any(
                    f.get("#d") == [f"electrum-swapserver-{VERSION}"]
                    and f.get("#r") == [f"net:{NET}"] for f in filters))
        run(main())

    def test_nothing_published_reports_no_event(self):
        async def main():
            async with FakeRelay() as relay:
                server = _Server()
                report = await run_check([relay.url], server)
                result = report.results[0]
                self.assertEqual(result.status, CheckStatus.NO_EVENT)
                self.assertIn("expired", result.detail)
                self.assertEqual(report.ok_count, 0)
        run(main())

    def test_relay_that_rejects_writes_reports_no_event(self):
        """A relay that silently drops our announcement looks like never having
        announced -- which is exactly what the operator needs to be told."""
        async def main():
            async with FakeRelay(accept_writes=False) as relay:
                server = _Server()
                # the relay answers ["OK", id, false, …]: the publish call
                # completes without raising, but nothing was stored.
                await publish(relay.url, server)
                self.assertEqual(relay.events, [])
                report = await run_check([relay.url], server)
                self.assertEqual(report.results[0].status, CheckStatus.NO_EVENT)
        run(main())

    def test_raising_the_taker_target_exposes_low_pow(self):
        """The silent killer: same event, stricter taker, no longer visible."""
        async def main():
            async with FakeRelay() as relay:
                server = _Server(pow_bits=POW_BITS)
                await publish(relay.url, server)

                lenient = await run_check([relay.url], server, taker_pow_target=POW_BITS)
                self.assertEqual(lenient.results[0].status, CheckStatus.DISCOVERABLE)

                strict = await run_check([relay.url], server, taker_pow_target=30)
                result = strict.results[0]
                self.assertEqual(result.status, CheckStatus.LOW_POW)
                self.assertIn("30", result.detail)
                self.assertEqual(strict.ok_count, 0)
        run(main())

    def test_low_pow_warning_survives_a_healthy_headline(self):
        """Discoverable to *us* but not to a stock taker: the report must say so."""
        async def main():
            async with FakeRelay() as relay:
                server = _Server(pow_bits=POW_BITS)
                await publish(relay.url, server)
                report = await run_check([relay.url], server, taker_pow_target=POW_BITS)
                self.assertEqual(report.ok_count, 1)
                warnings = " ".join(report.warnings())
                self.assertIn(str(nc.DEFAULT_TAKER_POW_TARGET), warnings)
        run(main())

    def test_wrong_network_tag_is_named(self):
        """A mutinynet server looking for signet takers, or vice versa."""
        async def main():
            async with FakeRelay() as relay:
                server = _Server()
                await publish(relay.url, server, net="mutinynet")
                report = await run_check([relay.url], server, net="signet")
                result = report.results[0]
                self.assertEqual(result.status, CheckStatus.WRONG_NET)
                self.assertIn("net:mutinynet", result.detail)
                self.assertIn("net:signet", result.detail)
        run(main())

    def test_wrong_event_version_is_named(self):
        async def main():
            async with FakeRelay() as relay:
                server = _Server()
                await publish(relay.url, server, version=4)
                report = await run_check([relay.url], server, version=5)
                result = report.results[0]
                self.assertEqual(result.status, CheckStatus.WRONG_VERSION)
                self.assertIn("electrum-swapserver-4", result.detail)
        run(main())

    def test_offer_older_than_the_lookback_window_reads_as_no_event(self):
        """A server that stopped announcing over an hour ago is simply gone.

        Both queries use ``since = now - 1h`` (upstream's window), so the relay
        still holds the event but never returns it. The operator is correctly
        told nothing is stored rather than being shown a stale success.
        (The explicit STALE verdict is reachable via clock skew; that path is
        covered directly in test_nostr_check.py.)
        """
        async def main():
            async with FakeRelay() as relay:
                server = _Server()
                await publish(relay.url, server, created_at=int(time.time()) - 7200)
                self.assertEqual(len(relay.events), 1)
                report = await run_check([relay.url], server)
                self.assertEqual(report.results[0].status, CheckStatus.NO_EVENT)
        run(main())

    def test_unreachable_relay_is_distinguished_from_a_missing_offer(self):
        async def main():
            async with FakeRelay() as relay:
                server = _Server()
                await publish(relay.url, server)
                dead = "ws://127.0.0.1:1"  # nothing listens here
                report = await run_check([relay.url, dead], server)
                by_relay = {r.relay: r for r in report.results}
                self.assertEqual(len(report.results), 2)
                self.assertEqual(by_relay[relay.url].status, CheckStatus.DISCOVERABLE)
                self.assertEqual(by_relay[dead].status, CheckStatus.UNREACHABLE)
                self.assertIn("Discoverable on 1 of 2", report.headline())
        run(main())

    def test_busy_relay_can_crowd_us_out(self):
        """Takers ask for 10 offers; a relay with newer ones can hide us."""
        async def main():
            async with FakeRelay() as relay:
                server = _Server()
                now = int(time.time())
                await publish(relay.url, server, created_at=now - 600)
                for i in range(nc.TAKER_QUERY_LIMIT):
                    other = _Server()
                    await publish(relay.url, other, created_at=now - i)
                report = await run_check([relay.url], server)
                result = report.results[0]
                self.assertEqual(result.status, CheckStatus.CROWDED_OUT, result.detail)
                self.assertIn("busy relay", " ".join(report.warnings()))
        run(main())

    def test_no_relays_configured(self):
        report = run(run_check([], _Server()))
        self.assertEqual(report.results, [])
        self.assertIn("No relays configured", report.headline())


if __name__ == '__main__':
    unittest.main()
