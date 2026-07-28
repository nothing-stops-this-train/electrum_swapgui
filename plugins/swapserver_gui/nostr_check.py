#!/usr/bin/env python
#
# swapserver_gui - a Qt GUI plugin for Electrum's submarine swap server.
# This file is released into the public domain (The Unlicense); see LICENSE.
#
# "Can a taker actually see me?" -- nostr identity helpers and a self-check that
# queries our own relays with a taker's exact filter.
#
# WHY THIS MODULE EXISTS
# ----------------------
# A swap server can be running, connected and grinding a valid proof of work and
# still be completely invisible, because every rejection on the taker side is
# silent.  ``SwapManager``'s client loop (electrum/submarine_swaps.py,
# ``NostrTransport._get_pairs_loop``) drops a candidate offer with at most a
# ``debug`` log line when any of these do not line up:
#
#   * kind                -> ``USER_STATUS_NIP38`` (30315)
#   * tag ``d``           -> ``electrum-swapserver-<NOSTR_EVENT_VERSION>``
#     (so two wallets running different Electrum versions never see each other)
#   * tag ``r``           -> ``net:<constants.net.NET_NAME>``
#     (note that 'signet' and 'mutinynet' are *different* NET_NAMEs)
#   * ``created_at``      -> within +/- 1h of the taker's clock
#   * proof of work       -> at least the *taker's* ``SWAPSERVER_POW_TARGET``
#
# That last one is the nastiest: ``SWAPSERVER_POW_TARGET`` means "how many bits
# to grind" on the server and "how many bits I demand" on the taker, so a server
# whose operator lowered it to save CPU is dropped by every taker still on the
# default of 30 -- with nothing shown in either GUI.
#
# On top of that, the server may never have published at all:
# ``NostrTransport.publish_offer`` returns early when there is no liquidity to
# advertise, and the announcement carries a NIP-40 ``expiration`` tag, so relays
# that honour it drop the event ~10 minutes after the server goes away.
#
# So we ask the relays directly.  For each configured relay we run two queries:
#
#   A. broad, by author: does *our* announcement exist on this relay at all?
#   B. the taker's verbatim filter: do we show up in what a taker actually asks
#      for?  (Only run when A produced an event that passes every rule -- it
#      distinguishes "valid but crowded out of the ``limit``" from the rest.)
#
# Splitting it in two is what lets us name the failing field instead of just
# reporting "not found".

import asyncio
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence

# ``pow.pow_bits``: (x-only pubkey, nonce) -> leading zero bits.
PowBitsFn = Callable[[bytes, Optional[int]], int]

if TYPE_CHECKING:
    from electrum.simple_config import SimpleConfig
    from electrum.network import Network
    from electrum.wallet import Abstract_Wallet


# electrum/simple_config.py: SWAPSERVER_POW_TARGET = ConfigVar(..., default=30).
# A taker who never touched the setting demands this many bits, so announcing
# with fewer makes us invisible to stock wallets even when everything else is
# correct.  Kept as a literal (rather than read from a fresh SimpleConfig) so it
# describes *other people's* default, not whatever this machine is configured to.
DEFAULT_TAKER_POW_TARGET = 30

# Upstream's client loop only looks one hour back, and independently discards
# events whose created_at is more than an hour from its own clock.
OFFER_LOOKBACK_SEC = 60 * 60

# Mirrors the ``limit`` in _get_pairs_loop's filter.
TAKER_QUERY_LIMIT = 10


class CheckStatus(Enum):
    """Why a taker would (or would not) see our offer on a given relay."""

    DISCOVERABLE = "discoverable"
    UNREACHABLE = "unreachable"
    NO_EVENT = "no_event"
    WRONG_VERSION = "wrong_version"
    WRONG_NET = "wrong_net"
    STALE = "stale"
    LOW_POW = "low_pow"
    UNPARSEABLE = "unparseable"
    CROWDED_OUT = "crowded_out"
    ERROR = "error"

    @property
    def is_ok(self) -> bool:
        return self is CheckStatus.DISCOVERABLE


