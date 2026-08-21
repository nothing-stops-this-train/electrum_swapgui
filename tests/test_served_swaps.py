#!/usr/bin/env python3
"""Unit tests for served-swap bookkeeping.

Three things are pinned down here:

* telling swaps this server *served* apart from the operator's own.  The Status
  tab used to report every swap in the wallet as "swaps served", because
  ``SwapManager._swaps`` is a single store shared by both roles and ``SwapData``
  carries no field naming the side we were on.
* one served swap = exactly one row, with its components enumerated.  A swap has
  several legs and the wallet's history both collapses a single-leg group into
  the leg itself and merges swaps that shared a batch transaction, so rows built
  from history groups could be labelled after a leg, or cover several swaps.
* per-swap values, including the split of a batch transaction's mining fee.

Run with:  python3 -m pytest tests/test_served_swaps.py
"""
import datetime
import os
import sys
import unittest
from typing import Any, Dict, List, Optional, Set
from unittest import mock

# --- make electrum + the plugin importable ---------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))  # /home/user/electrum_swapgui
_ELECTRUM_SRC = os.environ.get("ELECTRUM_SRC", os.path.join(_PROJECT_ROOT, "electrum"))
_PLUGINS_DIR = os.path.join(os.path.dirname(_HERE), "plugins")
for _p in (_ELECTRUM_SRC, _PLUGINS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from electrum.wallet_db import WalletDB  # noqa: E402

from electrum.address_synchronizer import (  # noqa: E402
    TX_HEIGHT_FUTURE, TX_HEIGHT_LOCAL, TX_HEIGHT_UNCONF_PARENT,
    TX_HEIGHT_UNCONFIRMED,
)

from swapserver_gui.swapserver_gui import (  # noqa: E402
    ComponentKind, SERVED_SWAPS_DB_KEY, SwapRole, SwapServerGuiPlugin, SwapStatus,
    build_served_swap_rows, classify_swap, format_batched_note,
    format_incomplete_note, format_summary_line, format_swap_label,
    format_unattributed_note, get_swap_history, get_swap_summary,
    is_served_swap, looks_server_side, record_served_swap,
    served_swaps_ledger, swap_margin_sat,
)
from swapserver_gui.served_swaps import (  # noqa: E402
    FUNDING_OUTPUT_VBYTES, OWN_LABEL_MARK, SERVED_LABEL_MARK, build_swap_rows,
    claim_input_vbytes, flatten_history, relabel_swap_history_items, split_fee,
    swap_status, taker_did_forward_swap,
)


#: A swap redeem script is ~110 bytes; the exact number only matters in that it
#: is what the claim input's witness carries, so it is fixed here.
REDEEM_SCRIPT = b'\x21' * 110
CLAIM_VBYTES = claim_input_vbytes(len(REDEEM_SCRIPT))


class _Swap:
    """The ``SwapData`` fields the classifier and the row builder look at."""

    def __init__(
            self, *,
            is_reverse: bool,
            prepay_hash: Optional[bytes] = None,
            claim_to_output: Optional[Any] = None,
            funding_txid: Optional[str] = None,
            spending_txid: Optional[str] = None,
            onchain_amount: int = 100_000,
            lightning_amount: int = 99_000,
            preimage: Optional[bytes] = b'\x99' * 32,
            redeem_script: bytes = REDEEM_SCRIPT,
            lockup_address: str = 'bc1qlockup',
            locktime: int = 800_000,
    ) -> None:
        self.is_reverse = is_reverse
        self.prepay_hash = prepay_hash
        self.claim_to_output = claim_to_output
        self.funding_txid = funding_txid
        self.spending_txid = spending_txid
        self.onchain_amount = onchain_amount
        self.lightning_amount = lightning_amount
        self.preimage = preimage
        self.redeem_script = redeem_script
        self.lockup_address = lockup_address
        self.locktime = locktime


# The four ways a swap can end up in the wallet, per electrum/submarine_swaps.py.
def _served_reverse_swap(**kw: Any) -> _Swap:
    """Us serving a taker's reverse swap: create_normal_swap -> prepay=True."""
    kw.setdefault('prepay_hash', b'\x01' * 32)
    return _Swap(is_reverse=False, **kw)


def _served_forward_swap(**kw: Any) -> _Swap:
    """Us serving a taker's forward swap: create_reverse_swap."""
    return _Swap(is_reverse=True, **kw)


def _own_forward_swap(**kw: Any) -> _Swap:
    """Our own forward swap: request_normal_swap -> prepay=False."""
    return _Swap(is_reverse=False, **kw)


def _own_reverse_swap(**kw: Any) -> _Swap:
    """Our own reverse swap: reverse_swap, with the server's minerFeeInvoice."""
    kw.setdefault('prepay_hash', b'\x02' * 32)
    return _Swap(is_reverse=True, **kw)


class _Value:
    """Stand-in for electrum.util.Satoshis."""

    def __init__(self, value: int) -> None:
        self.value = value


class _TxHeight:
    def __init__(self, height: int) -> None:
        self._height = height

    def height(self) -> int:
        return self._height


def _onchain(txid: str, value: int, *, fee: int = 0,
             ts: int = 1_700_000_000) -> Dict[str, Any]:
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


def _ln(payment_hash: str, value: int, *, ts: int = 1_700_000_000) -> Dict[str, Any]:
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


def _make_wallet(swaps: Dict[str, _Swap], *, history: Optional[List[Dict[str, Any]]] = None,
                 confirmed: bool = True,
                 heights: Optional[Dict[str, int]] = None) -> Any:
    """A wallet stand-in with a *real* db, so the ledger is really persisted.

    ``history`` is a flat list of entries; ``get_full_history`` keys them the way
    the wallet does, which the row builder must not depend on.

    ``heights`` overrides ``adb.get_tx_height`` per txid, which is what decides
    a swap's :class:`SwapStatus`.  Without it every transaction reports the same
    height, per ``confirmed``.  Use the ``TX_HEIGHT_*`` constants for the
    negative ones -- ``TX_HEIGHT_LOCAL`` is the interesting case, because that
    is a batch transaction that was never broadcast.
    """
    wallet = mock.MagicMock()
    wallet.db = WalletDB('', storage=None, upgrade=True)
    wallet.lnworker.swap_manager._swaps = swaps
    # No watcher: lockup values then fall back to the swap's expected amount,
    # which is what they are in every normal case anyway.
    wallet.lnworker.swap_manager.lnwatcher = None
    default_height = 100 if confirmed else 0
    if heights is None:
        wallet.adb.get_tx_height.return_value = _TxHeight(default_height)
    else:
        wallet.adb.get_tx_height.side_effect = \
            lambda txid: _TxHeight(heights.get(txid, default_height))
    entries = history if history is not None else []
    wallet.get_full_history.return_value = {
        (item.get('txid') or item.get('payment_hash')): item for item in entries
    }
    return wallet


class LooksServerSideTests(unittest.TestCase):
    """The heuristic used for swaps created before the ledger existed."""

    def test_served_reverse_swap(self) -> None:
        self.assertTrue(looks_server_side(_served_reverse_swap()))

    def test_served_forward_swap(self) -> None:
        self.assertTrue(looks_server_side(_served_forward_swap()))

    def test_own_forward_swap(self) -> None:
        self.assertFalse(looks_server_side(_own_forward_swap()))

    def test_own_reverse_swap(self) -> None:
        self.assertFalse(looks_server_side(_own_reverse_swap()))

    def test_own_submarine_payment(self) -> None:
        # claim_to_output is only ever set by the client (reverse_swap), even
        # when the server asked for no prepayment.
        swap = _Swap(is_reverse=True, claim_to_output=('bc1qexample', 1000))
        self.assertFalse(looks_server_side(swap))


class TakerRoleTests(unittest.TestCase):
    """``is_reverse`` means the opposite thing on each side of the same swap."""

    def test_served_swap_mirrors_the_taker(self) -> None:
        # server_create_normal_swap -- the handler for a taker's *forward* swap
        # -- calls create_reverse_swap, so our copy has is_reverse=True.
        self.assertTrue(taker_did_forward_swap(_served_forward_swap(), served=True))
        self.assertFalse(taker_did_forward_swap(_served_reverse_swap(), served=True))

    def test_own_swap_is_stored_from_our_own_side(self) -> None:
        self.assertTrue(taker_did_forward_swap(_own_forward_swap(), served=False))
        self.assertFalse(taker_did_forward_swap(_own_reverse_swap(), served=False))


class LedgerTests(unittest.TestCase):
    def test_record_and_read_back(self) -> None:
        wallet = _make_wallet({})
        self.assertTrue(record_served_swap(wallet, 'ab' * 32))
        self.assertIn('ab' * 32, served_swaps_ledger(wallet))
        self.assertEqual(wallet.db.get(SERVED_SWAPS_DB_KEY).keys(), {'ab' * 32})

    def test_record_is_idempotent(self) -> None:
        wallet = _make_wallet({})
        self.assertTrue(record_served_swap(wallet, 'cd' * 32))
        self.assertFalse(record_served_swap(wallet, 'cd' * 32))
        self.assertEqual(len(served_swaps_ledger(wallet)), 1)

    def test_ignores_empty_payment_hash(self) -> None:
        wallet = _make_wallet({})
        self.assertFalse(record_served_swap(wallet, ''))
        self.assertEqual(served_swaps_ledger(wallet), {})

    def test_ledger_overrides_heuristic(self) -> None:
        # A swap the ledger knows about counts as served even when its fields
        # say otherwise -- the recording happened at creation time and is exact.
        swap = _own_reverse_swap()
        self.assertTrue(is_served_swap(swap, 'ef' * 32, {'ef' * 32: 1}))
        self.assertFalse(is_served_swap(swap, 'ef' * 32, {}))

    def test_heuristic_used_when_not_in_ledger(self) -> None:
        self.assertTrue(is_served_swap(_served_forward_swap(), 'ff' * 32, {}))


class FeeSplitTests(unittest.TestCase):
    """The shares must add back up, or the per-swap values do not reconcile."""

    def test_sums_to_the_fee_exactly(self) -> None:
        for fee in (1, 2, 3, 7, 199, 1000, 12345):
            shares = split_fee({'a': 96, 'b': 43, 'c': 43}, fee)
            self.assertEqual(sum(shares.values()), fee, f"{fee=}")

    def test_larger_leg_pays_more(self) -> None:
        shares = split_fee({'claim': CLAIM_VBYTES, 'funding': FUNDING_OUTPUT_VBYTES}, 1000)
        self.assertGreater(shares['claim'], shares['funding'])

    def test_zero_and_negative_fee(self) -> None:
        self.assertEqual(split_fee({'a': 96, 'b': 43}, 0), {'a': 0, 'b': 0})
        self.assertEqual(split_fee({'a': 96, 'b': 43}, -5), {'a': 0, 'b': 0})

    def test_no_legs(self) -> None:
        self.assertEqual(split_fee({}, 500), {})

    def test_zero_weights_split_evenly(self) -> None:
        shares = split_fee({'a': 0, 'b': 0}, 10)
        self.assertEqual(sum(shares.values()), 10)
        self.assertEqual(shares['a'], shares['b'])

    def test_does_not_depend_on_dict_ordering(self) -> None:
        forward = split_fee({'a': 43, 'b': 43, 'c': 43}, 100)
        backward = split_fee({'c': 43, 'b': 43, 'a': 43}, 100)
        self.assertEqual(forward, backward)


class LegSizeTests(unittest.TestCase):
    def test_claim_input_is_bigger_than_a_funding_output(self) -> None:
        # A claim carries a witness (sig + preimage + redeem script); a funding
        # output is 43 bytes of scriptPubKey. The split has to reflect that.
        self.assertGreater(claim_input_vbytes(len(REDEEM_SCRIPT)), FUNDING_OUTPUT_VBYTES)

    def test_grows_with_the_redeem_script(self) -> None:
        self.assertLess(claim_input_vbytes(50), claim_input_vbytes(150))

    def test_matches_a_hand_computed_witness(self) -> None:
        # 41 base bytes * 4 + witness(3 items: 71-byte sig, 32-byte preimage,
        # 110-byte script) = 164 + (1 + 72 + 33 + 111) = 381 wu -> 96 vbytes.
        self.assertEqual(claim_input_vbytes(110), 96)


class FlattenHistoryTests(unittest.TestCase):
    """Both shapes ``get_full_history`` can produce must index the same."""

    def test_indexes_group_children(self) -> None:
        child_tx = _onchain('tx1', 10)
        child_ln = _ln('ph1', -5)
        history = {'group:tx1': {'label': 'g', 'txid': 'tx1', 'lightning': False,
                                 'children': [child_tx, child_ln]}}
        onchain, lightning = flatten_history(history)
        self.assertIs(onchain['tx1'], child_tx)
        self.assertIs(lightning['ph1'], child_ln)

    def test_indexes_a_collapsed_single_member_group(self) -> None:
        # wallet.get_full_history replaces a one-member group with the member,
        # keeping the 'group:...' key. Indexing by identity sidesteps that.
        item = _onchain('tx1', 10)
        onchain, _ = flatten_history({'group:tx1': item})
        self.assertIs(onchain['tx1'], item)

    def test_ignores_the_placeholder_txid_of_a_lightning_only_group(self) -> None:
        history = {'group:x': {'txid': '----', 'lightning': False, 'children': []}}
        onchain, _ = flatten_history(history)
        self.assertEqual(onchain, {})


class SwapRowTests(unittest.TestCase):
    """One served swap is one row, with its own value."""

    def test_own_swaps_are_excluded(self) -> None:
        swaps = {
            'aa' * 32: _own_forward_swap(funding_txid='tx_own_f', spending_txid='tx_own_f2'),
            'bb' * 32: _own_reverse_swap(funding_txid='tx_own_r', spending_txid='tx_own_r2'),
        }
        wallet = _make_wallet(swaps, history=[
            _onchain('tx_own_f', -100_205), _onchain('tx_own_r2', 99_064),
        ])
        self.assertEqual(build_served_swap_rows(wallet), [])

    def test_a_served_forward_swap_is_one_row(self) -> None:
        # The taker funds on-chain and we claim; the claim tx is ours, the
        # funding tx is theirs. Upstream's grouping used to show this as either
        # "Forward swap ..." or "Claim transaction" depending on how many legs
        # landed in the group.
        swap = _served_forward_swap(funding_txid='tx_theirs', spending_txid='tx_claim')
        wallet = _make_wallet({'aa' * 32: swap}, history=[
            _onchain('tx_claim', 99_800, fee=200),
            _ln('aa' * 32, -99_000),
        ])
        rows = build_served_swap_rows(wallet)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertTrue(row.taker_is_forward)
        self.assertIn("Served forward swap", row.label)
        # claim 100_000 - fee 200 - lightning 99_000
        self.assertEqual(row.return_sat, 800)
        kinds = [c.kind for c in row.components]
        self.assertEqual(kinds, [ComponentKind.LN_PAYMENT, ComponentKind.FUNDING_TX,
                                 ComponentKind.CLAIM_TX])
        by_kind = {c.kind: c for c in row.components}
        self.assertTrue(by_kind[ComponentKind.CLAIM_TX].in_wallet)
        self.assertFalse(by_kind[ComponentKind.FUNDING_TX].in_wallet)  # the taker's
        self.assertEqual(by_kind[ComponentKind.FUNDING_TX].txid, 'tx_theirs')
        self.assertEqual(row.batched_with, 0)

    def test_a_served_reverse_swap_is_one_row(self) -> None:
        # We fund on-chain and the taker claims; the prepayment is a second
        # lightning leg of ours and belongs on the row.
        swap = _served_reverse_swap(funding_txid='tx_fund', spending_txid='tx_theirs',
                                    onchain_amount=100_000, lightning_amount=101_000)
        wallet = _make_wallet({'aa' * 32: swap}, history=[
            _onchain('tx_fund', -100_300, fee=300),
            _ln('aa' * 32, 100_550),
            _ln('01' * 32, 450),  # the prepay_hash of _served_reverse_swap
        ])
        rows = build_served_swap_rows(wallet)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertFalse(row.taker_is_forward)
        self.assertIn("Served reverse swap", row.label)
        # -(100_000 funded) - 300 fee + 100_550 + 450 prepay
        self.assertEqual(row.return_sat, 700)
        kinds = [c.kind for c in row.components]
        self.assertEqual(kinds, [ComponentKind.LN_PAYMENT, ComponentKind.LN_PREPAYMENT,
                                 ComponentKind.FUNDING_TX, ComponentKind.CLAIM_TX])
        by_kind = {c.kind: c for c in row.components}
        self.assertTrue(by_kind[ComponentKind.FUNDING_TX].in_wallet)
        self.assertFalse(by_kind[ComponentKind.CLAIM_TX].in_wallet)  # the taker's

    def test_batched_claims_are_separate_rows_that_reconcile(self) -> None:
        # _claim_swap feeds every claim into the one 'swaps' batch, so several
        # served swaps can share a spending_txid. Each is its own row, and the
        # rows' on-chain shares must add back up to the wallet's delta.
        swaps = {
            'aa' * 32: _served_forward_swap(funding_txid='f_a', spending_txid='batch',
                                            onchain_amount=100_000, lightning_amount=99_000),
            'bb' * 32: _served_forward_swap(funding_txid='f_b', spending_txid='batch',
                                            onchain_amount=50_000, lightning_amount=49_500),
        }
        delta = 100_000 + 50_000 - 200
        wallet = _make_wallet(swaps, history=[
            _onchain('batch', delta, fee=200),
            _ln('aa' * 32, -99_000), _ln('bb' * 32, -49_500),
        ])
        rows = build_served_swap_rows(wallet)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r.batched_with for r in rows}, {1})
        # both claims carry the same redeem script, so they split the fee evenly
        self.assertEqual(sorted(r.return_sat for r in rows), [400, 900])
        onchain_total = sum(
            c.value_sat for r in rows for c in r.components
            if c.kind is ComponentKind.CLAIM_TX and c.in_wallet)
        self.assertEqual(onchain_total, delta)

    def test_a_batch_shared_with_an_own_swap_still_reconciles(self) -> None:
        # The operator's own swap is not a row, but its leg is in the same
        # transaction, so it has to take its share of the fee -- otherwise the
        # served row would silently absorb it.
        swaps = {
            'aa' * 32: _served_forward_swap(funding_txid='f_a', spending_txid='batch',
                                            onchain_amount=100_000, lightning_amount=99_000),
            'bb' * 32: _own_reverse_swap(funding_txid='f_b', spending_txid='batch',
                                         onchain_amount=50_000),
        }
        wallet = _make_wallet(swaps, history=[
            _onchain('batch', 149_800, fee=200),
            _ln('aa' * 32, -99_000),
        ])
        rows = build_served_swap_rows(wallet)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].return_sat, 100_000 - 100 - 99_000)
        self.assertEqual(rows[0].batched_with, 1)

    def test_a_refund_is_named_as_one(self) -> None:
        # We funded a served reverse swap, the taker never claimed, we took the
        # funds back: no preimage was ever revealed.  Both lightning legs are
        # present here, so the return is a complete sum.
        swap = _served_reverse_swap(funding_txid='tx_fund', spending_txid='tx_refund',
                                    preimage=None, onchain_amount=100_000)
        wallet = _make_wallet({'aa' * 32: swap}, history=[
            _onchain('tx_fund', -100_100, fee=100),
            _onchain('tx_refund', 99_900, fee=100),
            _ln('aa' * 32, 0),
            _ln('01' * 32, 0),  # the prepay hash _served_reverse_swap uses
        ])
        rows = build_served_swap_rows(wallet)
        self.assertEqual(len(rows), 1)
        kinds = [c.kind for c in rows[0].components]
        self.assertIn(ComponentKind.REFUND_TX, kinds)
        self.assertNotIn(ComponentKind.CLAIM_TX, kinds)
        # funded -100_000, refunded +100_000, two mining fees paid
        self.assertEqual(rows[0].return_sat, -200)
        self.assertTrue(rows[0].is_complete)

    def test_pending_swaps_are_skipped(self) -> None:
        swaps = {'aa' * 32: _served_forward_swap(funding_txid='f', spending_txid='tx_claim')}
        wallet = _make_wallet(swaps, history=[_onchain('tx_claim', 1)], confirmed=False)
        self.assertEqual(build_served_swap_rows(wallet), [])

    def test_swaps_with_nothing_of_ours_in_the_history_are_skipped(self) -> None:
        swaps = {'aa' * 32: _served_forward_swap(funding_txid='f', spending_txid='tx_claim')}
        self.assertEqual(build_served_swap_rows(_make_wallet(swaps, history=[])), [])

    def test_ledger_rescues_a_misclassified_swap(self) -> None:
        # Fields say "ours", the ledger says we served it: the ledger wins.
        swaps = {'aa' * 32: _own_reverse_swap(funding_txid='f', spending_txid='tx_claim')}
        wallet = _make_wallet(swaps, history=[_onchain('tx_claim', 7)])
        self.assertEqual(build_served_swap_rows(wallet), [])
        record_served_swap(wallet, 'aa' * 32)
        self.assertEqual(len(build_served_swap_rows(wallet)), 1)

    def test_rows_are_oldest_first(self) -> None:
        swaps = {
            'aa' * 32: _served_forward_swap(funding_txid='f_a', spending_txid='tx_a'),
            'bb' * 32: _served_forward_swap(funding_txid='f_b', spending_txid='tx_b'),
        }
        wallet = _make_wallet(swaps, history=[
            _onchain('tx_b', 10, ts=1_700_000_100),
            _onchain('tx_a', 10, ts=1_700_000_000),
        ])
        rows = build_served_swap_rows(wallet)
        self.assertEqual([r.timestamp for r in rows], [1_700_000_000, 1_700_000_100])

    def test_no_lightning(self) -> None:
        wallet = mock.MagicMock()
        wallet.lnworker = None
        self.assertEqual(build_served_swap_rows(wallet), [])

    def test_dict_adapter_keeps_the_fields_the_gui_reads(self) -> None:
        swap = _served_forward_swap(funding_txid='f', spending_txid='tx_claim')
        wallet = _make_wallet({'aa' * 32: swap}, history=[
            _onchain('tx_claim', 99_800, fee=200), _ln('aa' * 32, -99_000)])
        item = get_swap_history(wallet)[0]
        for key in ('label', 'date', 'timestamp', 'return_sat', 'components',
                    'payment_hash', 'batched_with'):
            self.assertIn(key, item)


