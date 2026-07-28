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
#   * The nostr server (``SwapManager.run_nostr_server``) is a long-running
#     coroutine that cleans up when cancelled, so for it we just cancel the task.
#   * Both ``run_nostr_server`` and ``SwapManager.set_nostr_proof_of_work`` route
#     into ``electrum.util.gen_nostr_ann_pow``, which *deadlocks the event loop
#     thread* if cancelled while grinding -- and cancelling is exactly what we do
#     at shutdown.  That is what made Electrum hang on exit.  We therefore
#     compute the announcement proof-of-work ourselves (``pow.py``) before
#     starting the nostr transport, so upstream always finds a good cached nonce
#     and never enters that function.  See ``pow.py`` for the full analysis.

import asyncio
import concurrent.futures
import importlib
import time
from enum import Enum
from typing import TYPE_CHECKING, Optional, List, Dict, Any, Tuple

from aiohttp import web

from electrum import constants
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
    WAITING_UNLOCK = "waiting_unlock"
    WAITING_POW = "waiting_pow"
    NO_LIQUIDITY = "no_liquidity"
    ANNOUNCING = "announcing"

    @property
    def is_announcing(self) -> bool:
        return self is AnnounceState.ANNOUNCING


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
) -> AnnounceState:
    """Classify the announcement path. Pure, so the GUI cannot drift from it.

    The order matches the order in which the real code blocks:
    ``run_nostr_server`` waits for the wallet password *before* it builds a
    transport, our ``_nostr_startup`` seeds the proof of work before handing
    over to it, and ``publish_offer`` checks liquidity last.
    """
    if not nostr_enabled:
        return AnnounceState.DISABLED
    if not running:
        return AnnounceState.STOPPED
    if wallet_locked:
        return AnnounceState.WAITING_UNLOCK
    if pow_grinding:
        return AnnounceState.WAITING_POW
    if not has_liquidity_to_announce(min_amount=min_amount,
                                     max_forward=max_forward,
                                     max_reverse=max_reverse):
        return AnnounceState.NO_LIQUIDITY
    return AnnounceState.ANNOUNCING


