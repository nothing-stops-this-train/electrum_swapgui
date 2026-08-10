#!/usr/bin/env python
#
# swapserver_gui - a Qt GUI plugin for Electrum's submarine swap server.
# This file is released into the public domain (The Unlicense); see LICENSE.
#
# This module is GUI-agnostic: it owns the lifecycle of the submarine swap
# *server* (the HTTP endpoint and the nostr announcement loop) and exposes a
# small, thread-safe API that the Qt tab (``qt.py``) drives.
#
# Background on the design (traced from electrum/submarine_swaps.py):
#   * ``SwapManager.main_loop`` only spawns the server tasks when
#     ``is_server`` is already True at ``start_network`` time.  In the Qt GUI
#     the swap manager starts with ``is_server=False``, so the server tasks are
#     never spawned by Electrum itself.  We therefore start/stop them ourselves.
#   * The HTTP server (``HttpSwapServer.run``) sets up an aiohttp site and
#     returns; cancelling that coroutine does not stop the listening socket.
#     ``ManagedHttpSwapServer`` keeps the ``AppRunner`` so we can shut it down.
#   * Both ``run_nostr_server`` and ``SwapManager.set_nostr_proof_of_work`` route
#     into ``electrum.util.gen_nostr_ann_pow``, which *deadlocks the event loop
#     thread* if cancelled while grinding -- and cancelling is exactly what we do
#     at shutdown.  That is what made Electrum hang on exit.  We therefore
#     compute the announcement proof-of-work ourselves (``pow.py``) before
#     starting the nostr transport, so upstream always finds a good cached nonce
#     and never enters that function.  See ``pow.py`` for the full analysis.
#   * We do NOT call ``SwapManager.run_nostr_server``; :meth:`_announce_loop`
#     below replaces it.  See ``ANNOUNCE LOOP`` for why -- upstream's version can
#     stop announcing permanently and silently, which is unacceptable for a
#     server an operator believes is live.

import asyncio
import concurrent.futures
import functools
import importlib
import json
import time
from enum import Enum
from typing import TYPE_CHECKING, Optional, List, Dict, Any, Tuple

from aiohttp import web

import electrum_aionostr as aionostr

from electrum import constants
from electrum.i18n import _
from electrum.plugin import BasePlugin
from electrum.util import get_asyncio_loop
from electrum.address_synchronizer import TX_HEIGHT_UNCONFIRMED
from electrum.submarine_swaps import NostrTransport, MIN_SWAP_AMOUNT_SAT

# NB: ``from . import pow as swap_pow`` must NOT be used here.  When Electrum
# loads us as an external *zip* plugin it registers the package in sys.modules
# under 'electrum_external_plugins.swapserver_gui', but builds its spec from
# ``zipimport.zipimporter(...).find_spec('swapserver_gui')`` (electrum/plugin.py:
# maybe_load_plugin_init_method), so the package object's ``__name__`` stays the
# bare 'swapserver_gui'.  CPython's ``_handle_fromlist`` resolves a submodule
# fromlist against ``__name__``, not against the sys.modules key, so
# ``from . import pow`` tries to import 'swapserver_gui.pow' and dies with
# ModuleNotFoundError: No module named 'swapserver_gui'.
# ``__package__`` is taken from the submodule's own (correctly dotted) spec, so
# importing through it works for both the zip and the directory layout.
swap_pow = importlib.import_module('.pow', __package__)
nostr_check = importlib.import_module('.nostr_check', __package__)

# Importing the bundled swapserver plugin's server module has the useful side
# effect of registering the shared config vars (plugins.swapserver.port etc.)
# via electrum/plugins/swapserver/__init__.py.  Reusing it avoids duplicating
# the request handlers and the config-var registration.
from electrum.plugins.swapserver.server import HttpSwapServer

if TYPE_CHECKING:
    from electrum.simple_config import SimpleConfig
    from electrum.wallet import Abstract_Wallet
    from electrum.submarine_swaps import SwapManager


class ManagedHttpSwapServer(HttpSwapServer):
    """An ``HttpSwapServer`` whose aiohttp runner we retain so it can be stopped.

    The upstream ``run`` coroutine returns as soon as the site is started, which
    means the plugin cannot stop the listening socket by cancelling a task.  We
    keep references to the ``AppRunner``/``TCPSite`` and expose :meth:`stop`.
    """

    def __init__(self, config: 'SimpleConfig', wallet: 'Abstract_Wallet') -> None:
        HttpSwapServer.__init__(self, config, wallet)
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None

    async def run(self) -> None:
        # Wait for the wallet to be unlocked (mirrors upstream behaviour).
        while self.wallet.has_password() and self.wallet.get_unlocked_password() is None:
            self.logger.info("wallet is locked; waiting to start swap server HTTP endpoint")
            await asyncio.sleep(2)
        app = web.Application()
        app.add_routes([
            web.get('/getpairs', self.get_pairs),
            web.post('/createswap', self.create_swap),
            web.post('/createnormalswap', self.create_normal_swap),
            web.post('/addswapinvoice', self.add_swap_invoice),
        ])
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, host='localhost', port=self.port)
        await self.site.start()
        self.logger.info(f"swap server HTTP endpoint listening on localhost:{self.port}")

    async def stop(self) -> None:
        try:
            if self.runner is not None:
                await self.runner.cleanup()
        finally:
            self.runner = None
            self.site = None
            try:
                self.unregister_callbacks()  # from EventListener
            except Exception:
                pass


class SwapServerError(Exception):
    """Raised when the swap server cannot be started with the current settings."""


class AnnounceState(Enum):
    """Why the nostr announcement is (or is not) going out right now.

    The GUI used to say "announcing to N relay(s)" whenever the server was
    running with relays configured, which is wrong in three common situations
    that all look identical from the outside -- see :func:`announce_state`.
    """

    DISABLED = "disabled"            # no relay configured
    STOPPED = "stopped"              # server not running
    TASK_DEAD = "task_dead"          # the announce task is gone; nothing publishes
    WAITING_UNLOCK = "waiting_unlock"
    WAITING_POW = "waiting_pow"
    NO_RELAY_CONNECTED = "no_relay_connected"
    NO_LIQUIDITY = "no_liquidity"
    PUBLISH_FAILING = "publish_failing"  # connected, but no relay accepts the offer
    ANNOUNCING = "announcing"

    @property
    def is_announcing(self) -> bool:
        return self is AnnounceState.ANNOUNCING


# ---------------------------------------------------------------------------
# Telling *served* swaps apart from the operator's own swaps
#
# ``SwapManager._swaps`` (wallet.db['submarine_swaps']) is the wallet's single
# store of every submarine swap it has ever been part of, and ``SwapData``
# (electrum/submarine_swaps.py) carries no field saying which side we were on:
# the maker and the taker of the same swap write the same 13 attributes.  So
# counting all of them -- which is what upstream's ``swapserver.get_history``
# command does, and what this plugin used to copy -- reports the operator's own
# swaps as revenue served to other people.
#
# Two mechanisms, in order of authority:
#   1. the ledger below: every swap created through the server's own entry
#      points is recorded by payment hash while our server is running.  Exact,
#      but only for swaps created since this version was installed.
#   2. :func:`looks_server_side`, a field heuristic, for everything older.
# ---------------------------------------------------------------------------

#: Wallet-db key holding ``{payment_hash_hex: unix_ts}`` for swaps our server
#: served.  A plain top-level key, like ``labels``'s ``wallet_nonce`` or
#: trustedcoin's billing addresses; it survives the db's save/reload cycle and
#: is ignored by everything else.
SERVED_SWAPS_DB_KEY = 'swapserver_gui_served_swaps'