class SwapLabelTests(unittest.TestCase):
    """The history label has to name the role, not ``is_reverse``."""

    def test_served_forward_swap(self) -> None:
        label = format_swap_label(role=SwapRole.SERVED, taker_is_forward=True,
                                  is_submarine_payment=False, amount_str="0.2 mBTC")
        self.assertIn("Served", label)
        self.assertIn("forward swap", label)
        self.assertIn("0.2 mBTC", label)

    def test_own_swap_says_so(self) -> None:
        label = format_swap_label(role=SwapRole.OWN, taker_is_forward=True,
                                  is_submarine_payment=False, amount_str="0.1 mBTC")
        self.assertIn("My", label)
        self.assertNotIn("Served", label)

    def test_submarine_payment(self) -> None:
        label = format_swap_label(role=SwapRole.OWN, taker_is_forward=False,
                                  is_submarine_payment=True, amount_str="1 mBTC")
        self.assertIn("submarine payment", label)

    def test_unattributed_swap_says_so(self) -> None:
        label = format_swap_label(role=SwapRole.UNKNOWN, taker_is_forward=True,
                                  is_submarine_payment=False, amount_str="1 mBTC")
        self.assertIn("Unattributed", label)
        self.assertNotIn("Served ", label)
        self.assertNotIn("My ", label)