@dataclass(frozen=True)
class EventView:
    """The parts of a nostr event this module reasons about.

    A plain value object rather than an ``electrum_aionostr.event.Event`` so the
    rule engine below can be exercised without a relay (or aionostr) present.
    """

    pubkey: str
    created_at: int
    tags: Sequence[Sequence[str]]
    content: str

    @classmethod
    def from_event(cls, event: Any) -> 'EventView':
        return cls(
            pubkey=str(event.pubkey),
            created_at=int(event.created_at),
            tags=[list(t) for t in (event.tags or [])],
            content=str(event.content or ""),
        )

    def tag_dict(self) -> Optional[Dict[str, str]]:
        """``{name: first_value}``, or None if upstream would fail to build it.

        Upstream does ``tags = {k: v for k, v in event.tags}`` inside a ``try``
        that skips the whole event on failure, so a *single* tag with anything
        other than exactly two elements makes the offer invisible.  We reproduce
        that rather than being more lenient: the point is to predict upstream.
        """
        result: Dict[str, str] = {}
        for tag in self.tags:
            if len(tag) != 2:
                return None
            result[str(tag[0])] = str(tag[1])
        return result


@dataclass(frozen=True)
class Verdict:
    status: CheckStatus
    detail: str
    pow_bits: Optional[int] = None

    @property
    def is_ok(self) -> bool:
        return self.status.is_ok


def d_tag_for(event_version: int) -> str:
    return f"electrum-swapserver-{event_version}"


def r_tag_for(net_name: str) -> str:
    return f"net:{net_name}"


def evaluate_offer_event(
        view: EventView,
        *,
        net_name: str,
        event_version: int,
        taker_pow_target: int,
        pow_bits_fn: PowBitsFn,
        now_ts: Optional[int] = None,
) -> Verdict:
    """Decide whether a taker's client loop would accept ``view`` as an offer.

    The checks -- and their order -- mirror
    ``NostrTransport._get_pairs_loop`` (electrum/submarine_swaps.py) so that the
    field we blame is the one that actually rejects us first upstream.

    ``pow_bits_fn(pubkey_bytes, nonce)`` is injected (rather than imported) to
    keep this module free of the sibling-import dance the zip plugin needs; it
    is always ``pow.pow_bits``.
    """
    if now_ts is None:
        now_ts = int(time.time())

    # 1. content + tags must both parse (upstream wraps them in one try block)
    tags = view.tag_dict()
    try:
        content = json.loads(view.content)
        if not isinstance(content, dict):
            raise ValueError("content is not a JSON object")
    except (ValueError, TypeError) as e:
        return Verdict(CheckStatus.UNPARSEABLE, f"announcement content is malformed: {e}")
    if tags is None:
        return Verdict(
            CheckStatus.UNPARSEABLE,
            "a tag on the announcement does not have exactly two elements, "
            "which makes takers discard the whole event")

    # 2. event version (the 'd' tag)
    want_d = d_tag_for(event_version)
    got_d = tags.get('d')
    if got_d != want_d:
        return Verdict(
            CheckStatus.WRONG_VERSION,
            f"announcement is tagged {got_d!r} but takers on this Electrum "
            f"version look for {want_d!r}")

    # 3. network (the 'r' tag)
    want_r = r_tag_for(net_name)
    got_r = tags.get('r')
    if got_r != want_r:
        return Verdict(
            CheckStatus.WRONG_NET,
            f"announcement is tagged {got_r!r} but takers on this chain look "
            f"for {want_r!r}")

    # 4. freshness (takers reject anything more than an hour off their clock)
    age = now_ts - view.created_at
    if age > OFFER_LOOKBACK_SEC:
        return Verdict(
            CheckStatus.STALE,
            f"announcement is {format_age(age)} old; takers ignore anything "
            f"older than 1 hour")
    if age < -OFFER_LOOKBACK_SEC:
        return Verdict(
            CheckStatus.STALE,
            f"announcement is timestamped {format_age(-age)} in the future; "
            f"check this machine's clock")

    # 5. proof of work
    try:
        pow_nonce = int(content.get('pow_nonce', "0"), 16)
    except (ValueError, TypeError):
        return Verdict(CheckStatus.UNPARSEABLE, "pow_nonce is not a hex string")
    try:
        pow_bits = int(pow_bits_fn(bytes.fromhex(view.pubkey), pow_nonce))
    except (ValueError, TypeError):
        return Verdict(CheckStatus.UNPARSEABLE, "pubkey is not valid hex")
    if pow_bits < taker_pow_target:
        return Verdict(
            CheckStatus.LOW_POW,
            f"announcement carries {pow_bits} bits of proof of work but takers "
            f"demand {taker_pow_target}",
            pow_bits=pow_bits)

    # 6. the payload a taker needs in order to build SwapFees
    if not isinstance(content.get('relays'), str):
        return Verdict(CheckStatus.UNPARSEABLE, "announcement has no relay list",
                       pow_bits=pow_bits)
    for required in ('percentage_fee', 'mining_fee', 'min_amount',
                     'max_forward_amount', 'max_reverse_amount'):
        if content.get(required) is None:
            return Verdict(CheckStatus.UNPARSEABLE,
                           f"announcement is missing {required!r}",
                           pow_bits=pow_bits)

    return Verdict(CheckStatus.DISCOVERABLE, "takers can see this offer",
                   pow_bits=pow_bits)


