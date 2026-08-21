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
    ComponentKind, SERVED_SWAPS_DB_KEY, SwapServerGuiPlugin, SwapStatus,
    build_served_swap_rows, build_swap_rows, get_swap_summary,
    served_swaps_ledger,
)
from swapserver_gui.served_swaps import (  # noqa: E402
    OWN_LABEL_MARK, SERVED_LABEL_MARK,
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

    def format_amount_and_units(self, sat: int) -> str:
        return f"{int(sat)} sat"


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
    # "this payment hash is free": create_normal_swap rejects a hash that is
    # already a known preimage or part of a payment bundle.  A MagicMock answers
    # both with a truthy Mock, which reads as "already in use" and fails every
    # swap -- so these have to be pinned rather than left to auto-speccing.
    wallet.lnworker.get_preimage.return_value = None
    wallet.lnworker.has_payment_bundle.return_value = False
    # What get_full_history will find to group the on-chain legs with. Empty by
    # default; a test that cares sets it, because whether a swap group has a
    # lightning member decides whether upstream's group survives to be expanded
    # in Electrum's own History tab.
    wallet.lnworker.get_lightning_history.return_value = {}
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


def _onchain(txid: str, value: int, *, fee: int, ts: int) -> Dict[str, Any]:
    """A wallet history entry for an on-chain transaction."""
    return {
        'txid': txid,
        'lightning': False,
        'value': _Value(value),
        'bc_value': _Value(value),
        'ln_value': _Value(0),
        'fee_sat': fee,
        'timestamp': ts,
        'date': datetime.datetime.fromtimestamp(ts),
    }


def _ln(payment_hash: str, value: int, *, ts: int) -> Dict[str, Any]:
    """A wallet history entry for a lightning payment."""
    return {
        'payment_hash': payment_hash,
        'lightning': True,
        'value': _Value(value),
        'ln_value': _Value(value),
        'bc_value': _Value(0),
        'timestamp': ts,
        'date': datetime.datetime.fromtimestamp(ts),
    }


def _history(*entries: Dict[str, Any]) -> Dict[str, Any]:
    return {(e.get('txid') or e.get('payment_hash')): e for e in entries}


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
        # claimed by one batched transaction, which is exactly what
        # ``_claim_swap`` does: every claim goes into the one 'swaps' batch.
        self._confirm(served_reverse, funding='tx_served_funding', spending='tx_served_claim')
        self._confirm(served_forward, funding='tx_f', spending='tx_batch')
        self._confirm(own_reverse, funding='tx_r', spending='tx_batch')
        self._confirm(own_forward, funding='tx_own_funding', spending='tx_own_claim')

        sr = self.sm._swaps[served_reverse]   # we funded on-chain
        sf = self.sm._swaps[served_forward]   # we claim on-chain
        orv = self.sm._swaps[own_reverse]     # ours, claimed by the same batch
        batch_delta = sf.onchain_amount + orv.onchain_amount - 500
        self.wallet.get_full_history.return_value = _history(
            _onchain('tx_served_funding', -sr.onchain_amount - 300, fee=300,
                     ts=1_700_000_000),
            _ln(served_reverse, sr.lightning_amount - 2_000, ts=1_700_000_000),
            _ln(sr.prepay_hash.hex(), 2_000, ts=1_700_000_000),
            _onchain('tx_batch', batch_delta, fee=500, ts=1_700_000_100),
            _ln(served_forward, -sf.lightning_amount, ts=1_700_000_100),
            _onchain('tx_own_funding', -900, fee=100, ts=1_700_000_200),
        )

        rows = build_served_swap_rows(self.wallet)
        self.assertEqual(len(rows), 2)                       # not 3, and not 4
        self.assertIn("Served reverse swap", rows[0].label)  # a taker's reverse swap
        self.assertIn("Served forward swap", rows[1].label)  # ...and forward swap

        # The funding swap is alone in its transaction, so it carries the whole
        # 300 sat fee: it is a complete, exact reconciliation.
        self.assertEqual(rows[0].batched_with, 0)
        self.assertEqual(rows[0].return_sat,
                         sr.lightning_amount - sr.onchain_amount - 300)

        # The claimed one shared its transaction with a swap of ours, so it pays
        # part of that transaction's fee -- some, but not all of it.
        self.assertEqual(rows[1].batched_with, 1)
        claim = [c for c in rows[1].components if c.kind is ComponentKind.CLAIM_TX][0]
        self.assertTrue(claim.in_wallet)
        self.assertLess(claim.value_sat, sf.onchain_amount)
        self.assertGreater(claim.value_sat, sf.onchain_amount - 500)
        self.assertEqual(rows[1].return_sat, claim.value_sat - sf.lightning_amount)

        # The taker's own leg is listed but is not ours to account for.
        funding = [c for c in rows[1].components if c.kind is ComponentKind.FUNDING_TX][0]
        self.assertFalse(funding.in_wallet)
        self.assertIsNone(funding.value_sat)

        summary = get_swap_summary(rows)
        self.assertEqual(summary['num_swaps'], 2)
        self.assertEqual(summary['overall_return_sat'],
                         rows[0].return_sat + rows[1].return_sat)
        self.assertEqual(summary['num_batched'], 1)

    def test_a_served_swap_is_one_row_not_one_per_leg(self) -> None:
        """The bug this replaces: a swap showing up as several entries."""
        self._start_server()
        served = self._serve_taker_reverse_swap()
        self._confirm(served, funding='tx_fund', spending='tx_taker_claim')
        swap = self.sm._swaps[served]
        self.wallet.get_full_history.return_value = _history(
            _onchain('tx_fund', -swap.onchain_amount - 300, fee=300, ts=1_700_000_000),
            _ln(served, swap.lightning_amount - 2_000, ts=1_700_000_000),
            _ln(swap.prepay_hash.hex(), 2_000, ts=1_700_000_000),
        )
        rows = build_served_swap_rows(self.wallet)
        self.assertEqual(len(rows), 1)
        # ...and the one row carries every leg, including the taker's.
        self.assertEqual([c.kind for c in rows[0].components],
                         [ComponentKind.LN_PAYMENT, ComponentKind.LN_PREPAYMENT,
                          ComponentKind.FUNDING_TX, ComponentKind.CLAIM_TX])
        self.assertEqual([c.in_wallet for c in rows[0].components],
                         [True, True, True, False])

    def test_electrums_own_history_labels_are_left_untouched(self) -> None:
        """Electrum's History tab is upstream's again, against the real builder.

        This plugin used to wrap
        ``SwapManager.get_groups_for_onchain_history`` so that swap rows in
        Electrum's own History tab would say which side of the swap we were on.
        Reorganising that history is now the "History (Swaps)" tab's job, and
        the History tab has to render exactly as upstream renders it --
        including upstream's own confusing name for a served forward swap
        ("Reverse swap", after *our* copy's is_reverse flag), which is not this
        plugin's to correct in someone else's tab.
        """
        self._start_server()
        served_forward = self._serve_taker_forward_swap()
        own_forward = self._own_forward_swap()
        self._confirm(served_forward, funding='tx_f', spending='tx_claim')
        self._confirm(own_forward, funding='tx_own_funding', spending='tx_own_claim')
        # upstream reads the spending tx back to look for a preimage; there is
        # no chain here, so tell it there is no such transaction.
        self.wallet.lnworker.lnwatcher.adb.get_transaction.return_value = None
        self.wallet.lnworker.get_lightning_history.return_value = {
            served_forward: None, own_forward: None}

        # Nothing of ours is hung on the swap manager, even though the plugin is
        # bound to the wallet and the server is running...
        self.assertNotIn('get_groups_for_onchain_history', self.sm.__dict__)
        groups = self.sm.get_groups_for_onchain_history()

        # ...so upstream's labels come through untouched, marks and all.
        self.assertIn('Reverse swap', groups['tx_claim']['group_label'])
        self.assertEqual(groups['tx_claim']['label'], 'Claim transaction')
        for txid, entry in groups.items():
            for key in ('label', 'group_label'):
                self.assertNotIn(SERVED_LABEL_MARK, entry.get(key) or '',
                                 f"{txid}.{key} was rewritten")
                self.assertNotIn(OWN_LABEL_MARK, entry.get(key) or '',
                                 f"{txid}.{key} was rewritten")

    def test_an_unbroadcast_batch_is_reported_as_local_not_as_served(self) -> None:
        """The stranded-batch case, end to end.

        ``TxBatch.run_iteration`` adds a batch transaction to the wallet before
        broadcasting it and deliberately keeps it when the broadcast fails with
        no base transaction to fall back on, and only retries while the batch is
        still open -- so a batch can sit in the wallet as "Local" indefinitely.
        Such a swap has not settled: it must not be counted as served, and the
        Swaps history tab has to be able to name it rather than show it as a
        swap that completed.
        """
        from electrum.address_synchronizer import TX_HEIGHT_LOCAL

        self._start_server()
        served = self._serve_taker_reverse_swap()
        self._confirm(served, funding='tx_fund', spending='tx_spend')
        self.wallet.lnworker.lnwatcher.adb.get_transaction.return_value = None
        # the spending tx is in the wallet but never made it to the network
        self.wallet.adb.get_tx_height.side_effect = lambda txid: _TxHeight(
            TX_HEIGHT_LOCAL if txid == 'tx_spend' else 100)

        # The Swap Server tab's population skips it, exactly as before.
        self.assertEqual(build_served_swap_rows(self.wallet), [])

        # The Swaps history tab shows it, named for what it is, and keeps it out
        # of the net return.
        rows = build_swap_rows(self.wallet, include_own=True, include_pending=True)
        local = [row for row in rows if row.payment_hash == served]
        self.assertEqual(len(local), 1, "the local swap is missing from the tab")
        self.assertIs(local[0].status, SwapStatus.LOCAL)
        self.assertFalse(local[0].counts_towards_total)
        self.assertEqual(get_swap_summary(rows)['overall_return_sat'], 0)
        self.assertEqual(get_swap_summary(rows)['num_pending'], 1)

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
        reloaded_sm = _make_swap_manager(reloaded_wallet)
        self.assertEqual(set(reloaded_sm._swaps), {served, own})
        swap = reloaded_sm._swaps[served]
        # The taker pays a served reverse swap in two parts -- the hold invoice
        # and the mining-fee prepayment (create_normal_swap, prepay=True) -- and
        # both have to be in the history for the row to be a complete one.
        prepay_sat = 2 * reloaded_sm.mining_fee
        reloaded_wallet.get_full_history.return_value = _history(
            _onchain('tx_served_funding', -swap.onchain_amount - 300, fee=300,
                     ts=1_700_000_000),
            _ln(served, swap.lightning_amount - prepay_sat, ts=1_700_000_000),
            _ln(swap.prepay_hash.hex(), prepay_sat, ts=1_700_000_000),
            _onchain('tx_own_funding', -900, fee=100, ts=1_700_000_200),
        )

        rows = build_served_swap_rows(reloaded_wallet)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].payment_hash, served)
        self.assertTrue(rows[0].is_complete)
        self.assertEqual(rows[0].return_sat,
                         swap.lightning_amount - swap.onchain_amount - 300)

    def test_a_missing_prepayment_leaves_the_return_unstated(self) -> None:
        """Without the prepayment the sum is short by it, not smaller by it."""
        self._start_server()
        served = self._serve_taker_reverse_swap()
        self._confirm(served, funding='tx_served_funding', spending='tx_served_claim')
        swap = self.sm._swaps[served]
        self.wallet.get_full_history.return_value = _history(
            _onchain('tx_served_funding', -swap.onchain_amount - 300, fee=300,
                     ts=1_700_000_000),
            _ln(served, swap.lightning_amount - 2 * self.sm.mining_fee,
                ts=1_700_000_000),
        )

        rows = build_served_swap_rows(self.wallet)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].is_complete)
        self.assertIsNone(rows[0].return_sat)
        self.assertEqual(get_swap_summary(rows)['num_incomplete'], 1)
        self.assertEqual(get_swap_summary(rows)['overall_return_sat'], 0)

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
        swap = self.sm._swaps[response['id']]
        self.wallet.get_full_history.return_value = _history(
            _onchain('tx_late', -swap.onchain_amount - 100, fee=100, ts=1_700_000_300),
        )
        self.assertEqual(len(build_served_swap_rows(self.wallet)), 1)


if __name__ == "__main__":
    unittest.main()