class RelabelHistoryItemsTests(unittest.TestCase):
    """Rewriting swap labels in one copy of ``get_full_history``'s output.

    This is what the "History (Swaps)" tab renders.  Two things are under test
    throughout: that the rows say which side of the swap this wallet was on,
    and that nothing is written to the wallet -- because the wallet is what
    Electrum's own History tab reads, and that tab has to stay upstream's.
    """

    def _wallet(self, swaps: Dict[str, _Swap]) -> Any:
        wallet = _make_wallet(swaps)
        # MagicMock answers every call with a truthy Mock; both of these read as
        # data in the code under test ("the operator labelled this", "here is
        # the formatted amount"), so they have to be pinned rather than left to
        # auto-speccing.
        wallet._get_label.return_value = ''
        wallet.lnworker.swap_manager.config.format_amount_and_units.side_effect = \
            lambda sat: f"{sat} sat"
        return wallet

    @staticmethod
    def _group(group_id: str, label: str, *children: Dict[str, Any]) -> Dict[str, Any]:
        """A history group, keyed the way ``get_full_history`` keys one."""
        return {'group:' + group_id: {
            'txid': '----', 'label': label, 'lightning': False,
            'value': _Value(0), 'children': list(children),
        }}

    def test_a_served_forward_swap_stops_reading_reverse_swap(self) -> None:
        """The row upstream names after *our* copy's is_reverse flag."""
        swap = _served_forward_swap(funding_txid='tx_f', spending_txid='tx_claim')
        wallet = self._wallet({'ab' * 32: swap})
        # is_reverse is True on our copy, so this is upstream's label for it.
        history = self._group('tx_claim', 'Reverse swap 100000 sat',
                              _onchain('tx_claim', 99_800))
        relabel_swap_history_items(wallet, history)
        label = history['group:tx_claim']['label']
        self.assertIn(SERVED_LABEL_MARK, label)
        self.assertIn("Served forward swap", label)
        self.assertNotIn("Reverse swap", label)

    def test_the_operators_own_swap_reads_as_theirs(self) -> None:
        swap = _own_forward_swap(funding_txid='tx_own', spending_txid='tx_own_claim')
        wallet = self._wallet({'cd' * 32: swap})
        history = self._group('tx_own', 'Forward swap 100000 sat',
                              _onchain('tx_own', -100_000))
        relabel_swap_history_items(wallet, history)
        label = history['group:tx_own']['label']
        self.assertIn(OWN_LABEL_MARK, label)
        self.assertIn("My", label)

    def test_an_unattributable_swap_says_so_rather_than_guessing(self) -> None:
        # Fields identical on both sides and no margin to break the tie.
        swap = _Swap(is_reverse=True, onchain_amount=100_000,
                     lightning_amount=100_000, funding_txid='tx_f',
                     spending_txid='tx_claim')
        wallet = self._wallet({'ef' * 32: swap})
        history = self._group('tx_claim', 'Reverse swap 100000 sat',
                              _onchain('tx_claim', 99_800))
        relabel_swap_history_items(wallet, history)
        self.assertIn("Unattributed", history['group:tx_claim']['label'])

    def test_the_legs_inside_a_group_keep_their_own_names(self) -> None:
        """The group is the swap; its children are the components."""
        swap = _served_forward_swap(funding_txid='tx_f', spending_txid='tx_claim')
        wallet = self._wallet({'ab' * 32: swap})
        history = self._group('tx_claim', 'Reverse swap 100000 sat',
                              dict(_onchain('tx_claim', 99_800),
                                   label='Claim transaction'),
                              dict(_ln('ab' * 32, -99_000), label=''))
        relabel_swap_history_items(wallet, history)
        children = history['group:tx_claim']['children']
        self.assertEqual(children[0]['label'], 'Claim transaction')

    def test_a_group_of_one_is_relabelled_where_it_is_actually_shown(self) -> None:
        """``get_full_history`` replaces a single-member group by the member.

        The group label then never reaches the screen, so a swap whose
        lightning leg is missing would keep reading "Claim transaction" -- with
        no swap row anywhere -- unless the leg row itself is rewritten.
        """
        swap = _served_forward_swap(funding_txid='tx_f', spending_txid='tx_claim')
        wallet = self._wallet({'ab' * 32: swap})
        collapsed = dict(_onchain('tx_claim', 99_800), label='Claim transaction')
        history = {'group:tx_claim': collapsed}  # no 'children': collapsed
        relabel_swap_history_items(wallet, history)
        self.assertIn("Served forward swap", history['group:tx_claim']['label'])

    def test_a_label_the_operator_typed_wins_on_a_collapsed_group(self) -> None:
        """Same precedence as everywhere else in Electrum: the user's label."""
        swap = _served_forward_swap(funding_txid='tx_f', spending_txid='tx_claim')
        wallet = self._wallet({'ab' * 32: swap})
        wallet._get_label.side_effect = \
            lambda key: 'my note about this one' if key == 'tx_claim' else ''
        collapsed = dict(_onchain('tx_claim', 99_800), label='my note about this one')
        history = {'group:tx_claim': collapsed}
        relabel_swap_history_items(wallet, history)
        self.assertEqual(history['group:tx_claim']['label'], 'my note about this one')

    def test_an_ungrouped_transaction_is_still_found(self) -> None:
        swap = _served_forward_swap(funding_txid='tx_f', spending_txid='tx_claim')
        wallet = self._wallet({'ab' * 32: swap})
        history = {'tx_claim': dict(_onchain('tx_claim', 99_800), label='')}
        relabel_swap_history_items(wallet, history)
        self.assertIn("Served forward swap", history['tx_claim']['label'])

    def test_nothing_is_written_to_the_wallet(self) -> None:
        """The whole point: Electrum's History tab reads the wallet's labels."""
        swap = _served_forward_swap(funding_txid='tx_f', spending_txid='tx_claim')
        wallet = self._wallet({'ab' * 32: swap})
        history = self._group('tx_claim', 'Reverse swap 100000 sat',
                              _onchain('tx_claim', 99_800))
        relabel_swap_history_items(wallet, history)
        wallet.set_group_label.assert_not_called()
        wallet.set_default_label.assert_not_called()
        wallet.set_label.assert_not_called()

    def test_a_swap_with_no_transaction_of_ours_is_skipped(self) -> None:
        swap = _served_forward_swap(funding_txid=None, spending_txid=None)
        wallet = self._wallet({'ab' * 32: swap})
        history = {'tx_other': dict(_onchain('tx_other', 1), label='coffee')}
        relabel_swap_history_items(wallet, history)
        self.assertEqual(history['tx_other']['label'], 'coffee')

    def test_a_swap_missing_from_the_history_changes_nothing(self) -> None:
        swap = _served_forward_swap(funding_txid='tx_f', spending_txid='tx_gone')
        wallet = self._wallet({'ab' * 32: swap})
        history = {'tx_other': dict(_onchain('tx_other', 1), label='coffee')}
        out = relabel_swap_history_items(wallet, history)
        self.assertIs(out, history)  # mutated in place, and returned
        self.assertEqual(history['tx_other']['label'], 'coffee')

    def test_a_wallet_without_a_swap_manager_is_not_an_error(self) -> None:
        wallet = mock.MagicMock()
        wallet.lnworker = None
        history = {'tx1': {'label': 'coffee'}}
        self.assertIs(relabel_swap_history_items(wallet, history), history)
        self.assertEqual(history['tx1']['label'], 'coffee')