def format_age(seconds: int) -> str:
    seconds = int(seconds)
    if seconds < 0:
        return "just now"
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 90 * 60:
        return f"{seconds // 60} min"
    if seconds < 48 * 3600:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


# ------------------------------------------------------------------- queries
def author_query(pubkey_hex: str, *, kind: int, now_ts: Optional[int] = None) -> Dict[str, Any]:
    """Query A: everything *we* published recently, whatever its tags say."""
    if now_ts is None:
        now_ts = int(time.time())
    return {
        "kinds": [kind],
        "authors": [pubkey_hex],
        "since": now_ts - OFFER_LOOKBACK_SEC,
        "limit": TAKER_QUERY_LIMIT,
    }


def taker_query(*, kind: int, net_name: str, event_version: int,
                now_ts: Optional[int] = None) -> Dict[str, Any]:
    """Query B: byte-for-byte what a taker asks a relay for.

    Kept identical to the filter built in ``NostrTransport._get_pairs_loop``;
    if upstream changes it, this must follow.
    """
    if now_ts is None:
        now_ts = int(time.time())
    return {
        "kinds": [kind],
        "limit": TAKER_QUERY_LIMIT,
        "#d": [d_tag_for(event_version)],
        "#r": [r_tag_for(net_name)],
        "since": now_ts - OFFER_LOOKBACK_SEC,
    }


# ------------------------------------------------------------------- results
@dataclass(frozen=True)
class RelayResult:
    relay: str
    status: CheckStatus
    detail: str
    pow_bits: Optional[int] = None
    event_age_sec: Optional[int] = None

    @property
    def is_ok(self) -> bool:
        return self.status.is_ok