def served_swaps_ledger(wallet: 'Abstract_Wallet') -> Dict[str, int]:
    """The ``{payment_hash_hex: recorded_at}`` map, creating it if absent.

    Returns the live ``StoredDict``, so assigning into it is persisted (as an
    incremental json patch, not a rewrite of the whole map).
    """
    try:
        return wallet.db.get_dict(SERVED_SWAPS_DB_KEY)
    except Exception:
        return {}


def record_served_swap(wallet: 'Abstract_Wallet', payment_hash_hex: str) -> bool:
    """Mark ``payment_hash_hex`` as a swap our server created for a taker."""
    if not payment_hash_hex:
        return False
    ledger = served_swaps_ledger(wallet)
    if payment_hash_hex in ledger:
        return False
    ledger[payment_hash_hex] = int(time.time())
    return True


def looks_server_side(swap: Any) -> bool:
    """Best-effort role guess for a swap that predates the ledger.

    The four creation paths in electrum/submarine_swaps.py leave distinguishable
    field combinations behind, because the prepayment is asymmetric:

    ==============================================  ==========  ===========  ===============
    stored by                                       is_reverse  prepay_hash  claim_to_output
    ==============================================  ==========  ===========  ===============
    server, serving a taker's reverse swap          False       set          None
      (``create_normal_swap`` -> ``prepay=True``)
    server, serving a taker's forward swap          True        None         None
      (``create_reverse_swap``)
    us, our own forward swap                        False       None         None
      (``request_normal_swap`` -> ``prepay=False``)
    us, our own reverse swap (``reverse_swap``)     True        set*         maybe set
    ==============================================  ==========  ===========  ===============

    (*) the taker stores a prepay hash whenever the server answered with a
    ``minerFeeInvoice``, which every Electrum server does -- ``create_normal_swap``
    hardcodes ``prepay=True``.  The one swap this misfiles is our own reverse
    swap against a server that asked for no mining-fee prepayment; the non-prepay
    ``'submarine'`` request type is rejected as deprecated by Electrum servers,
    so it does not arise in practice.  Swaps created from now on are covered
    exactly by :data:`SERVED_SWAPS_DB_KEY` regardless.
    """
    if not getattr(swap, 'is_reverse', False):
        return getattr(swap, 'prepay_hash', None) is not None
    return (getattr(swap, 'prepay_hash', None) is None
            and getattr(swap, 'claim_to_output', None) is None)


def is_served_swap(swap: Any, payment_hash_hex: str, ledger: Dict[str, int]) -> bool:
    """True when ``swap`` was created for a remote taker by our swap server."""
    if payment_hash_hex in ledger:
        return True
    return looks_server_side(swap)


def has_liquidity_to_announce(
        *,
        min_amount: Optional[int],
        max_forward: Optional[int],
        max_reverse: Optional[int],
) -> bool:
    """Mirror ``NostrTransport.publish_offer``'s liquidity gate.

    Upstream (electrum/submarine_swaps.py) bails out with::

        if sm._max_forward < sm._min_amount and sm._max_reverse < sm._min_amount:
            ... "not publishing swap offer, no liquidity available" ; return

    ``_min_amount`` is ``MIN_SWAP_AMOUNT_SAT`` (20,000 sat) and the maxima come
    from ``server_update_pairs``: ``max_forward`` is capped by *both* lightning
    inbound capacity and the on-chain spendable balance, ``max_reverse`` by
    lightning outbound.  Note that ``server_update_pairs`` also rounds the
    maxima *down* to two leading digits, so 19,999 sat becomes 19,000.

    A ``None`` anywhere means ``server_update_pairs`` has not run yet; upstream
    would raise a TypeError on the comparison, which its ``@ignore_exceptions``
    decorator swallows -- i.e. no announcement either way.
    """
    if min_amount is None or max_forward is None or max_reverse is None:
        return False
    return max_forward >= min_amount or max_reverse >= min_amount


def announce_state(
        *,
        running: bool,
        nostr_enabled: bool,
        wallet_locked: bool,
        pow_grinding: bool,
        min_amount: Optional[int],
        max_forward: Optional[int],
        max_reverse: Optional[int],
        task_dead: bool = False,
        relay_connected: bool = True,
        publish_failing: bool = False,
) -> AnnounceState:
    """Classify the announcement path. Pure, so the GUI cannot drift from it.

    The order matches the order in which the real code blocks:
    :meth:`SwapServerGuiPlugin._serve_one_transport` waits for the wallet
    password before it builds a transport, ``_nostr_startup`` seeds the proof of
    work before handing over to the announce loop, the loop then needs a relay
    connection, and only then does the liquidity gate apply.

    ``task_dead``/``relay_connected``/``publish_failing`` default to the healthy
    value so callers that only know the static picture (tests, and any caller
    predating the announce loop) keep the original five-way classification.
    """
    if not nostr_enabled:
        return AnnounceState.DISABLED
    if not running:
        return AnnounceState.STOPPED
    if task_dead:
        return AnnounceState.TASK_DEAD
    if wallet_locked:
        return AnnounceState.WAITING_UNLOCK
    if pow_grinding:
        return AnnounceState.WAITING_POW
    if not relay_connected:
        return AnnounceState.NO_RELAY_CONNECTED
    if not has_liquidity_to_announce(min_amount=min_amount,
                                     max_forward=max_forward,
                                     max_reverse=max_reverse):
        return AnnounceState.NO_LIQUIDITY
    if publish_failing:
        return AnnounceState.PUBLISH_FAILING
    return AnnounceState.ANNOUNCING


# ---------------------------------------------------------------------------
# The announcement event
#
# We build and publish the offer event ourselves rather than calling
# ``NostrTransport.publish_offer``, for one reason: upstream's method is
# decorated ``@ignore_exceptions`` *and* swallows ``asyncio.TimeoutError``
# internally (electrum/submarine_swaps.py), so it returns None whether a relay
# accepted the event or nothing did.  Owning the call is the only way to know
# which happened, and "did the last announcement actually land" is precisely the
# question the Status tab has to answer.
#
# The payload and tags below must stay byte-identical to upstream's, because
# takers filter on the tags and parse the content.  ``test_diagnostics`` pins
# both against ``NostrTransport``.
# ---------------------------------------------------------------------------

def build_offer_payload(sm: 'SwapManager') -> Dict[str, Any]:
    """The announcement content, exactly as ``publish_offer`` composes it."""
    return {
        # cast to float for <= 4.7.1 backwards compatibility, as upstream does
        'percentage_fee': float(sm.percentage),
        'mining_fee': sm.mining_fee,
        'min_amount': sm._min_amount,
        'max_forward_amount': sm._max_forward,
        'max_reverse_amount': sm._max_reverse,
        'relays': sm.config.NOSTR_RELAYS,
        'pow_nonce': hex(sm.config.SWAPSERVER_ANN_POW_NONCE),
    }


def build_offer_tags(*, now_ts: int) -> List[List[str]]:
    """The announcement tags, exactly as ``publish_offer`` composes them.

    The ``expiration`` (NIP-40) window stays upstream's
    ``OFFER_UPDATE_INTERVAL_SEC + 10`` even though we republish far more often:
    the fix for the expiry gap is the shorter *cadence*
    (:attr:`SwapServerGuiPlugin.REPUBLISH_INTERVAL_SEC`), not a longer window.
    Each republish carries a fresh expiration, so the offer is now refreshed at
    roughly half its lifetime instead of right at the end of it.
    """
    return [['d', f'electrum-swapserver-{NostrTransport.NOSTR_EVENT_VERSION}'],
            ['r', 'net:' + constants.net.NET_NAME],
            ['expiration',
             str(now_ts + NostrTransport.OFFER_UPDATE_INTERVAL_SEC + 10)]]