class SummaryLineTests(unittest.TestCase):
    """The strings the operator reads; pure, so testable without PyQt6."""

    def test_no_batched_entries(self) -> None:
        line = format_summary_line(num_swaps=12, net_return="1,234 sat",
                                   swaps_per_day=0.78, num_batched=0)
        self.assertIn("Swaps served: 12", line)
        self.assertIn("1,234 sat", line)
        self.assertIn("0.78/day", line)
        self.assertNotIn("shared transaction", line)

    def test_batched_entries_are_called_out(self) -> None:
        line = format_summary_line(num_swaps=12, net_return="1,234 sat",
                                   swaps_per_day=0.78, num_batched=2)
        self.assertIn("2 settled in a shared transaction", line)

    def test_batched_note_explains_the_shared_transaction(self) -> None:
        note = format_batched_note(batched_with=2)
        self.assertIn("2 other", note)
        # the point of the note: the value *is* this swap's own now
        self.assertIn("this swap's own share", note)


class _RecordingSwapManager:
    """Stand-in exposing the two server-side creation entry points."""

    def __init__(self) -> None:
        self.calls: List[str] = []

    def server_create_swap(self, request: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append('server_create_swap')
        return {'id': 'aa' * 32, 'invoice': 'lnbc1...'}

    def server_create_normal_swap(self, request: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append('server_create_normal_swap')
        return {'id': 'bb' * 32, 'preimageHash': 'bb' * 32}


class RecorderTests(unittest.TestCase):
    """Installing/removing the wrappers around the server creation methods."""

    def _plugin(self, sm: Any, wallet: Any) -> SwapServerGuiPlugin:
        config = mock.MagicMock()
        plugin = SwapServerGuiPlugin(mock.MagicMock(), config, "swapserver_gui")
        plugin.wallet = wallet
        plugin._sm = sm
        return plugin

    def test_records_both_entry_points(self) -> None:
        sm, wallet = _RecordingSwapManager(), _make_wallet({})
        plugin = self._plugin(sm, wallet)
        plugin._install_served_swap_recorder(sm)
        sm.server_create_swap({'type': 'reversesubmarine'})
        sm.server_create_normal_swap({'invoiceAmount': 1})
        self.assertEqual(set(served_swaps_ledger(wallet)), {'aa' * 32, 'bb' * 32})

    def test_response_is_passed_through_untouched(self) -> None:
        sm, wallet = _RecordingSwapManager(), _make_wallet({})
        plugin = self._plugin(sm, wallet)
        plugin._install_served_swap_recorder(sm)
        response = sm.server_create_swap({'type': 'reversesubmarine'})
        self.assertEqual(response, {'id': 'aa' * 32, 'invoice': 'lnbc1...'})

    def test_recording_failure_does_not_fail_the_swap(self) -> None:
        # A swap must never fail because we could not write our bookkeeping;
        # it just falls back to the heuristic in the history.
        sm, wallet = _RecordingSwapManager(), _make_wallet({})
        plugin = self._plugin(sm, wallet)
        plugin._install_served_swap_recorder(sm)
        with mock.patch("swapserver_gui.swapserver_gui.record_served_swap",
                        side_effect=RuntimeError("db is on fire")):
            response = sm.server_create_swap({'type': 'reversesubmarine'})
        self.assertEqual(response['id'], 'aa' * 32)

    def test_install_is_idempotent(self) -> None:
        sm, wallet = _RecordingSwapManager(), _make_wallet({})
        plugin = self._plugin(sm, wallet)
        plugin._install_served_swap_recorder(sm)
        wrapped = sm.server_create_swap
        plugin._install_served_swap_recorder(sm)
        self.assertIs(sm.server_create_swap, wrapped)  # not double-wrapped

    def test_remove_restores_the_original_methods(self) -> None:
        sm, wallet = _RecordingSwapManager(), _make_wallet({})
        plugin = self._plugin(sm, wallet)
        original = sm.server_create_swap
        plugin._install_served_swap_recorder(sm)
        self.assertIsNot(sm.server_create_swap, original)
        plugin._remove_served_swap_recorder(sm)
        self.assertNotIn('server_create_swap', sm.__dict__)  # class method again
        sm.server_create_swap({'type': 'reversesubmarine'})
        self.assertEqual(served_swaps_ledger(wallet), {})  # no longer recording

    def test_remove_targets_the_manager_that_was_wrapped(self) -> None:
        # load_wallet can rebind the plugin to a second wallet while the first
        # wallet's server is still running; unwrapping must follow the manager
        # we actually patched, not whatever _sm points at now.
        sm_a, wallet_a = _RecordingSwapManager(), _make_wallet({})
        plugin = self._plugin(sm_a, wallet_a)
        plugin._install_served_swap_recorder(sm_a)
        sm_b, wallet_b = _RecordingSwapManager(), _make_wallet({})
        plugin._sm, plugin.wallet = sm_b, wallet_b  # a second wallet is opened
        plugin._remove_served_swap_recorder()
        self.assertNotIn('server_create_swap', sm_a.__dict__)

    def test_recording_follows_the_wallet_it_was_installed_for(self) -> None:
        sm, wallet_a = _RecordingSwapManager(), _make_wallet({})
        plugin = self._plugin(sm, wallet_a)
        plugin._install_served_swap_recorder(sm)
        plugin.wallet = _make_wallet({})  # rebound after the server came up
        sm.server_create_swap({'type': 'reversesubmarine'})
        self.assertEqual(set(served_swaps_ledger(wallet_a)), {'aa' * 32})

    def test_remove_is_idempotent(self) -> None:
        sm, wallet = _RecordingSwapManager(), _make_wallet({})
        plugin = self._plugin(sm, wallet)
        plugin._remove_served_swap_recorder(sm)  # never installed
        plugin._install_served_swap_recorder(sm)
        plugin._remove_served_swap_recorder(sm)
        plugin._remove_served_swap_recorder(sm)

    def test_swap_manager_without_the_methods(self) -> None:
        # An older/foreign SwapManager must not break start_server.
        sm, wallet = mock.Mock(spec=[]), _make_wallet({})
        plugin = self._plugin(sm, wallet)
        plugin._install_served_swap_recorder(sm)
        plugin._remove_served_swap_recorder(sm)


class SwapMarginTests(unittest.TestCase):
    """The server is the side that comes out ahead; that is what names it.

    ``_get_send_amount`` adds ``percentage_fee + mining_fee`` and
    ``_get_recv_amount`` subtracts them (electrum/submarine_swaps.py), and both
    are used in one direction only, so the sign of the margin identifies the
    side even when every field is identical.
    """

    def test_served_forward_swap_gains_onchain(self) -> None:
        # taker sends on-chain, we pay lightning: we receive the fee on-chain
        swap = _served_forward_swap(onchain_amount=25_455, lightning_amount=25_000)
        self.assertGreater(swap_margin_sat(swap), 0)

    def test_served_reverse_swap_gains_lightning(self) -> None:
        # taker pays lightning, we send on-chain: we keep the fee in lightning
        swap = _served_reverse_swap(onchain_amount=24_605, lightning_amount=25_000)
        self.assertGreater(swap_margin_sat(swap), 0)

    def test_our_own_reverse_swap_loses(self) -> None:
        swap = _own_reverse_swap(onchain_amount=257_580, lightning_amount=280_000)
        self.assertLess(swap_margin_sat(swap), 0)

    def test_our_own_forward_swap_loses(self) -> None:
        swap = _own_forward_swap(onchain_amount=100_000, lightning_amount=99_000)
        self.assertLess(swap_margin_sat(swap), 0)


class ClassifySwapTests(unittest.TestCase):
    """The one case the fields cannot settle, and what is done about it."""

    def test_ledger_is_authoritative(self) -> None:
        swap = _own_reverse_swap()
        self.assertIs(classify_swap(swap, 'ef' * 32, {'ef' * 32: 1}), SwapRole.SERVED)

    def test_claim_to_output_is_ours_alone(self) -> None:
        # Only reverse_swap, the customer's path, ever sets it -- whatever the
        # amounts happen to say.
        swap = _Swap(is_reverse=True, claim_to_output=('bc1qexample', 1000),
                     onchain_amount=30_000, lightning_amount=25_000)
        self.assertIs(classify_swap(swap, 'aa' * 32, {}), SwapRole.OWN)

    def test_prepay_on_a_reverse_swap_is_ours_alone(self) -> None:
        # create_reverse_swap, the server's path, never sets a prepay hash.
        swap = _own_reverse_swap(onchain_amount=30_000, lightning_amount=25_000)
        self.assertIs(classify_swap(swap, 'aa' * 32, {}), SwapRole.OWN)

    def test_our_reverse_swap_without_a_miner_fee_invoice(self) -> None:
        # THE case looks_server_side got wrong: a reverse swap we made as a
        # customer against a server that sent no minerFeeInvoice records exactly
        # what our own server records for a forward swap it serves.  It used to
        # land in the served table with our cost reported as revenue.
        swap = _Swap(is_reverse=True, prepay_hash=None, claim_to_output=None,
                     onchain_amount=236_300, lightning_amount=280_731)
        self.assertTrue(looks_server_side(swap))  # the old heuristic still says served
        self.assertIs(classify_swap(swap, 'aa' * 32, {}), SwapRole.OWN)

    def test_a_forward_swap_we_served_is_still_served(self) -> None:
        swap = _served_forward_swap(onchain_amount=25_455, lightning_amount=25_000)
        self.assertIs(classify_swap(swap, 'aa' * 32, {}), SwapRole.SERVED)

    def test_equal_amounts_are_unattributable(self) -> None:
        # A server charging neither a percentage nor a mining fee leaves nothing
        # to tell the two sides apart. Say so rather than guess.
        swap = _Swap(is_reverse=True, prepay_hash=None, claim_to_output=None,
                     onchain_amount=25_000, lightning_amount=25_000)
        self.assertIs(classify_swap(swap, 'aa' * 32, {}), SwapRole.UNKNOWN)

    def test_an_unattributable_swap_is_still_shown(self) -> None:
        # It might be revenue; it is kept visible, and kept out of the total.
        self.assertTrue(is_served_swap(
            _Swap(is_reverse=True, onchain_amount=1, lightning_amount=1), 'aa' * 32, {}))

    def test_forward_swap_direction_is_settled_by_the_prepay_flag(self) -> None:
        # create_normal_swap hardcodes prepay=True for the server and
        # request_normal_swap hardcodes prepay=False for the customer, so the
        # margin is never consulted for this direction.
        self.assertIs(classify_swap(_served_reverse_swap(), 'aa' * 32, {}),
                      SwapRole.SERVED)
        self.assertIs(classify_swap(_own_forward_swap(), 'aa' * 32, {}),
                      SwapRole.OWN)


class IncompleteRowTests(unittest.TestCase):
    """A leg of ours that is not in the history has an unknown value, not zero.

    Every case here used to be reported as a plain number: a partial sum that
    reads as a real result, and for a served reverse swap reads as a loss the
    swap never made.
    """

    PH = 'aa' * 32
    PREPAY = '01' * 32   # what _served_reverse_swap uses

    def _served_reverse(self, history: List[Dict[str, Any]]) -> Any:
        swap = _served_reverse_swap(funding_txid='tx_fund', spending_txid='tx_claim',
                                    onchain_amount=24_605, lightning_amount=25_000)
        rows = build_served_swap_rows(_make_wallet({self.PH: swap}, history=history))
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_complete_served_reverse_swap_reports_its_return(self) -> None:
        row = self._served_reverse([
            _onchain('tx_fund', -25_000, fee=395),
            _ln(self.PH, 24_605),
            _ln(self.PREPAY, 395),
        ])
        self.assertTrue(row.is_complete)
        self.assertEqual(row.return_sat, 0)
        self.assertTrue(row.counts_towards_total)

    def test_a_missing_prepayment_withholds_the_return(self) -> None:
        row = self._served_reverse([
            _onchain('tx_fund', -25_000, fee=395),
            _ln(self.PH, 24_605),
        ])
        self.assertFalse(row.is_complete)
        self.assertIsNone(row.return_sat)          # used to be -395
        self.assertFalse(row.counts_towards_total)
        self.assertEqual(len(row.missing_legs), 1)
        self.assertIn("prepayment", row.missing_legs[0].lower())

    def test_a_swap_the_taker_never_paid_withholds_the_return(self) -> None:
        # We funded, the taker never paid, we refunded ourselves: both lightning
        # legs are absent, so both mining fees are all that is left to add up.
        swap = _served_reverse_swap(funding_txid='tx_fund', spending_txid='tx_refund',
                                    preimage=None, onchain_amount=24_545)
        rows = build_served_swap_rows(_make_wallet({self.PH: swap}, history=[
            _onchain('tx_fund', -24_990, fee=445),
            _onchain('tx_refund', 24_100, fee=445),
        ]))
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].return_sat)      # used to be -890
        self.assertEqual(len(rows[0].missing_legs), 2)

    def test_a_claim_with_no_lightning_leg_withholds_the_return(self) -> None:
        # The shape behind the orphan "Claim transaction" rows: we claimed the
        # lockup but there is no lightning payment to set against it, so the
        # on-chain leg alone would read as pure profit.
        swap = _served_forward_swap(funding_txid='tx_fund', spending_txid='tx_claim',
                                    onchain_amount=25_455, lightning_amount=25_000)
        rows = build_served_swap_rows(_make_wallet({self.PH: swap}, history=[
            _onchain('tx_claim', 25_000, fee=455),
        ]))
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].return_sat)      # used to be +25_000
        self.assertEqual(len(rows[0].missing_legs), 1)
        self.assertIn("Lightning payment", rows[0].missing_legs[0])

    def test_the_takers_own_leg_does_not_make_a_row_incomplete(self) -> None:
        # We serve a forward swap: the taker funds the lockup and that funding
        # tx is not in our history by design.  That is not a gap.
        swap = _served_forward_swap(funding_txid='tx_fund', spending_txid='tx_claim',
                                    onchain_amount=25_455, lightning_amount=25_000)
        rows = build_served_swap_rows(_make_wallet({self.PH: swap}, history=[
            _onchain('tx_claim', 25_000, fee=455),
            _ln(self.PH, -24_500),
        ]))
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].is_complete)
        self.assertEqual(rows[0].return_sat, 500)
        funding = [c for c in rows[0].components
                   if c.kind is ComponentKind.FUNDING_TX][0]
        self.assertFalse(funding.in_wallet)
        self.assertFalse(funding.is_missing)


