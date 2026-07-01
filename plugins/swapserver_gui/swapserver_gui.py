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

import asyncio
import concurrent.futures
from typing import TYPE_CHECKING, Optional, List, Dict, Any

from aiohttp import web

from electrum.plugin import BasePlugin
from electrum.util import get_asyncio_loop
from electrum.address_synchronizer import TX_HEIGHT_UNCONFIRMED

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


class SwapServerGuiPlugin(BasePlugin):
    """Owns the swap-server lifecycle. The Qt layer subclasses this."""

    def __init__(self, parent: Any, config: 'SimpleConfig', name: str) -> None:
        BasePlugin.__init__(self, parent, config, name)
        self.wallet: Optional['Abstract_Wallet'] = None
        self._sm: Optional['SwapManager'] = None
        self._http_fut: Optional['concurrent.futures.Future'] = None
        self._nostr_fut: Optional['concurrent.futures.Future'] = None
        self._running: bool = False

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

        if port:
            server = ManagedHttpSwapServer(self.config, self.wallet)
            sm.http_server = server
            self._http_fut = self._spawn(server.run(), "http")
        if relays:
            self._nostr_fut = self._spawn(sm.run_nostr_server(), "nostr")

        self._running = True
        self.logger.info(f"swap server started (http_port={port or None}, "
                          f"nostr_relays={len(relays.split(',')) if relays else 0})")

    def stop_server(self) -> None:
        """Stop all server transports. Idempotent."""
        if not self._running:
            return
        sm = self._sm
        # Stop the HTTP listener (needs an explicit aiohttp runner cleanup).
        # Schedule it on the loop fire-and-forget; do NOT block the GUI thread.
        if sm is not None and isinstance(getattr(sm, 'http_server', None), ManagedHttpSwapServer):
            self._spawn(sm.http_server.stop(), "http-stop")
            sm.http_server = None
        self._cancel_fut(self._http_fut)
        self._cancel_fut(self._nostr_fut)
        self._http_fut = None
        self._nostr_fut = None
        if sm is not None:
            sm.is_server = False
        self._running = False
        self.logger.info("swap server stopped")

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
        return data


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