async def publish_offer_event(transport: 'NostrTransport', sm: 'SwapManager') -> str:
    """Publish one swap offer; return the id of the event a relay acknowledged.

    Raises ``asyncio.TimeoutError`` when no relay acknowledged in time --
    ``aionostr.Manager.add_event`` resolves on the first OK and gives up after
    ``connect_timeout``.  A relay list that has been emptied (see
    :meth:`SwapServerGuiPlugin._serve_one_transport`) also lands here, because
    there is then nothing to wait for.
    """
    return await aionostr._add_event(
        transport.relay_manager,
        kind=NostrTransport.USER_STATUS_NIP38,
        tags=build_offer_tags(now_ts=int(time.time())),
        content=json.dumps(build_offer_payload(sm)),
        private_key=transport.nostr_private_key)


class SwapServerGuiPlugin(BasePlugin):
    """Owns the swap-server lifecycle. The Qt layer subclasses this."""

    #: How often the announce loop re-publishes an unchanged offer.  Upstream
    #: uses OFFER_UPDATE_INTERVAL_SEC (600s) against a NIP-40 expiration of
    #: 610s, checked on a 30s tick and re-stamped only *after* the publish
    #: returns -- so the real period is 600-630s plus latency and drifts later
    #: every cycle, leaving the offer expired on strict relays for part of each
    #: cycle.  Refreshing at half the window removes that gap entirely.
    REPUBLISH_INTERVAL_SEC = 300

    #: Tick of the announce loop: how often liquidity/mining fees are recomputed
    #: and compared, matching upstream's LIQUIDITY_UPDATE_INTERVAL_SEC.  A server
    #: that just started may have no open channels yet, so the loop simply keeps
    #: ticking until there is something to advertise.
    LIQUIDITY_POLL_SEC = 30

    #: Rebuild the transport this often even when nothing is wrong.
    #: ``aionostr.Manager.connect`` drops every relay that did not answer its one
    #: connection attempt (``self.relays = connected``) and is guarded by
    #: ``if not self.connected``, so it never runs again -- a relay that was down
    #: at start-up stays dropped for the life of the transport.  A fresh
    #: transport is the only way to get it back.
    TRANSPORT_RECYCLE_SEC = 3600

    #: Rebuild early once this many consecutive publishes go unacknowledged.
    PUBLISH_FAILURES_BEFORE_RECYCLE = 3

    #: Backoff bounds for rebuilding the transport after a failure.
    TRANSPORT_RETRY_MIN_SEC = 5
    TRANSPORT_RETRY_MAX_SEC = 300

    #: How often to re-check a password-protected wallet for being unlocked.
    WALLET_UNLOCK_POLL_SEC = 10

    #: Floor on how often :meth:`supervise` may resurrect a dead announce task.
    NOSTR_RESTART_MIN_INTERVAL_SEC = 30

    def __init__(self, parent: Any, config: 'SimpleConfig', name: str) -> None:
        BasePlugin.__init__(self, parent, config, name)
        self.wallet: Optional['Abstract_Wallet'] = None
        self._sm: Optional['SwapManager'] = None
        self._http_fut: Optional['concurrent.futures.Future'] = None
        self._nostr_fut: Optional['concurrent.futures.Future'] = None
        self._running: bool = False
        # The swap manager whose server_create_* methods we wrapped, if any.
        self._recorded_sm: Optional['SwapManager'] = None
        # Set once the announcement proof-of-work is usable (or once we know we
        # cannot compute one, e.g. no nostr keypair). Gates the nostr announce
        # path so we never publish an offer that takers would reject.
        self._pow_gate: asyncio.Event = asyncio.Event()
        self._pow_state: Optional['swap_pow.PowState'] = None
        self._pow_grinding: bool = False
        # Bookkeeping for the announce loop. Because we own the publish call
        # (see publish_offer_event) these are the real thing: _attempt_at is
        # stamped before every send, _success_at only when a relay acknowledged
        # the event. The Diagnostics check still exists to answer the one
        # question local state cannot -- whether a taker's *filter* matches.
        self._last_publish_attempt_at: Optional[float] = None
        self._last_publish_success_at: Optional[float] = None
        self._last_publish_note: Optional[str] = None
        self._consecutive_publish_failures: int = 0
        # None until the loop has tried to connect at least once.
        self._relay_connected: Optional[bool] = None
        # Set (threadsafely) to make the announce loop publish on its next tick.
        self._publish_wakeup: asyncio.Event = asyncio.Event()
        # The transport the announce loop is currently using, so stop_server can
        # tear it down even when cancellation cuts __aexit__ short.
        self._live_transport: Optional['NostrTransport'] = None
        self._last_nostr_restart_at: Optional[float] = None
        self._check_fut: Optional['concurrent.futures.Future'] = None

    # ------------------------------------------------------------------ utils
    @property
    def sm(self) -> Optional['SwapManager']:
        return self._sm

    def _loop(self) -> asyncio.AbstractEventLoop:
        return get_asyncio_loop()

    def _spawn(self, coro, label: str) -> 'concurrent.futures.Future':
        """Schedule a coroutine on the network loop WITHOUT blocking the caller.

        Must never call ``.result()`` here: this runs on the Qt GUI thread, and
        the asyncio loop may be busy (e.g. the nostr announcement proof-of-work),
        so blocking would freeze the UI and can raise TimeoutError.
        ``run_coroutine_threadsafe`` returns immediately; the returned future's
        ``.cancel()`` schedules cancellation of the underlying task on the loop.
        """
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop())

        def _log_result(f: 'concurrent.futures.Future') -> None:
            if f.cancelled():
                return
            exc = f.exception()
            if exc is not None:
                self.logger.warning(f"swap server task {label!r} ended with error: {exc!r}")
        fut.add_done_callback(_log_result)
        return fut

    @staticmethod
    def _cancel_fut(fut: Optional['concurrent.futures.Future']) -> None:
        if fut is not None:
            fut.cancel()

    # -------------------------------------------------- served-swap bookkeeping
    #: Instance attribute we hang on the SwapManager to mark it as wrapped and
    #: to remember what to put back.  Maps method name -> the instance attribute
    #: that was shadowing the class method before us (None if there was none).
    _RECORDER_MARK = '_swapserver_gui_recorder'

    #: The only two SwapManager methods that create a swap on behalf of a remote
    #: taker.  Both transports funnel into them -- the HTTP endpoint via
    #: electrum/plugins/swapserver/server.py (``create_swap``/``create_normal_swap``
    #: handlers) and nostr via ``NostrTransport._handle_requests`` -- so wrapping
    #: the pair catches every served swap without touching either transport.
    _SERVER_CREATE_METHODS = ('server_create_swap', 'server_create_normal_swap')

    def _install_served_swap_recorder(self, sm: 'SwapManager') -> None:
        """Start recording which swaps this server creates for takers.

        Wrapping the two entry points on the *instance* keeps upstream's class
        untouched, so nothing leaks into other wallets or survives
        :meth:`stop_server`.
        """
        if getattr(sm, self._RECORDER_MARK, None) is not None:
            return  # already wrapped (start_server is idempotent)
        wallet = self.wallet
        if wallet is None:
            return
        shadowed: Dict[str, Any] = {}
        for name in self._SERVER_CREATE_METHODS:
            original = getattr(sm, name, None)
            if original is None:
                self.logger.debug(f"swap manager has no {name}; not recording served swaps")
                continue
            shadowed[name] = sm.__dict__.get(name)
            setattr(sm, name, self._wrap_server_create(original, wallet))
        setattr(sm, self._RECORDER_MARK, shadowed)
        # Remember which manager we patched rather than re-deriving it at stop
        # time: ``bind_wallet`` can point ``_sm`` at a second wallet's manager
        # while this one's server is still running, and unwrapping the wrong
        # object would leave this one wrapped forever.
        self._recorded_sm = sm

    def _wrap_server_create(self, original: Any, wallet: 'Abstract_Wallet') -> Any:
        """Wrap a ``server_create_*`` method so its swap lands in the ledger.

        Both wrapped methods answer with the swap's payment hash under ``'id'``
        (electrum/submarine_swaps.py, ``server_create_swap`` /
        ``server_create_normal_swap``).  Recording must never be able to fail the
        swap itself: a swap we do not record is merely classified by
        :func:`looks_server_side` later, whereas an exception here would be
        returned to the taker as a failed request.
        """
        @functools.wraps(original)
        def wrapper(request: Dict[str, Any], *args: Any, **kwargs: Any) -> Dict[str, Any]:
            response = original(request, *args, **kwargs)
            try:
                payment_hash_hex = response['id']
                if record_served_swap(wallet, payment_hash_hex):
                    self.logger.info(f"served swap recorded: {payment_hash_hex}")
            except Exception:
                self.logger.warning(
                    "could not record a served swap; it will be classified "
                    "heuristically in the history", exc_info=True)
            return response
        return wrapper

    def _remove_served_swap_recorder(self, sm: Optional['SwapManager'] = None) -> None:
        """Undo :meth:`_install_served_swap_recorder`. Idempotent.

        Defaults to the manager that was actually wrapped, which is not always
        the one ``self._sm`` currently points at.
        """
        if sm is None:
            sm = self._recorded_sm
        if sm is None:
            return
        shadowed = getattr(sm, self._RECORDER_MARK, None)
        if shadowed is None:
            self._recorded_sm = None
            return
        for name, previous in shadowed.items():
            if previous is None:
                sm.__dict__.pop(name, None)  # expose the class method again
            else:
                setattr(sm, name, previous)
        try:
            delattr(sm, self._RECORDER_MARK)
        except AttributeError:
            pass
        if sm is self._recorded_sm:
            self._recorded_sm = None

    # ------------------------------------------------------------ proof of work
    def _nostr_pubkey(self) -> Optional[bytes]:
        """The x-only nostr pubkey the announcement PoW is bound to, if any.

        Upstream hashes ``b'electrum-' + keypair.pubkey[1:]`` (the compressed
        pubkey without its prefix byte), so we must use exactly the same bytes.
        """
        if self.wallet is None or self.wallet.lnworker is None:
            return None
        keypair = getattr(self.wallet.lnworker, "nostr_keypair", None)
        pubkey = getattr(keypair, "pubkey", None)
        if not isinstance(pubkey, (bytes, bytearray)) or len(pubkey) < 2:
            return None
        return bytes(pubkey[1:])

    # ------------------------------------------------------------- identity
    def nostr_identity(self) -> Optional[Tuple[str, str]]:
        """``(pubkey_hex, npub)`` for this wallet's nostr key, or None.

        This is the identity takers see and pin as ``SWAPSERVER_NPUB``.  It is
        derived from the wallet seed (``lnworker.nostr_keypair``), so it exists
        whether or not the server is running.

        The hex form is x-only -- ``keypair.pubkey.hex()[2:]``, dropping the
        compressed-key prefix byte -- exactly as ``NostrTransport.nostr_pubkey``
        computes it, so it matches what the taker's provider list displays.
        """
        pubkey = self._nostr_pubkey()
        if pubkey is None:
            return None
        pubkey_hex = pubkey.hex()
        try:
            from electrum_aionostr.util import to_nip19
            npub = to_nip19('npub', pubkey_hex)
        except Exception:
            self.logger.debug("could not encode npub", exc_info=True)
            return None
        return pubkey_hex, npub

    @staticmethod
    def nostr_match_fields() -> Dict[str, Any]:
        """The values a taker's filter must agree with, byte for byte.

        A mismatch in any of these makes the server invisible with no error on
        either side, so they are worth showing verbatim: the network name
        distinguishes 'signet' from 'mutinynet', and the event version differs
        between Electrum releases.
        """
        net_name = constants.net.NET_NAME
        version = NostrTransport.NOSTR_EVENT_VERSION
        return {
            "net_name": net_name,
            "event_version": version,
            "kind": NostrTransport.USER_STATUS_NIP38,
            "d_tag": nostr_check.d_tag_for(version),
            "r_tag": nostr_check.r_tag_for(net_name),
        }

    def _save_pow_state(self, state: 'swap_pow.PowState') -> None:
        self._pow_state = state
        try:
            self.config.SWAPSERVER_GUI_POW_STATE = state.dumps()
        except Exception:
            self.logger.debug("could not persist proof-of-work state", exc_info=True)

    async def ensure_pow_nonce(self) -> bool:
        """Make sure ``SWAPSERVER_ANN_POW_NONCE`` satisfies ``SWAPSERVER_POW_TARGET``.

        Returns True when a usable nonce is in place.  Always opens
        :attr:`_pow_gate` before returning so the announce path is never
        blocked forever, even when we cannot compute a proof of work.

        This is cancellable and, unlike upstream's ``gen_nostr_ann_pow``,
        cancelling it neither blocks nor wedges the event loop thread.
        """
        pubkey = self._nostr_pubkey()
        if pubkey is None:
            # No nostr keypair (or a stand-in without one): nothing to grind
            # against. Let the caller proceed; upstream will report the problem.
            self.logger.info("no nostr keypair available; skipping announcement proof-of-work")
            self._pow_gate.set()
            return False

        target = int(self.config.SWAPSERVER_POW_TARGET or 0)
        if swap_pow.pow_bits(pubkey, self.config.SWAPSERVER_ANN_POW_NONCE) >= target:
            self.logger.debug("reusing cached nostr announcement proof-of-work")
            self._pow_gate.set()
            return True

        state = swap_pow.PowState.load(
            self.config.SWAPSERVER_GUI_POW_STATE, pubkey=pubkey, target=target)
        self._pow_state = state
        # A previously-found "best" nonce may already satisfy a lowered target.
        if state.best_nonce is not None and state.best_bits >= target:
            self.logger.info(f"reusing best-known nonce ({state.best_bits} bits) for target {target}")
            self.config.SWAPSERVER_ANN_POW_NONCE = state.best_nonce
            self._pow_gate.set()
            return True

        est = swap_pow.format_duration(swap_pow.estimate_seconds(target))
        self.logger.info(
            f"generating nostr announcement proof-of-work: target={target} bits, "
            f"resuming from {state.hashes_done()} hashes already scanned, "
            f"estimated {est}")
        self._pow_grinding = True
        try:
            result = await swap_pow.grind(
                pubkey=pubkey,
                target_bits=target,
                state=state,
                on_progress=self._save_pow_state,
            )
        except asyncio.CancelledError:
            # Search cursors were already persisted by pow.grind's cancel path,
            # so the next attempt resumes instead of re-scanning.
            self.logger.info("announcement proof-of-work cancelled; progress saved")
            raise
        finally:
            self._pow_grinding = False
            self._pow_gate.set()

        if not result.found or result.nonce is None:
            self.logger.warning("announcement proof-of-work ended without a solution")
            return False
        self.config.SWAPSERVER_ANN_POW_NONCE = result.nonce
        self.logger.info(f"found nostr announcement proof-of-work: {result.bits} bits")
        return True

    # -------------------------------------------------------- ANNOUNCE LOOP
    #
    # This replaces ``SwapManager.run_nostr_server``.  That coroutine has four
    # failure modes that all end in "the server is silently invisible", which is
    # the worst outcome an operator can have, and none of them are observable or
    # recoverable from outside it (electrum/submarine_swaps.py):
    #
    #   1. it blocks on ``await transport.is_connected.wait()`` with no timeout.
    #      ``is_connected`` is set exactly once, in ``NostrTransport.main_loop``,
    #      and only if ``relay_manager.connect()`` returned at least one live
    #      relay.  If every relay happens to be unreachable at that moment --
    #      Electrum starting before the network is up, Tor not ready yet, a
    #      resume from suspend -- the wait never completes.  ``run_nostr_server``
    #      then hangs for the whole process lifetime, and so does
    #      ``check_direct_messages``, so the server also stops answering swap
    #      requests over nostr.  Nothing retries the connection.
    #   2. relays that failed that single attempt are removed from the manager
    #      permanently (``Manager.connect``: ``self.relays = connected``), so a
    #      momentary blip silently degrades a five-relay server to one relay.
    #   3. it republishes right at the NIP-40 expiry instead of inside it; see
    #      :attr:`REPUBLISH_INTERVAL_SEC`.
    #   4. neither the attempt nor its outcome is reportable, because
    #      ``publish_offer`` is ``@ignore_exceptions``.
    #
    # Owning the loop makes all four fixable, and is the same trade this plugin
    # already made for the proof of work.  The transport itself is still
    # upstream's, so ``check_direct_messages`` and ``_handle_requests`` -- the
    # nostr side of actually serving swaps -- are unchanged: they are spawned by
    # ``NostrTransport.main_loop`` because ``sm.is_server`` is True.

    async def _nostr_startup(self) -> None:
        """Seed the proof of work, then run the announce loop.

        Ordering matters: anything that reaches upstream's
        ``set_nostr_proof_of_work`` grinds inside the un-cancellable helper in
        ``electrum.util``.  Seeding a good nonce first guarantees nothing does,
        so cancelling this task at shutdown is always safe.
        """
        await self.ensure_pow_nonce()
        await self._announce_loop()

    async def _announce_loop(self) -> None:
        """Keep a connected nostr transport alive and the offer fresh, forever.

        Never returns except by cancellation: every error path rebuilds the
        transport after a backoff, because a fresh transport is the only way to
        recover a relay list that ``Manager.connect`` has pruned.
        """
        delay = float(self.TRANSPORT_RETRY_MIN_SEC)
        while True:
            try:
                made_progress = await self._serve_one_transport()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Nothing may end this loop except cancellation: a server whose
                # announce task has silently exited is exactly the state this
                # code exists to prevent. supervise() is only the backstop.
                self.logger.warning("nostr announce: unexpected error; retrying",
                                    exc_info=True)
                made_progress = False
            if made_progress:
                delay = float(self.TRANSPORT_RETRY_MIN_SEC)
            else:
                self.logger.info(
                    f"nostr announce: retrying with a new transport in {delay:.0f}s")
            await asyncio.sleep(delay)
            if not made_progress:
                delay = min(delay * 2, float(self.TRANSPORT_RETRY_MAX_SEC))

    async def _serve_one_transport(self) -> bool:
        """Build a transport, announce through it until it should be recycled.

        Returns True when the transport was useful (it connected and published
        at least once), which is what resets the retry backoff.
        """
        sm = self._sm
        if sm is None or self.wallet is None or self.wallet.lnworker is None:
            return False
        keypair = getattr(self.wallet.lnworker, "nostr_keypair", None)
        if keypair is None:
            # Upstream would fail the same way; say so instead of spinning.
            self.logger.warning("no nostr keypair; cannot announce")
            self._last_publish_note = "this wallet has no nostr key"
            return False
        # Mirrors run_nostr_server: no transport at all until the wallet is
        # unlocked, because the offer is signed with a key derived from the seed.
        while self.wallet_is_locked():
            self.logger.info("wallet is locked; waiting to announce over nostr")
            await asyncio.sleep(self.WALLET_UNLOCK_POLL_SEC)

        useful = False
        try:
            # Inside the try: building the transport reads sm.network and the
            # config, either of which can be missing or half-initialised while
            # a wallet is loading.
            transport = NostrTransport(self.config, sm, keypair)
            self._live_transport = transport
            async with transport:
                try:
                    await asyncio.wait_for(transport.is_connected.wait(),
                                           timeout=transport.connect_timeout + 1)
                except asyncio.TimeoutError:
                    self._relay_connected = False
                    self._last_publish_note = "no relay connected"
                    self.logger.warning(
                        "nostr announce: no relay connected; discarding this "
                        "transport so the relay list is rebuilt")
                    return False
                self._relay_connected = True
                self.logger.info("nostr announce: transport connected")
                useful = await self._announce_until_recycle(transport)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._last_publish_note = f"transport error: {type(e).__name__}: {e}"
            self.logger.warning("nostr announce: transport failed", exc_info=True)
        finally:
            self._relay_connected = False
            self._live_transport = None
        return useful

    async def _announce_until_recycle(self, transport: 'NostrTransport') -> bool:
        """Publish on this transport until it is time to build a new one.

        Publishes when the offer would otherwise go stale, when the advertised
        numbers change (upstream's rule), or when :meth:`publish_now` asks.
        """
        sm = self._sm
        assert sm is not None
        loop = asyncio.get_running_loop()
        built_at = loop.time()
        last_publish_at: Optional[float] = None
        published_at_least_once = False
        previous = self._advertised_values(sm)
        forced = True  # announce as soon as we are connected, not one tick later
        while True:
            try:
                sm.server_update_pairs()
            except Exception:
                self.logger.debug("server_update_pairs failed in the announce loop",
                                  exc_info=True)
            current = self._advertised_values(sm)
            changed = current != previous
            previous = current
            now_mono = loop.time()
            due = (forced or last_publish_at is None or changed
                   or now_mono - last_publish_at >= self.REPUBLISH_INTERVAL_SEC)
            if due and has_liquidity_to_announce(min_amount=sm._min_amount,
                                                 max_forward=sm._max_forward,
                                                 max_reverse=sm._max_reverse):
                last_publish_at = now_mono
                if await self._publish_once(transport):
                    published_at_least_once = True
                if self._consecutive_publish_failures >= self.PUBLISH_FAILURES_BEFORE_RECYCLE:
                    self.logger.warning(
                        f"nostr announce: {self._consecutive_publish_failures} "
                        f"publishes in a row went unacknowledged; rebuilding the "
                        f"transport")
                    return published_at_least_once
            if loop.time() - built_at >= self.TRANSPORT_RECYCLE_SEC:
                self.logger.debug("nostr announce: recycling the transport on schedule")
                return published_at_least_once
            forced = await self._wait_for_tick(self.LIQUIDITY_POLL_SEC)

    @staticmethod
    def _advertised_values(sm: 'SwapManager') -> Tuple[Any, Any, Any]:
        """The parts of the offer whose change forces an immediate re-announce.

        Same triple upstream compares (``liquidity_changed``/
        ``mining_fees_changed``); an offer that no longer matches reality is
        worse than a slightly late one.
        """
        return (sm._max_forward, sm._max_reverse, sm.mining_fee)

    async def _wait_for_tick(self, seconds: float) -> bool:
        """Sleep for ``seconds``, or wake early if :meth:`publish_now` fired.

        Returns True when it was woken early, i.e. an announcement was requested.
        """
        try:
            await asyncio.wait_for(self._publish_wakeup.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return False
        self._publish_wakeup.clear()
        return True

    async def _publish_once(self, transport: 'NostrTransport') -> bool:
        """Send one offer; record whether a relay acknowledged it."""
        sm = self._sm
        assert sm is not None
        self._last_publish_attempt_at = time.time()
        try:
            event_id = await publish_offer_event(transport, sm)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._consecutive_publish_failures += 1
            self._last_publish_note = f"failed: {type(e).__name__}: {e}"
            self.logger.warning(
                f"nostr announce: publish failed "
                f"({self._consecutive_publish_failures} in a row): {e!r}")
            return False
        self._consecutive_publish_failures = 0
        self._last_publish_success_at = time.time()
        self._last_publish_note = "accepted by a relay"
        self.logger.info(f"nostr announce: published offer {event_id}")
        return True

    # -------------------------------------------------------------- lifecycle
    def bind_wallet(self, wallet: 'Abstract_Wallet') -> None:
        """Associate this plugin instance with a wallet's swap manager."""
        self.wallet = wallet
        self._sm = wallet.lnworker.swap_manager if wallet.lnworker else None

    def can_run(self) -> Optional[str]:
        """Return None if the server can run, otherwise a human-readable reason."""
        if self.wallet is None or self._sm is None:
            return "no lightning-enabled wallet is loaded"
        port = self.config.SWAPSERVER_PORT
        relays = (self.config.NOSTR_RELAYS or "").strip()
        if not port and not relays:
            return "configure an HTTP port and/or at least one nostr relay first"
        return None

    def is_running(self) -> bool:
        return self._running

    def start_server(self) -> None:
        """Start the configured server transports. Idempotent."""
        if self._running:
            return
        reason = self.can_run()
        if reason is not None:
            raise SwapServerError(reason)
        assert self._sm is not None and self.wallet is not None
        sm = self._sm
        sm.is_server = True
        # Before any transport can accept a request: from here on, every swap
        # created for a taker is recorded, so the history can tell them apart
        # from the operator's own swaps without guessing.
        self._install_served_swap_recorder(sm)

        port = self.config.SWAPSERVER_PORT
        relays = (self.config.NOSTR_RELAYS or "").strip()
        self._pow_gate = asyncio.Event()  # fresh gate for this run
        # Nothing from a previous run may be reported as if it belonged to this
        # one; a stale "last announcement" is exactly the bug this all started as.
        self._last_publish_attempt_at = None
        self._last_publish_success_at = None
        self._last_publish_note = None
        self._consecutive_publish_failures = 0
        self._relay_connected = None
        self._publish_wakeup = asyncio.Event()
        self._last_nostr_restart_at = None

        if port:
            server = ManagedHttpSwapServer(self.config, self.wallet)
            sm.http_server = server
            self._http_fut = self._spawn(server.run(), "http")
        if relays:
            # _nostr_startup seeds the proof-of-work, then runs our own announce
            # loop in place of SwapManager.run_nostr_server; see ANNOUNCE LOOP.
            # It publishes as soon as the transport connects, so there is no
            # separate "announce on startup" step to schedule here.
            self._nostr_fut = self._spawn(self._nostr_startup(), "nostr")

        self._running = True
        self.logger.info(f"swap server started (http_port={port or None}, "
                          f"nostr_relays={len(relays.split(',')) if relays else 0})")

    def publish_now(self) -> None:
        """Ask the announce loop to publish on its next tick (non-blocking).

        Safe to call from the Qt GUI thread. This no longer spins up a transport
        of its own: doing that while the long-lived one was up meant two
        ``NostrTransport`` instances subscribed to the same pubkey's DMs, both
        running ``_handle_requests``, with the short-lived one able to pick up a
        taker's request and then be torn down mid-flight.
        """
        if not self._running:
            return
        if not (self.config.NOSTR_RELAYS or "").strip():
            return
        if self._sm is None or self.wallet is None or self.wallet.lnworker is None:
            return
        if getattr(self.wallet.lnworker, "nostr_keypair", None) is None:
            return
        self._loop().call_soon_threadsafe(self._publish_wakeup.set)

    # ------------------------------------------------------------ supervision
    def nostr_task_failed(self) -> bool:
        """True when the announce task ended by itself while the server runs.

        :meth:`_announce_loop` only ends by cancellation, so this means it hit
        something the loop could not absorb (or ``ensure_pow_nonce`` raised).
        Either way nothing is being announced and the GUI must not claim
        otherwise.
        """
        fut = self._nostr_fut
        return bool(self._running and fut is not None
                    and fut.done() and not fut.cancelled())

    def supervise(self) -> None:
        """Restart the announce task if it died. Cheap; call it periodically.

        The loop absorbs its own errors, so this is a backstop rather than the
        primary recovery path -- but "the plugin says Announcing while nothing
        announces" must not be reachable, and a dead task is the one way to get
        there that the loop itself cannot fix.

        Throttled, because the caller is a GUI timer: a failure that reproduces
        on every start (``ensure_pow_nonce`` raising, say) would otherwise be
        retried several times a minute forever.
        """
        if not self.nostr_task_failed():
            return
        now_ts = time.time()
        if (self._last_nostr_restart_at is not None
                and now_ts - self._last_nostr_restart_at < self.NOSTR_RESTART_MIN_INTERVAL_SEC):
            return
        self._last_nostr_restart_at = now_ts
        self.logger.warning("nostr announce task had died; restarting it")
        self._nostr_fut = self._spawn(self._nostr_startup(), "nostr")

    def stop_server(self) -> None:
        """Stop all server transports. Idempotent."""
        if not self._running:
            return
        sm = self._sm
        # Stop the HTTP listener (needs an explicit aiohttp runner cleanup).
        # Schedule it on the loop fire-and-forget; do NOT block the GUI thread.
        if sm is not None and isinstance(getattr(sm, 'http_server', None), ManagedHttpSwapServer):
            http_server = sm.http_server
            # Unregister the EventListener synchronously: the cleanup coroutine
            # below can be cancelled by the loop shutting down before it runs,
            # and a stale registration would keep a stopped wallet's callbacks
            # alive in the global registry.
            try:
                http_server.unregister_callbacks()
            except Exception:
                self.logger.debug("unregister_callbacks failed", exc_info=True)
            self._spawn(http_server.stop(), "http-stop")
            sm.http_server = None
        self._cancel_fut(self._http_fut)
        self._cancel_fut(self._nostr_fut)
        self._http_fut = None
        self._nostr_fut = None
        # Cancelling the announce task cuts NostrTransport.__aexit__ short (it
        # awaits transport.stop(), which cannot complete once the task is being
        # cancelled), so the relay websockets would leak until the process
        # exits. Tear the transport down separately, fire-and-forget.
        transport = self._live_transport
        self._live_transport = None
        if transport is not None:
            self._spawn(self._shutdown_transport(transport), "nostr-stop")
        if sm is not None:
            sm.is_server = False
        self._remove_served_swap_recorder()
        self._running = False
        self._pow_grinding = False
        self._relay_connected = None
        self.logger.info("swap server stopped")

    async def _shutdown_transport(self, transport: 'NostrTransport') -> None:
        """Close a nostr transport, tolerating a partly-built or already-closed one.

        ``NostrTransport.stop`` is ``@log_exceptions`` (it re-raises) and touches
        ``relay_manager`` unconditionally, which is None if ``main_loop`` never
        got that far. Running it twice is also possible, when ``__aexit__`` did
        manage to complete before we got here.
        """
        try:
            await transport.stop()
        except Exception:
            self.logger.debug("nostr transport shutdown failed", exc_info=True)

    # ------------------------------------------------------------ diagnostics
    def wallet_is_locked(self) -> bool:
        """True while the announce loop is waiting for the wallet password.

        :meth:`_serve_one_transport` loops on exactly this condition before it
        builds a transport, as upstream's ``run_nostr_server`` does, because the
        offer is signed with a key derived from the seed: a password-protected
        wallet that was never unlocked announces nothing at all.
        """
        wallet = self.wallet
        if wallet is None:
            return False
        try:
            return bool(wallet.has_password()) and wallet.get_unlocked_password() is None
        except Exception:
            return False

    def relay_list(self) -> List[str]:
        return [r.strip() for r in (self.config.NOSTR_RELAYS or "").split(",") if r.strip()]

    def check_discoverability(self) -> 'concurrent.futures.Future':
        """Ask every configured relay whether a taker could see this server.

        Non-blocking: returns a future resolving to a
        ``nostr_check.DiscoveryReport``.  Safe to call from the Qt GUI thread.
        Raises :class:`SwapServerError` when there is nothing to check.
        """
        identity = self.nostr_identity()
        if identity is None:
            raise SwapServerError("this wallet has no nostr key")
        relays = self.relay_list()
        if not relays:
            raise SwapServerError("no nostr relay is configured")
        pubkey_hex, npub = identity
        fields = self.nostr_match_fields()
        network = self._sm.network if self._sm is not None else None
        coro = nostr_check.run_discovery_check(
            relays=relays,
            pubkey_hex=pubkey_hex,
            npub=npub,
            net_name=fields["net_name"],
            event_version=fields["event_version"],
            kind=fields["kind"],
            taker_pow_target=int(self.config.SWAPSERVER_POW_TARGET or 0),
            pow_bits_fn=swap_pow.pow_bits,
            network=network,
        )
        self._check_fut = self._spawn(coro, "discovery-check")
        return self._check_fut

    def request_pairs_update(self) -> None:
        """Ask the swap manager to recompute the advertised pairs (non-blocking)."""
        sm = self._sm
        if sm is None or not self._running:
            return
        def _update() -> None:
            try:
                sm.server_update_pairs()
            except Exception:
                self.logger.debug("server_update_pairs failed", exc_info=True)
        self._loop().call_soon_threadsafe(_update)

    # ------------------------------------------------------------------ views
    def status(self) -> Dict[str, Any]:
        """A snapshot of server state for the UI (safe to read from GUI thread)."""
        sm = self._sm
        port = self.config.SWAPSERVER_PORT
        relays = [r for r in (self.config.NOSTR_RELAYS or "").split(",") if r.strip()]
        data: Dict[str, Any] = {
            "running": self._running,
            "http_enabled": bool(port),
            "http_port": port,
            "http_listening": bool(
                self._running and isinstance(getattr(sm, 'http_server', None), ManagedHttpSwapServer)
                and sm.http_server.site is not None
            ) if sm is not None else False,
            "nostr_enabled": bool(relays),
            "nostr_relay_count": len(relays),
            "pow_target": int(self.config.SWAPSERVER_POW_TARGET or 0),
            "pow_ready": self._pow_gate.is_set() and not self._pow_grinding,
            "pow_grinding": self._pow_grinding,
            "pow_best_bits": self._pow_state.best_bits if self._pow_state else 0,
            "pow_hashes_done": self._pow_state.hashes_done() if self._pow_state else 0,
            "percentage": None,
            "min_amount": None,
            "max_forward": None,
            "max_reverse": None,
            "mining_fee": None,
        }
        if sm is not None:
            data["percentage"] = float(sm.percentage) if sm.percentage is not None else None
            data["min_amount"] = sm._min_amount
            data["max_forward"] = sm._max_forward
            data["max_reverse"] = sm._max_reverse
            data["mining_fee"] = sm.mining_fee

        # ---- nostr identity and announcement diagnostics ------------------
        identity = self.nostr_identity()
        data["nostr_pubkey"] = identity[0] if identity else None
        data["nostr_npub"] = identity[1] if identity else None
        data.update(self.nostr_match_fields())
        data["wallet_locked"] = self.wallet_is_locked()
        data["nostr_task_dead"] = self.nostr_task_failed()
        data["relay_connected"] = self._relay_connected
        data["consecutive_publish_failures"] = self._consecutive_publish_failures
        data["announce_state"] = announce_state(
            running=self._running,
            nostr_enabled=bool(relays),
            wallet_locked=data["wallet_locked"],
            pow_grinding=self._pow_grinding,
            min_amount=data["min_amount"],
            max_forward=data["max_forward"],
            max_reverse=data["max_reverse"],
            task_dead=data["nostr_task_dead"],
            # None means "the loop has not tried yet", which is a start-up
            # moment rather than a fault: do not flag it as disconnected.
            relay_connected=self._relay_connected is not False,
            publish_failing=self._consecutive_publish_failures > 0,
        )
        # What the announcement would actually carry, as opposed to the target.
        pubkey = self._nostr_pubkey()
        data["pow_bits_achieved"] = (
            swap_pow.pow_bits(pubkey, self.config.SWAPSERVER_ANN_POW_NONCE)
            if pubkey is not None else None)
        data["pow_default_taker_target"] = nostr_check.DEFAULT_TAKER_POW_TARGET
        data["min_swap_amount"] = MIN_SWAP_AMOUNT_SAT
        data["last_publish_attempt_at"] = self._last_publish_attempt_at
        data["last_publish_success_at"] = self._last_publish_success_at
        data["last_publish_note"] = self._last_publish_note
        data["republish_interval_sec"] = self.REPUBLISH_INTERVAL_SEC
        return data

    def announcement_reason(self, st: Optional[Dict[str, Any]] = None) -> str:
        """One human sentence explaining :attr:`AnnounceState` for the GUI."""
        if st is None:
            st = self.status()
        state = st["announce_state"]
        if state is AnnounceState.DISABLED:
            return "No nostr relay is configured, so nothing is announced."
        if state is AnnounceState.STOPPED:
            return "The swap server is stopped."
        if state is AnnounceState.TASK_DEAD:
            return ("The announcement task stopped unexpectedly, so nothing is "
                    "being published. It will be restarted automatically; see "
                    "the log for what happened.")
        if state is AnnounceState.NO_RELAY_CONNECTED:
            return ("Not announcing: no nostr relay is reachable. A new "
                    "connection is retried with a growing backoff, which also "
                    "restores relays that were unreachable at start-up.")
        if state is AnnounceState.PUBLISH_FAILING:
            return ("Not announcing: the offer is being sent but no relay is "
                    "acknowledging it. After a few failures the connection is "
                    "rebuilt from scratch.")
        if state is AnnounceState.WAITING_UNLOCK:
            return ("Waiting for the wallet password. Electrum does not start "
                    "the nostr announcement until the wallet is unlocked.")
        if state is AnnounceState.WAITING_POW:
            return ("Computing the announcement proof of work. Nothing is "
                    "published until it completes.")
        if state is AnnounceState.NO_LIQUIDITY:
            min_amount = st.get("min_amount") or st["min_swap_amount"]
            return (f"Not announcing: no liquidity to advertise. A swap offer is "
                    f"only published when max forward or max reverse reaches "
                    f"{min_amount:,} sat. Max forward is capped by lightning "
                    f"inbound capacity AND the on-chain balance; max reverse by "
                    f"lightning outbound capacity.")
        return (f"Announcing, and re-announcing every "
                f"{st.get('republish_interval_sec', self.REPUBLISH_INTERVAL_SEC) // 60} "
                f"minutes. Use the discoverability check below to confirm the "
                f"relays are actually serving the offer to takers.")

    def cancel_discovery_check(self) -> None:
        self._cancel_fut(self._check_fut)
        self._check_fut = None


def save_settings(
        config: 'SimpleConfig',
        *,
        port: int,
        fee_millionths: int,
        pow_target: int,
        relays: str,
) -> None:
    """Persist the swap-server settings edited in the GUI.

    Lives here rather than in ``qt.py`` so it can be tested against a real
    ``SimpleConfig``: PyQt6 is not installed in CI, so anything importable only
    through ``qt.py`` is effectively untestable.

    ``port`` MUST be an ``int``; ``0`` means "HTTP endpoint disabled".  Passing
    ``None`` for a disabled port -- the obvious way to express it, since
    ``SWAPSERVER_PORT`` defaults to ``None`` -- crashes Electrum:

        AttributeError: 'NoneType' object has no attribute 'pop'

    ``SWAPSERVER_PORT`` is registered by Electrum's bundled swapserver plugin as
    the *dotted* key ``plugins.swapserver.port``
    (electrum/plugins/swapserver/__init__.py).  Assigning ``None`` to a ConfigVar
    routes into ``SimpleConfig._set_key_in_user_config``'s key-*deletion* branch,
    whose recursion walks the dotted path without checking that each intermediate
    dict exists (electrum/simple_config.py, ``delete_key``)::

        prefix, suffix = key.split('.', 1)
        d2 = d.get(prefix)          # None when 'plugins.swapserver' was never written
        empty = delete_key(d2, suffix)

    This plugin only ever writes ``plugins.swapserver_gui.*``, so unless the user
    has separately enabled and configured the *bundled* swapserver plugin, the
    ``plugins.swapserver`` sub-dict does not exist and the delete always raises.

    Storing ``0`` takes the ordinary write branch and is behaviourally identical:
    every reader of the key tests it for truthiness rather than comparing it to
    ``None`` -- ``electrum/submarine_swaps.py`` (``if self.config.SWAPSERVER_PORT:``),
    ``electrum/plugins/swapserver/server.py``, and this plugin's own ``can_run``,
    ``start_server`` and ``status``.

    This is an upstream Electrum bug, but the fix has to live here: users run
    released AppImages we cannot patch.
    """
    assert isinstance(port, int), f"port must be an int, got {port!r}"
    config.SWAPSERVER_PORT = port
    config.SWAPSERVER_FEE_MILLIONTHS = int(fee_millionths)
    config.SWAPSERVER_POW_TARGET = int(pow_target)
    config.NOSTR_RELAYS = relays


def get_swap_history(wallet: 'Abstract_Wallet') -> List[Dict[str, Any]]:
    """Confirmed swaps this node *served to other wallets*.

    Structured like the bundled swapserver plugin's ``get_history`` command, but
    with two differences that matter to an operator reading the Status tab:

    * swaps the operator's own wallet initiated as a customer are left out.
      Upstream counts them (it walks all of ``sm._swaps``), which inflates both
      the swap count and the net return with activity nobody was served.  See
      :func:`is_served_swap` for how the two are told apart.
    * a batched transaction that settles a served swap *and* one of our own is
      still reported -- dropping it would hide real revenue -- but is marked
      ``is_mixed``, because its ``return_sat`` covers both.  ``wallet``'s history
      aggregates value per group, so there is no per-swap split to report
      instead.  This happens because ``_claim_swap`` puts the claim inputs of
      every ``is_reverse`` swap into one ``'swaps'`` tx batch regardless of which
      side we were on, and both roles then share the resulting ``spending_txid``.

    Each entry additionally carries ``num_served_swaps``/``num_own_swaps``.
    """
    if not wallet.lnworker or not wallet.lnworker.swap_manager:
        return []
    sm = wallet.lnworker.swap_manager
    ledger = served_swaps_ledger(wallet)
    # group id -> [swaps we served, swaps we initiated ourselves]
    groups: Dict[str, List[int]] = {}
    for payment_hash_hex, swap in list(sm._swaps.items()):
        group_id = swap.spending_txid if swap.is_reverse else swap.funding_txid
        if group_id is None:
            continue
        if swap.spending_txid is None \
                or wallet.adb.get_tx_height(swap.spending_txid).height() <= TX_HEIGHT_UNCONFIRMED:
            # only final swaps, so the history is stable and excludes pending ones
            continue
        counts = groups.setdefault(group_id, [0, 0])
        counts[0 if is_served_swap(swap, payment_hash_hex, ledger) else 1] += 1

    result: List[Dict[str, Any]] = []
    full_history = wallet.get_full_history()
    for group_id, (num_served, num_own) in groups.items():
        if not num_served:
            continue  # entirely the operator's own swaps
        item = full_history.get('group:' + group_id)
        if not item:
            continue
        result.append({
            'label': item['label'],
            'return_sat': int(item['value'].value),
            'date': item['date'].strftime("%Y-%m-%d"),
            'timestamp': item['timestamp'],
            'num_served_swaps': num_served,
            'num_own_swaps': num_own,
            'is_mixed': num_own > 0,
        })
    return sorted(result, key=lambda x: x['timestamp'])


#: Appended to a history row whose transaction was batched with a swap of the
#: operator's own, so its value is not purely server revenue.
MIXED_ROW_MARKER = "⚠ batched"


def format_summary_line(
        *,
        num_swaps: int,
        net_return: str,
        swaps_per_day: float,
        num_mixed: int,
) -> str:
    """The one-line summary above the history table.

    Pure, and here rather than in ``qt.py``, so it is testable without PyQt6
    (same reasoning as :func:`save_settings`).  ``net_return`` arrives
    pre-formatted because only the GUI knows the user's unit settings.
    """
    text = _("Swaps served: {num} · net return: {ret} · {rate}/day").format(
        num=num_swaps, ret=net_return, rate=swaps_per_day)
    if num_mixed:
        text += " · " + _("{n} batched with own swaps").format(n=num_mixed)
    return text


def format_mixed_note(*, num_served: int, num_own: int) -> str:
    """Why a flagged row's return value is not purely server revenue."""
    return _(
        "This transaction was batched: it settles {served} swap(s) served by this "
        "server together with {own} swap(s) this wallet initiated itself.\n"
        "The return shown covers all of them, so it is not purely server revenue."
    ).format(served=num_served, own=num_own)


def get_swap_summary(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate stats for a list produced by :func:`get_swap_history`.

    ``num_mixed`` counts entries whose value is not purely server revenue
    because the transaction was batched with one of the operator's own swaps.
    """
    num_mixed = sum(1 for s in history if s.get('is_mixed'))
    if not history:
        return {'num_swaps': 0, 'overall_return_sat': 0, 'swaps_per_day': 0.0,
                'num_mixed': 0}
    profit_loss_sum = sum(s['return_sat'] for s in history)
    first_swap = min(s['timestamp'] for s in history)
    last_swap = max(s['timestamp'] for s in history)
    days = (last_swap - first_swap) // 86400
    swaps_per_day = (len(history) / days) if days > 0 else 0.0
    return {
        'num_swaps': len(history),
        'overall_return_sat': profit_loss_sum,
        'swaps_per_day': round(swaps_per_day, 2),
        'num_mixed': num_mixed,
    }