class SummaryExcludesUncountableRowsTests(unittest.TestCase):
    """A total that silently drops rows reads like one that includes them."""

    def _rows(self) -> List[Any]:
        good = _served_reverse_swap(funding_txid='f1', spending_txid='s1',
                                    onchain_amount=24_605, lightning_amount=25_000)
        incomplete = _served_forward_swap(funding_txid='f2', spending_txid='s2',
                                          onchain_amount=25_455, lightning_amount=25_000)
        unknown = _Swap(is_reverse=True, funding_txid='f3', spending_txid='s3',
                        onchain_amount=25_000, lightning_amount=25_000)
        return build_served_swap_rows(_make_wallet(
            {'aa' * 32: good, 'bb' * 32: incomplete, 'cc' * 32: unknown},
            history=[
                _onchain('f1', -25_000, fee=395), _ln('aa' * 32, 24_605),
                _ln('01' * 32, 395),
                _onchain('s2', 25_000, fee=455),
                _onchain('s3', 24_800, fee=200), _ln('cc' * 32, -24_000),
            ]))

    def test_only_complete_served_rows_are_summed(self) -> None:
        rows = self._rows()
        self.assertEqual(len(rows), 3)
        summary = get_swap_summary(rows)
        self.assertEqual(summary['num_swaps'], 3)
        self.assertEqual(summary['num_incomplete'], 1)
        self.assertEqual(summary['num_unattributed'], 1)
        # only the one complete, attributed row: 25_000 in, 25_000 out
        self.assertEqual(summary['overall_return_sat'], 0)

    def test_summary_works_on_the_dict_form_too(self) -> None:
        # get_swap_history hands these to callers outside the GUI.
        dicts = [row._asdict() for row in self._rows()]
        self.assertEqual(get_swap_summary(dicts), get_swap_summary(self._rows()))

    def test_empty(self) -> None:
        summary = get_swap_summary([])
        self.assertEqual(summary['num_incomplete'], 0)
        self.assertEqual(summary['num_unattributed'], 0)

    def test_summary_line_names_what_it_left_out(self) -> None:
        text = format_summary_line(num_swaps=3, net_return="0 sat", swaps_per_day=1.0,
                                   num_batched=0, num_incomplete=1, num_unattributed=1)
        self.assertIn("Not counted", text)
        self.assertIn("incomplete", text)
        self.assertIn("unattributed", text)

    def test_summary_line_stays_quiet_when_everything_counted(self) -> None:
        text = format_summary_line(num_swaps=2, net_return="7 sat", swaps_per_day=1.0,
                                   num_batched=0)
        self.assertNotIn("Not counted", text)

    def test_notes_name_the_missing_legs(self) -> None:
        note = format_incomplete_note(missing_legs=("Lightning payment",))
        self.assertIn("Lightning payment", note)
        self.assertIn("unknown, not zero", note)
        self.assertIn("which side", format_unattributed_note())


