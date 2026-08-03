#!/usr/bin/env python3
"""End-to-end test: only swaps served over the wire reach the Status tab.

Everything below the plugin API is real here — the plugin's own
``start_server``, the aiohttp endpoint on a loopback port, Electrum's
``SwapManager`` with its real ``server_create_swap`` /
``server_create_normal_swap`` handlers, real ``SwapData`` objects, and a real
``WalletDB`` that the test dumps and reloads.  Only lightning and the network
are stubbed, because a swap server needs neither to *create* a swap.

Two properties are under test:

  * a swap requested by a remote taker over HTTP is recorded and shows up in
    the history, while a swap this wallet initiated itself never does;
  * both survive a wallet-file round trip, so the classification is not an
    artefact of in-memory state.

Run with:  python3 -m pytest tests/test_served_swaps_e2e.py
"""
import asyncio
import datetime
import json
import os
import socket
import sys
import threading
import unittest
import urllib.request
from decimal import Decimal
from typing import Any, Dict, Optional
from unittest import mock

# --- make electrum + the plugin importable ---------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))  # /home/user/electrum_swapgui
_ELECTRUM_SRC = os.environ.get("ELECTRUM_SRC", os.path.join(_PROJECT_ROOT, "electrum"))
_PLUGINS_DIR = os.path.join(os.path.dirname(_HERE), "plugins")
for _p in (_ELECTRUM_SRC, _PLUGINS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from electrum.crypto import sha256  # noqa: E402
from electrum.submarine_swaps import SwapManager  # noqa: E402
from electrum.wallet_db import WalletDB  # noqa: E402

from swapserver_gui.swapserver_gui import (  # noqa: E402
    SERVED_SWAPS_DB_KEY, SwapServerGuiPlugin, get_swap_history, get_swap_summary,
    served_swaps_ledger,
)

THEIR_PUBKEY = bytes.fromhex('02' + '11' * 32)  # the taker's key, we only embed it


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Config:
    def __init__(self, port: int) -> None:
        self.SWAPSERVER_PORT = port
        self.NOSTR_RELAYS = ""          # HTTP only: no relay traffic from a test
        self.SWAPSERVER_FEE_MILLIONTHS = 5000
        self.SWAPSERVER_POW_TARGET = 0
        self.SWAPSERVER_GUI_AUTOSTART = False


class _Value:
    """Stand-in for electrum's Satoshis wrapper in a history group."""
    def __init__(self, value: int) -> None:
        self.value = value


class _TxHeight:
    def __init__(self, height: int) -> None:
        self._height = height

    def height(self) -> int:
        return self._height


def _make_wallet(db: WalletDB, config: _Config) -> Any:
    """A wallet stand-in carrying a *real* db; lightning/chain are stubbed."""
    wallet = mock.MagicMock()
    wallet.db = db
    wallet.config = config
    wallet.has_password.return_value = False
    wallet.get_unlocked_password.return_value = None
    wallet.adb.get_tx_height.return_value = _TxHeight(100)  # everything confirmed
    lnaddr = mock.Mock()
    lnaddr.get_min_final_cltv_delta.return_value = 200
    wallet.lnworker.get_bolt11_invoice.return_value = (lnaddr, 'lnbc1fakeinvoice')
    wallet.lnworker.create_payment_info.return_value = b'\x0f' * 32
    return wallet


def _make_swap_manager(wallet: Any) -> SwapManager:
    sm = SwapManager(wallet=wallet, lnworker=wallet.lnworker)
    wallet.lnworker.swap_manager = sm
    sm.network = mock.MagicMock()
    sm.network.get_local_height.return_value = 1000
    sm.network.blockchain().is_tip_stale.return_value = False
    # what server_update_pairs would have published
    sm.percentage = Decimal('0.5')
    sm.mining_fee = 1000
    sm._min_amount = 20_000
    sm._max_forward = 10_000_000
    sm._max_reverse = 10_000_000
    return sm


def _group(label: str, value: int, ts: int) -> Dict[str, Any]:
    return {
        'label': label,
        'value': _Value(value),
        'date': datetime.datetime.fromtimestamp(ts),
        'timestamp': ts,
    }


class ServedSwapsE2ETest(unittest.TestCase):

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        self.port = _free_port()
        self.config = _Config(self.port)
        self.db = WalletDB('', storage=None, upgrade=True)
        self.wallet = _make_wallet(self.db, self.config)
        self.sm = _make_swap_manager(self.wallet)
        self.plugin = SwapServerGuiPlugin(mock.MagicMock(), self.config, "swapserver_gui")
        self.plugin.bind_wallet(self.wallet)
        self._loop_patch = mock.patch(
            "swapserver_gui.swapserver_gui.get_asyncio_loop", return_value=self.loop)
        self._loop_patch.start()
        self.addCleanup(self._loop_patch.stop)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def tearDown(self) -> None:
        try:
            self.plugin.stop_server()
        finally:
            # let the fire-and-forget http-stop coroutine finish before teardown
            try:
                asyncio.run_coroutine_threadsafe(
                    asyncio.sleep(0.2), self.loop).result(timeout=5)
            except Exception:
                pass
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)
            self.loop.close()

    # ------------------------------------------------------------- utilities
    def _start_server(self) -> None:
        self.plugin.start_server()
        # start_server schedules ManagedHttpSwapServer.run(); wait for the socket.
        # Probing at the TCP level rather than with GET /getpairs keeps the
        # liquidity machinery (server_update_pairs -> lnworker) out of a test
        # about swap bookkeeping.
        deadline = 5.0
        while deadline > 0:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
                return
            except OSError:
                deadline -= 0.1
                threading.Event().wait(0.1)
        self.fail("the swap server's HTTP endpoint never came up")

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            return json.loads(resp.read())

    def _serve_taker_reverse_swap(self) -> str:
        """A taker asks for a reverse swap -> our create_normal_swap. is_reverse=False."""
        preimage_hash = sha256(b'\x07' * 32)
        response = self._post('/createswap', {
            'type': 'reversesubmarine',
            'pairId': 'BTC/BTC',
            'invoiceAmount': 200_000,
            'preimageHash': preimage_hash.hex(),
            'claimPublicKey': THEIR_PUBKEY.hex(),
        })
        return response['id']

    def _serve_taker_forward_swap(self) -> str:
        """A taker asks for a forward swap -> our create_reverse_swap. is_reverse=True."""
        response = self._post('/createnormalswap', {
            'invoiceAmount': 300_000,
            'refundPublicKey': THEIR_PUBKEY.hex(),
        })
        return response['id']

    def _own_forward_swap(self) -> str:
        """What request_normal_swap stores for a swap *we* initiate (prepay=False)."""
        payment_hash = sha256(b'\x21' * 32)
        self.sm.add_normal_swap(
            redeem_script=b'\x51' * 40,
            locktime=1100,
            onchain_amount_sat=100_000,
            lightning_amount_sat=101_000,
            payment_hash=payment_hash,
            our_privkey=b'\x03' * 32,
            prepay=False,
        )
        return payment_hash.hex()

    def _own_reverse_swap(self) -> str:
        """What reverse_swap stores for a swap *we* initiate (server prepay hash)."""
        preimage = b'\x22' * 32
        payment_hash = sha256(preimage)
        self.sm.add_reverse_swap(
            redeem_script=b'\x52' * 40,
            locktime=1100,
            privkey=b'\x04' * 32,
            lightning_amount_sat=150_000,
            onchain_amount_sat=148_000,
            preimage=preimage,
            payment_hash=payment_hash,
            prepay_hash=b'\x33' * 32,
        )
        return payment_hash.hex()

    def _confirm(self, payment_hash_hex: str, *, funding: str, spending: str) -> None:
        swap = self.sm._swaps[payment_hash_hex]
        swap.funding_txid = funding
        swap.spending_txid = spending

    # ----------------------------------------------------------------- tests
    def test_only_swaps_served_over_http_are_counted(self) -> None:
        self._start_server()
        served_reverse = self._serve_taker_reverse_swap()
        served_forward = self._serve_taker_forward_swap()
        own_forward = self._own_forward_swap()
        own_reverse = self._own_reverse_swap()

        # Both server entry points were recorded at creation time; neither of
        # our own swaps was (they never went through the server).
        self.assertEqual(set(served_swaps_ledger(self.wallet)),
                         {served_reverse, served_forward})

        # The upstream code really does leave the field signatures the fallback
        # heuristic relies on -- assert against the objects it just built.
        self.assertFalse(self.sm._swaps[served_reverse].is_reverse)
        self.assertIsNotNone(self.sm._swaps[served_reverse].prepay_hash)
        self.assertTrue(self.sm._swaps[served_forward].is_reverse)
        self.assertIsNone(self.sm._swaps[served_forward].prepay_hash)
        self.assertFalse(self.sm._swaps[own_forward].is_reverse)
        self.assertIsNone(self.sm._swaps[own_forward].prepay_hash)
        self.assertTrue(self.sm._swaps[own_reverse].is_reverse)
        self.assertIsNotNone(self.sm._swaps[own_reverse].prepay_hash)

        # Confirm them. The served forward swap and our own reverse swap are
        # claimed by one batched tx, which is what makes a group "mixed".
        self._confirm(served_reverse, funding='tx_served_funding', spending='tx_served_claim')
        self._confirm(served_forward, funding='tx_f', spending='tx_batched_claim')
        self._confirm(own_reverse, funding='tx_r', spending='tx_batched_claim')
        self._confirm(own_forward, funding='tx_own_funding', spending='tx_own_claim')
        self.wallet.get_full_history.return_value = {
            'group:tx_served_funding': _group('Forward swap 0.2 mBTC', 205, 1_700_000_000),
            'group:tx_batched_claim': _group('Reverse swap 0.3 mBTC', 64, 1_700_000_100),
            'group:tx_own_funding': _group('Forward swap 0.1 mBTC', -900, 1_700_000_200),
        }

        history = get_swap_history(self.wallet)
        self.assertEqual([h['label'] for h in history],
                         ['Forward swap 0.2 mBTC', 'Reverse swap 0.3 mBTC'])
        self.assertEqual([h['is_mixed'] for h in history], [False, True])
        self.assertEqual(history[1]['num_served_swaps'], 1)
        self.assertEqual(history[1]['num_own_swaps'], 1)

        summary = get_swap_summary(history)
        self.assertEqual(summary['num_swaps'], 2)          # not 3
        self.assertEqual(summary['overall_return_sat'], 269)  # our -900 is not here
        self.assertEqual(summary['num_mixed'], 1)

    def test_classification_survives_a_wallet_file_round_trip(self) -> None:
        self._start_server()
        served = self._serve_taker_reverse_swap()
        own = self._own_forward_swap()
        self._confirm(served, funding='tx_served_funding', spending='tx_served_claim')
        self._confirm(own, funding='tx_own_funding', spending='tx_own_claim')

        # Save and reload the wallet file, then rebuild the swap manager from it
        # exactly as opening the wallet again would.
        reloaded_db = WalletDB(self.db.dump(), storage=None, upgrade=True)
        self.assertEqual(set(reloaded_db.get(SERVED_SWAPS_DB_KEY)), {served})
        reloaded_wallet = _make_wallet(reloaded_db, self.config)
        _make_swap_manager(reloaded_wallet)
        self.assertEqual(set(reloaded_wallet.lnworker.swap_manager._swaps),
                         {served, own})
        reloaded_wallet.get_full_history.return_value = {
            'group:tx_served_funding': _group('Forward swap 0.2 mBTC', 205, 1_700_000_000),
            'group:tx_own_funding': _group('Forward swap 0.1 mBTC', -900, 1_700_000_200),
        }

        history = get_swap_history(reloaded_wallet)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['return_sat'], 205)

    def test_recorder_is_removed_when_the_server_stops(self) -> None:
        self._start_server()
        self._serve_taker_reverse_swap()
        self.plugin.stop_server()
        self.assertNotIn('server_create_swap', self.sm.__dict__)
        self.assertNotIn('server_create_normal_swap', self.sm.__dict__)
        # A direct call now bypasses the ledger (nothing can reach it anyway --
        # the endpoint is down); the heuristic still classifies it correctly.
        before = set(served_swaps_ledger(self.wallet))
        payment_hash = sha256(b'\x44' * 32)
        response = self.sm.server_create_swap({
            'type': 'reversesubmarine',
            'pairId': 'BTC/BTC',
            'invoiceAmount': 200_000,
            'preimageHash': payment_hash.hex(),
            'claimPublicKey': THEIR_PUBKEY.hex(),
        })
        self.assertEqual(set(served_swaps_ledger(self.wallet)), before)
        self._confirm(response['id'], funding='tx_late', spending='tx_late_claim')
        self.wallet.get_full_history.return_value = {
            'group:tx_late': _group('Forward swap', 12, 1_700_000_300),
        }
        self.assertEqual(len(get_swap_history(self.wallet)), 1)


if __name__ == "__main__":
    unittest.main()