@dataclass
class DiscoveryReport:
    """What every configured relay had to say about our own announcement."""

    pubkey_hex: str
    npub: str
    net_name: str
    event_version: int
    kind: int
    taker_pow_target: int
    results: List[RelayResult] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.is_ok)

    @property
    def best_pow_bits(self) -> Optional[int]:
        seen = [r.pow_bits for r in self.results if r.pow_bits is not None]
        return max(seen) if seen else None

    def headline(self) -> str:
        total = len(self.results)
        if total == 0:
            return "No relays configured."
        if self.ok_count == 0:
            return f"Not discoverable on any of the {total} relay(s)."
        return f"Discoverable on {self.ok_count} of {total} relay(s)."

    def warnings(self) -> List[str]:
        """Problems that are real even when the headline looks healthy."""
        out: List[str] = []
        bits = self.best_pow_bits
        if bits is not None and bits < DEFAULT_TAKER_POW_TARGET:
            out.append(
                f"The announcement carries {bits} bits of proof of work. Takers "
                f"still on Electrum's default target of {DEFAULT_TAKER_POW_TARGET} "
                f"will silently ignore this server, even where the check above "
                f"says 'discoverable'. Raise the Nostr PoW target to "
                f"{DEFAULT_TAKER_POW_TARGET} and restart the server.")
        if self.taker_pow_target < DEFAULT_TAKER_POW_TARGET:
            out.append(
                f"This wallet demands only {self.taker_pow_target} bits from "
                f"other servers, so the check above is more forgiving than a "
                f"stock taker would be.")
        if any(r.status is CheckStatus.CROWDED_OUT for r in self.results):
            out.append(
                f"Some relays returned a full page of {TAKER_QUERY_LIMIT} other "
                f"offers before ours. Takers query with that limit, so a busy "
                f"relay can hide this server.")
        return out


# --------------------------------------------------------------- the network
def _throwaway_nsec() -> Optional[str]:
    """A random nsec so the check queries relays as a stranger would."""
    try:
        from electrum.lnutil import generate_random_keypair
        from electrum_aionostr.util import to_nip19
        return to_nip19('nsec', generate_random_keypair().privkey.hex())
    except Exception:
        return None


def _build_manager(relay_url: str, *, network: Optional['Network'],
                   connect_timeout: int) -> Any:
    """One aionostr Manager per relay, so results are attributable.

    ``Manager.connect`` prunes relays it could not reach from its own list, so
    a single multi-relay Manager cannot tell us *which* relay was unreachable.
    """
    import electrum_aionostr as aionostr
    import ssl
    from electrum.util import ca_path, make_aiohttp_proxy_connector

    ssl_context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH, cafile=ca_path)
    proxy = None
    if network is not None and network.proxy and network.proxy.enabled:
        proxy = make_aiohttp_proxy_connector(network.proxy, ssl_context)
    return aionostr.Manager(
        [relay_url],
        private_key=_throwaway_nsec(),
        ssl_context=ssl_context,
        proxy=proxy,
        connect_timeout=connect_timeout,
    )


async def _collect(manager: Any, query: Dict[str, Any], *, timeout: float) -> List[EventView]:
    """Drain a subscription into a list, bounded by ``timeout``.

    ``Manager.get_events`` waits for an EOSE from every relay or its own 60s
    fallback, which is far too long for a button in a GUI.
    """
    out: List[EventView] = []

    async def _run() -> None:
        async for event in manager.get_events(query, only_stored=True):
            out.append(EventView.from_event(event))

    try:
        await asyncio.wait_for(_run(), timeout=timeout)
    except asyncio.TimeoutError:
        pass  # whatever arrived before the deadline is still informative
    return out