class SwapStatusTests(unittest.TestCase):
    """Which height means what.

    The confirmed test has to stay exactly the one the row builder always used
    -- above ``TX_HEIGHT_UNCONFIRMED`` -- or the population of the Swap Server
    tab would change silently.  The rest is about telling the three "not yet"
    heights apart, because only one of them is a problem.
    """

    def _wallet_with_spending_height(self, height: int) -> Any:
        swap = _served_reverse_swap(funding_txid='tx_fund', spending_txid='tx_spend')
        return _make_wallet({'aa' * 32: swap}, heights={'tx_spend': height}), swap

    def test_confirmed_is_final(self) -> None:
        wallet, swap = self._wallet_with_spending_height(100)
        self.assertIs(swap_status(wallet, swap), SwapStatus.FINAL)

    def test_mempool_is_unconfirmed(self) -> None:
        for height in (TX_HEIGHT_UNCONFIRMED, TX_HEIGHT_UNCONF_PARENT):
            with self.subTest(height=height):
                wallet, swap = self._wallet_with_spending_height(height)
                self.assertIs(swap_status(wallet, swap), SwapStatus.UNCONFIRMED)

    def test_never_broadcast_is_local(self) -> None:
        wallet, swap = self._wallet_with_spending_height(TX_HEIGHT_LOCAL)
        self.assertIs(swap_status(wallet, swap), SwapStatus.LOCAL)

    def test_timelocked_is_in_flight(self) -> None:
        wallet, swap = self._wallet_with_spending_height(TX_HEIGHT_FUTURE)
        self.assertIs(swap_status(wallet, swap), SwapStatus.IN_FLIGHT)

    def test_unspent_lockup_is_in_flight(self) -> None:
        swap = _served_reverse_swap(funding_txid='tx_fund', spending_txid=None)
        wallet = _make_wallet({'aa' * 32: swap})
        self.assertIs(swap_status(wallet, swap), SwapStatus.IN_FLIGHT)

    def test_an_unreadable_height_is_not_reported_as_settled(self) -> None:
        # Whatever went wrong, the one answer that must not come back is
        # "final": that would put an unsettled swap into the net return.
        swap = _served_reverse_swap(funding_txid='tx_fund', spending_txid='tx_spend')
        wallet = _make_wallet({'aa' * 32: swap})
        wallet.adb.get_tx_height.side_effect = RuntimeError("no chain")
        self.assertIs(swap_status(wallet, swap), SwapStatus.IN_FLIGHT)