class SwapServerGuiPlugin(BasePlugin):
    """Owns the swap-server lifecycle. The Qt layer subclasses this."""

    # publish_now() waits (bounded) for the server to actually have liquidity to
    # advertise before announcing. A server that just started may have no open
    # channels yet (e.g. on a fresh regtest rig the channels are funded shortly
    # after the server comes up); without this it would announce nothing.
    PUBLISH_NOW_LIQUIDITY_WAIT_SEC = 180
    PUBLISH_NOW_POLL_SEC = 3

    def __init__(self, parent: Any, config: 'SimpleConfig', name: str) -> None:
        BasePlugin.__init__(self, parent, config, name)
        self.wallet: Optional['Abstract_Wallet'] = None
        self._sm: Optional['SwapManager'] = None
        self._http_fut: Optional['concurrent.futures.Future'] = None
        self._nostr_fut: Optional['concurrent.futures.Future'] = None
        self._publish_now_fut: Optional['concurrent.futures.Future'] = None
        self._running: bool = False
        # Set once the announcement proof-of-work is usable (or once we know we
        # cannot compute one, e.g. no nostr keypair). Gates the nostr announce
        # path so we never publish an offer that takers would reject.
        self._pow_gate: asyncio.Event = asyncio.Event()
        self._pow_state: Optional['swap_pow.PowState'] = None
        self._pow_grinding: bool = False
        # Bookkeeping for the announcements *we* trigger (publish_now). Upstream's
        # run_nostr_server keeps its transport private and publish_offer is
        # decorated with @ignore_exceptions, so neither its schedule nor its
        # success is observable from here -- which is exactly why the Diagnostics
        # pane asks the relays instead of trusting local state.
        self._last_publish_attempt_at: Optional[float] = None
        self._last_publish_note: Optional[str] = None
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

    async def _nostr_startup(self) -> None:
        """Compute the PoW ourselves, then hand over to upstream's nostr server.

        Ordering matters: ``run_nostr_server`` begins with
        ``set_nostr_proof_of_work``, which grinds inside the un-cancellable
        upstream helper.  By seeding a good nonce first we guarantee it
        short-circuits, so cancelling this task at shutdown is always safe.
        """
        await self.ensure_pow_nonce()
        assert self._sm is not None
        await self._sm.run_nostr_server()

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

        port = self.config.SWAPSERVER_PORT
        relays = (self.config.NOSTR_RELAYS or "").strip()
        self._pow_gate = asyncio.Event()  # fresh gate for this run

        if port:
            server = ManagedHttpSwapServer(self.config, self.wallet)
            sm.http_server = server
            self._http_fut = self._spawn(server.run(), "http")
        if relays:
            # _nostr_startup seeds the proof-of-work before starting upstream's
            # nostr server; see the note in the module docstring.
            self._nostr_fut = self._spawn(self._nostr_startup(), "nostr")

        self._running = True
        self.logger.info(f"swap server started (http_port={port or None}, "
                          f"nostr_relays={len(relays.split(',')) if relays else 0})")

        # Announce immediately instead of waiting for run_nostr_server's first
        # OFFER_UPDATE_INTERVAL_SEC (~10 min) tick, so takers can discover us
        # right after start-up. No-op when no nostr relay is configured.
        if relays:
            self.publish_now()

    def publish_now(self) -> Optional['concurrent.futures.Future']:
        """Force an immediate swap announcement over nostr (non-blocking).

        Safe to call from the Qt GUI thread; returns at once (the work runs on
        the asyncio loop). Useful both as an explicit "announce now" action and,
        internally, right after :meth:`start_server` so we don't wait for
        ``run_nostr_server``'s first ``OFFER_UPDATE_INTERVAL_SEC`` tick. No-op
        unless the server is running with at least one nostr relay configured.
        """
        if not self._running:
            return None
        if not (self.config.NOSTR_RELAYS or "").strip():
            return None
        if self._sm is None or self.wallet is None or self.wallet.lnworker is None:
            return None
        if getattr(self.wallet.lnworker, "nostr_keypair", None) is None:
            return None
        self._publish_now_fut = self._spawn(self._one_shot_publish(), "publish-now")
        return self._publish_now_fut

    async def _one_shot_publish(self) -> None:
        """Publish a single announcement using a short-lived NostrTransport.

        ``run_nostr_server`` keeps its transport private, so we spin up our own
        (same pattern as the client's ``get_submarine_swap_providers``) with the
        server's nostr keypair, wait until there is liquidity to advertise, emit
        one offer, and tear it down. ``publish_offer`` refuses to announce when
        there is no liquidity, so we poll ``server_update_pairs`` up to
        :attr:`PUBLISH_NOW_LIQUIDITY_WAIT_SEC` before giving up.
        """
        sm = self._sm
        if sm is None or self.wallet is None or self.wallet.lnworker is None:
            return
        keypair = getattr(self.wallet.lnworker, "nostr_keypair", None)
        if keypair is None:
            return
        # Wait for the announcement proof-of-work instead of computing one here.
        # We must NOT call sm.set_nostr_proof_of_work(): it routes into
        # electrum.util.gen_nostr_ann_pow, which wedges the event loop thread if
        # this task is cancelled mid-grind. _nostr_startup owns the PoW and
        # opens the gate; that path is cancel-safe. Waiting here is cancellable.
        await self._pow_gate.wait()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.PUBLISH_NOW_LIQUIDITY_WAIT_SEC
        while True:
            try:
                sm.server_update_pairs()
            except Exception:
                self.logger.debug("server_update_pairs failed in publish_now",
                                  exc_info=True)
            min_amount = sm._min_amount or 0
            have_liquidity = min_amount > 0 and (
                (sm._max_forward or 0) >= min_amount
                or (sm._max_reverse or 0) >= min_amount)
            if have_liquidity:
                break
            if loop.time() >= deadline:
                self.logger.info("publish_now: no liquidity to advertise within "
                                 f"{self.PUBLISH_NOW_LIQUIDITY_WAIT_SEC}s; skipping")
                self._last_publish_note = (
                    f"gave up after {self.PUBLISH_NOW_LIQUIDITY_WAIT_SEC}s: "
                    f"no liquidity to advertise")
                return
            await asyncio.sleep(self.PUBLISH_NOW_POLL_SEC)
        transport = NostrTransport(self.config, sm, keypair)
        try:
            async with transport:
                try:
                    await asyncio.wait_for(transport.is_connected.wait(),
                                           timeout=transport.connect_timeout + 1)
                except asyncio.TimeoutError:
                    self.logger.info("publish_now: no relay connected; "
                                     "skipping immediate announcement")
                    self._last_publish_note = "no relay connected"
                    return
                self._last_publish_attempt_at = time.time()
                # publish_offer is decorated with @ignore_exceptions upstream, so
                # returning normally is NOT proof that a relay accepted the
                # event. Only the Diagnostics check can confirm that.
                await transport.publish_offer(sm)
                self._last_publish_note = "announcement sent to relays"
                self.logger.info("publish_now: immediate swap announcement published")
        except Exception as e:
            self._last_publish_note = f"failed: {type(e).__name__}: {e}"
            self.logger.warning("publish_now: immediate announcement failed",
                                exc_info=True)

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
        self._cancel_fut(self._publish_now_fut)
        self._http_fut = None
        self._nostr_fut = None
        self._publish_now_fut = None
        if sm is not None:
            sm.is_server = False
        self._running = False
        self._pow_grinding = False
        self.logger.info("swap server stopped")

    # ------------------------------------------------------------ diagnostics
    def wallet_is_locked(self) -> bool:
        """True while ``run_nostr_server`` would be stuck waiting for a password.

        Upstream loops on exactly this condition before it builds a transport
        (electrum/submarine_swaps.py, ``run_nostr_server``), so a password-
        protected wallet that was never unlocked announces nothing at all.
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
        data["announce_state"] = announce_state(
            running=self._running,
            nostr_enabled=bool(relays),
            wallet_locked=data["wallet_locked"],
            pow_grinding=self._pow_grinding,
            min_amount=data["min_amount"],
            max_forward=data["max_forward"],
            max_reverse=data["max_reverse"],
        )
        # What the announcement would actually carry, as opposed to the target.
        pubkey = self._nostr_pubkey()
        data["pow_bits_achieved"] = (
            swap_pow.pow_bits(pubkey, self.config.SWAPSERVER_ANN_POW_NONCE)
            if pubkey is not None else None)
        data["pow_default_taker_target"] = nostr_check.DEFAULT_TAKER_POW_TARGET
        data["min_swap_amount"] = MIN_SWAP_AMOUNT_SAT
        data["last_publish_attempt_at"] = self._last_publish_attempt_at
        data["last_publish_note"] = self._last_publish_note
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
        return ("Announcing. Use the discoverability check below to confirm the "
                "relays are actually serving the offer to takers.")

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
    """Confirmed swaps served by this node (mirrors the bundled swapserver
    plugin's ``get_history`` command, but as a plain sync helper)."""
    if not wallet.lnworker or not wallet.lnworker.swap_manager:
        return []
    sm = wallet.lnworker.swap_manager
    swap_group_ids = set()
    for swap in sm._swaps.values():
        group_id = swap.spending_txid if swap.is_reverse else swap.funding_txid
        if group_id is None:
            continue
        if swap.spending_txid is None \
                or wallet.adb.get_tx_height(swap.spending_txid).height() <= TX_HEIGHT_UNCONFIRMED:
            continue
        swap_group_ids.add(group_id)

    result: List[Dict[str, Any]] = []
    full_history = wallet.get_full_history()
    for swap_group_id in swap_group_ids:
        item = full_history.get('group:' + swap_group_id)
        if not item:
            continue
        result.append({
            'label': item['label'],
            'return_sat': int(item['value'].value),
            'date': item['date'].strftime("%Y-%m-%d"),
            'timestamp': item['timestamp'],
        })
    return sorted(result, key=lambda x: x['timestamp'])


def get_swap_summary(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate stats for a list produced by :func:`get_swap_history`."""
    if not history:
        return {'num_swaps': 0, 'overall_return_sat': 0, 'swaps_per_day': 0.0}
    profit_loss_sum = sum(s['return_sat'] for s in history)
    first_swap = min(s['timestamp'] for s in history)
    last_swap = max(s['timestamp'] for s in history)
    days = (last_swap - first_swap) // 86400
    swaps_per_day = (len(history) / days) if days > 0 else 0.0
    return {
        'num_swaps': len(history),
        'overall_return_sat': profit_loss_sum,
        'swaps_per_day': round(swaps_per_day, 2),
    }