async def check_relay(
        relay_url: str,
        *,
        pubkey_hex: str,
        net_name: str,
        event_version: int,
        kind: int,
        taker_pow_target: int,
        pow_bits_fn: PowBitsFn,
        network: Optional['Network'] = None,
        connect_timeout: int = 5,
        query_timeout: float = 10.0,
) -> RelayResult:
    """Ask one relay whether our announcement is there and would be accepted."""
    manager = None
    try:
        manager = _build_manager(relay_url, network=network, connect_timeout=connect_timeout)
        await asyncio.wait_for(manager.connect(), timeout=connect_timeout + 1)
        if not manager.relays:
            return RelayResult(relay_url, CheckStatus.UNREACHABLE,
                               "could not connect to this relay")

        now_ts = int(time.time())
        # A: is our announcement stored here at all?
        ours = await _collect(
            manager, author_query(pubkey_hex, kind=kind, now_ts=now_ts),
            timeout=query_timeout)
        ours = [e for e in ours if e.pubkey == pubkey_hex]
        if not ours:
            return RelayResult(
                relay_url, CheckStatus.NO_EVENT,
                "this relay is holding no announcement from us: either none was "
                "published, the relay rejected it, or it expired (announcements "
                "carry a 10 minute expiration tag)")
        newest = max(ours, key=lambda e: e.created_at)
        age = now_ts - newest.created_at

        verdict = evaluate_offer_event(
            newest, net_name=net_name, event_version=event_version,
            taker_pow_target=taker_pow_target, pow_bits_fn=pow_bits_fn,
            now_ts=now_ts)
        if not verdict.is_ok:
            return RelayResult(relay_url, verdict.status, verdict.detail,
                               pow_bits=verdict.pow_bits, event_age_sec=age)

        # B: our event is valid -- but does it survive the taker's own filter?
        seen = await _collect(
            manager,
            taker_query(kind=kind, net_name=net_name,
                        event_version=event_version, now_ts=now_ts),
            timeout=query_timeout)
        if not any(e.pubkey == pubkey_hex for e in seen):
            if len(seen) >= TAKER_QUERY_LIMIT:
                return RelayResult(
                    relay_url, CheckStatus.CROWDED_OUT,
                    f"our announcement is valid, but this relay returned "
                    f"{len(seen)} other offers first and takers only ask for "
                    f"{TAKER_QUERY_LIMIT}",
                    pow_bits=verdict.pow_bits, event_age_sec=age)
            return RelayResult(
                relay_url, CheckStatus.NO_EVENT,
                "our announcement is stored here but the relay did not return "
                "it for a taker's filter; it may not index tag queries",
                pow_bits=verdict.pow_bits, event_age_sec=age)

        return RelayResult(relay_url, CheckStatus.DISCOVERABLE,
                           f"announced {format_age(age)} ago",
                           pow_bits=verdict.pow_bits, event_age_sec=age)
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        return RelayResult(relay_url, CheckStatus.UNREACHABLE,
                           "timed out connecting to this relay")
    except Exception as e:
        return RelayResult(relay_url, CheckStatus.ERROR, f"{type(e).__name__}: {e}")
    finally:
        if manager is not None:
            try:
                await manager.close()
            except Exception:
                pass


async def run_discovery_check(
        *,
        relays: Sequence[str],
        pubkey_hex: str,
        npub: str,
        net_name: str,
        event_version: int,
        kind: int,
        taker_pow_target: int,
        pow_bits_fn: PowBitsFn,
        network: Optional['Network'] = None,
        connect_timeout: int = 5,
        query_timeout: float = 10.0,
) -> DiscoveryReport:
    """Check every relay concurrently and collect the verdicts."""
    report = DiscoveryReport(
        pubkey_hex=pubkey_hex, npub=npub, net_name=net_name,
        event_version=event_version, kind=kind,
        taker_pow_target=taker_pow_target)
    urls = [r.strip() for r in relays if r.strip()]
    if not urls:
        return report
    tasks = [
        check_relay(
            url, pubkey_hex=pubkey_hex, net_name=net_name,
            event_version=event_version, kind=kind,
            taker_pow_target=taker_pow_target, pow_bits_fn=pow_bits_fn,
            network=network, connect_timeout=connect_timeout,
            query_timeout=query_timeout)
        for url in urls
    ]
    gathered = await asyncio.gather(*tasks, return_exceptions=True)
    for url, outcome in zip(urls, gathered):
        if isinstance(outcome, RelayResult):
            report.results.append(outcome)
        elif isinstance(outcome, BaseException):
            report.results.append(RelayResult(
                url, CheckStatus.ERROR,
                f"{type(outcome).__name__}: {outcome}"))
    return report