class PendingRowTests(unittest.TestCase):
    """What the Swaps history tab adds, and what the Swap Server tab must not."""

    def _local_swap_wallet(self) -> Any:
        swap = _served_reverse_swap(funding_txid='tx_fund', spending_txid='tx_spend')
        return _make_wallet(
            {'aa' * 32: swap},
            history=[_onchain('tx_fund', -100_000, fee=300),
                     _ln('aa' * 32, 99_000), _ln('01' * 32, 500)],
            heights={'tx_spend': TX_HEIGHT_LOCAL, 'tx_fund': 100},
        )

    def test_the_served_table_still_skips_unsettled_swaps(self) -> None:
        # The whole point of the default: the net return covers finished swaps.
        self.assertEqual(build_served_swap_rows(self._local_swap_wallet()), [])

    def test_the_swaps_tab_shows_a_local_swap_and_says_so(self) -> None:
        rows = build_swap_rows(self._local_swap_wallet(),
                               include_own=True, include_pending=True)
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0].status, SwapStatus.LOCAL)

    def test_an_unsettled_swap_never_counts_towards_the_total(self) -> None:
        # Even a complete, served, unambiguous row: it has not happened yet.
        rows = build_swap_rows(self._local_swap_wallet(),
                               include_own=True, include_pending=True)
        row = rows[0]
        self.assertTrue(row.is_complete)
        self.assertIs(row.role, SwapRole.SERVED)
        self.assertFalse(row.counts_towards_total)
        self.assertEqual(get_swap_summary(rows)['overall_return_sat'], 0)
        self.assertEqual(get_swap_summary(rows)['num_pending'], 1)

    def test_own_swaps_appear_only_when_asked_for(self) -> None:
        swaps = {
            'aa' * 32: _served_reverse_swap(funding_txid='tx_s', spending_txid='tx_sc'),
            'bb' * 32: _own_reverse_swap(funding_txid='tx_o', spending_txid='tx_oc'),
        }
        wallet = _make_wallet(swaps, history=[
            _onchain('tx_s', -100_000, fee=300), _ln('aa' * 32, 99_000),
            _onchain('tx_oc', 99_000, fee=300), _ln('bb' * 32, -100_000),
        ])
        served_only = build_swap_rows(wallet)
        self.assertEqual([r.role for r in served_only], [SwapRole.SERVED])
        both = build_swap_rows(wallet, include_own=True)
        self.assertEqual({r.role for r in both}, {SwapRole.SERVED, SwapRole.OWN})
        # ...and the operator's own swap is still not revenue.
        own = [r for r in both if r.role is SwapRole.OWN][0]
        self.assertFalse(own.counts_towards_total)

    def test_a_swap_with_nothing_in_the_history_yet_still_gets_a_row(self) -> None:
        # An in-flight swap has no leg in the history at all.  Dropping it would
        # hide exactly the rows the Swaps history tab exists to show.
        swap = _served_reverse_swap(funding_txid=None, spending_txid=None)
        wallet = _make_wallet({'aa' * 32: swap})
        record_served_swap(wallet, 'aa' * 32)
        rows = build_swap_rows(wallet, include_own=True, include_pending=True)
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0].status, SwapStatus.IN_FLIGHT)
        # dated from the ledger, which is when our server created it
        self.assertTrue(rows[0].timestamp)
        self.assertTrue(rows[0].date)

    def test_a_swap_with_no_date_at_all_prints_none(self) -> None:
        # No ledger entry either (a swap older than the ledger): rather than
        # invent a date, and sort the row into 1970, the row carries no date.
        swap = _served_reverse_swap(funding_txid=None, spending_txid=None)
        wallet = _make_wallet({'aa' * 32: swap})
        rows = build_swap_rows(wallet, include_own=True, include_pending=True)
        self.assertEqual(rows[0].date, "")

    def test_an_undated_row_does_not_flatten_the_rate(self) -> None:
        day = 86400
        rows = [
            {'return_sat': 10, 'timestamp': 10 * day, 'status': SwapStatus.FINAL},
            {'return_sat': 10, 'timestamp': 12 * day, 'status': SwapStatus.FINAL},
            # the undated one: 1970 would make the span 4383 days
            {'return_sat': None, 'timestamp': 0, 'status': SwapStatus.IN_FLIGHT},
        ]
        summary = get_swap_summary(rows)
        self.assertEqual(summary['swaps_per_day'], 1.5)  # 3 rows over 2 days
        self.assertEqual(summary['overall_return_sat'], 20)
        self.assertEqual(summary['num_pending'], 1)

    def test_pending_is_not_also_reported_as_incomplete(self) -> None:
        # A swap that has not settled is not a swap with a missing leg; the
        # remedies are nothing alike, and counting it twice in the "not counted"
        # note would read as two separate problems.
        rows = [{'return_sat': None, 'timestamp': 1, 'status': SwapStatus.LOCAL,
                 'missing_legs': ('Claim transaction',), 'role': SwapRole.SERVED}]
        summary = get_swap_summary(rows)
        self.assertEqual(summary['num_pending'], 1)
        self.assertEqual(summary['num_incomplete'], 0)

    def test_the_summary_line_counts_pending_out_loud(self) -> None:
        text = format_summary_line(num_swaps=4, net_return="9 sat", swaps_per_day=1.0,
                                   num_batched=0, num_pending=2)
        self.assertIn("Not counted", text)
        self.assertIn("2 not settled yet", text)

    def test_the_summary_line_says_when_own_swaps_are_in_the_count(self) -> None:
        # "Swaps served: 17" over a table counting the operator's own swaps too
        # would be a wrong number, not just a loose phrase.
        text = format_summary_line(num_swaps=17, net_return="9 sat", swaps_per_day=1.0,
                                   num_batched=0, counts_own=True)
        self.assertNotIn("Swaps served:", text)
        self.assertIn("17", text)
        self.assertIn("served and own", text)


if __name__ == "__main__":
    unittest.main()
