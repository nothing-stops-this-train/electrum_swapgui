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
from typing import Any, Dict, List, Optional
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

from swapserver_gui.swapserver_gui import (  # noqa: E402
    ComponentKind, SERVED_SWAPS_DB_KEY, SwapServerGuiPlugin,
    build_served_swap_rows, format_batched_note, format_summary_line,
    format_swap_label, get_swap_history, get_swap_summary, is_served_swap,
    looks_server_side, record_served_swap, served_swaps_ledger,
)
from swapserver_gui.served_swaps import (  # noqa: E402
    FUNDING_OUTPUT_VBYTES, claim_input_vbytes, flatten_history, split_fee,
    relabel_swap_history_groups, taker_did_forward_swap,
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
                 confirmed: bool = True) -> Any:
    """A wallet stand-in with a *real* db, so the ledger is really persisted.

    ``history`` is a flat list of entries; ``get_full_history`` keys them the way
    the wallet does, which the row builder must not depend on.
    """
    wallet = mock.MagicMock()
    wallet.db = WalletDB('', storage=None, upgrade=True)
    wallet.lnworker.swap_manager._swaps = swaps
    # No watcher: lockup values then fall back to the swap's expected amount,
    # which is what they are in every normal case anyway.
    wallet.lnworker.swap_manager.lnwatcher = None
    wallet.adb.get_tx_height.return_value = _TxHeight(100 if confirmed else 0)
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
        # funds back: no preimage was ever revealed.
        swap = _served_reverse_swap(funding_txid='tx_fund', spending_txid='tx_refund',
                                    preimage=None, onchain_amount=100_000)
        wallet = _make_wallet({'aa' * 32: swap}, history=[
            _onchain('tx_fund', -100_100, fee=100),
            _onchain('tx_refund', 99_900, fee=100),
        ])
        rows = build_served_swap_rows(wallet)
        self.assertEqual(len(rows), 1)
        kinds = [c.kind for c in rows[0].components]
        self.assertIn(ComponentKind.REFUND_TX, kinds)
        self.assertNotIn(ComponentKind.CLAIM_TX, kinds)
        # funded -100_000, refunded +100_000, two mining fees paid
        self.assertEqual(rows[0].return_sat, -200)

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
        label = format_swap_label(served=True, taker_is_forward=True,
                                  is_submarine_payment=False, amount_str="0.2 mBTC")
        self.assertIn("Served", label)
        self.assertIn("forward swap", label)
        self.assertIn("0.2 mBTC", label)

    def test_own_swap_says_so(self) -> None:
        label = format_swap_label(served=False, taker_is_forward=True,
                                  is_submarine_payment=False, amount_str="0.1 mBTC")
        self.assertIn("My", label)
        self.assertNotIn("Served", label)

    def test_submarine_payment(self) -> None:
        label = format_swap_label(served=False, taker_is_forward=False,
                                  is_submarine_payment=True, amount_str="1 mBTC")
        self.assertIn("submarine payment", label)

    def test_relabelling_a_group_mapping(self) -> None:
        # This is what wraps SwapManager.get_groups_for_onchain_history, which
        # rewrites its labels into the wallet on every history refresh.
        swap = _served_forward_swap(funding_txid='f', spending_txid='tx_claim')
        wallet = _make_wallet({'aa' * 32: swap})
        sm = wallet.lnworker.swap_manager
        sm.config.format_amount_and_units.return_value = "0.99 mBTC"
        groups = {'tx_claim': {'group_id': 'tx_claim', 'label': 'Claim transaction',
                               'group_label': 'Reverse swap 0.99 mBTC'}}
        relabel_swap_history_groups(wallet, sm, groups)
        entry = groups['tx_claim']
        # upstream calls a *served forward* swap a "Reverse swap", because it
        # names swaps after our own copy's is_reverse flag
        self.assertIn("Served forward swap", entry['group_label'])
        self.assertEqual(entry['label'], 'Claim transaction')  # leg names untouched

    def test_relabelling_leaves_unrelated_groups_alone(self) -> None:
        wallet = _make_wallet({})
        sm = wallet.lnworker.swap_manager
        groups = {'chan': {'group_id': 'chan', 'group_label': 'Open channel x'}}
        relabel_swap_history_groups(wallet, sm, groups)
        self.assertEqual(groups['chan']['group_label'], 'Open channel x')


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


class _GroupBuildingSwapManager:
    """Stand-in exposing the history-group builder the relabeller wraps."""

    def __init__(self, swaps: Dict[str, _Swap]) -> None:
        self._swaps = swaps
        self.calls = 0
        self.config = mock.MagicMock()
        self.config.format_amount_and_units.return_value = "0.99 mBTC"

    def get_groups_for_onchain_history(self) -> Dict[str, Dict[str, Any]]:
        self.calls += 1
        return {'tx_claim': {'group_id': 'tx_claim', 'label': 'Claim transaction',
                             'group_label': 'Reverse swap 0.99 mBTC'}}


class HistoryRelabellerTests(unittest.TestCase):
    """Wrapping ``get_groups_for_onchain_history`` on the swap manager."""

    def _plugin(self, sm: Any, wallet: Any) -> SwapServerGuiPlugin:
        plugin = SwapServerGuiPlugin(mock.MagicMock(), mock.MagicMock(), "swapserver_gui")
        plugin.wallet = wallet
        plugin._sm = sm
        return plugin

    def _sm_and_wallet(self):
        swaps = {'aa' * 32: _served_forward_swap(funding_txid='f', spending_txid='tx_claim')}
        wallet = _make_wallet(swaps)
        return _GroupBuildingSwapManager(swaps), wallet

    def test_labels_are_rewritten(self) -> None:
        sm, wallet = self._sm_and_wallet()
        self._plugin(sm, wallet)._install_history_relabeller(sm)
        groups = sm.get_groups_for_onchain_history()
        self.assertIn("Served forward swap", groups['tx_claim']['group_label'])
        self.assertEqual(sm.calls, 1)  # upstream still ran exactly once

    def test_install_is_idempotent(self) -> None:
        sm, wallet = self._sm_and_wallet()
        plugin = self._plugin(sm, wallet)
        plugin._install_history_relabeller(sm)
        wrapped = sm.get_groups_for_onchain_history
        plugin._install_history_relabeller(sm)
        self.assertIs(sm.get_groups_for_onchain_history, wrapped)

    def test_remove_restores_the_original(self) -> None:
        sm, wallet = self._sm_and_wallet()
        plugin = self._plugin(sm, wallet)
        plugin._install_history_relabeller(sm)
        plugin._remove_history_relabeller(sm)
        self.assertNotIn('get_groups_for_onchain_history', sm.__dict__)
        groups = sm.get_groups_for_onchain_history()
        self.assertEqual(groups['tx_claim']['group_label'], 'Reverse swap 0.99 mBTC')

    def test_remove_is_idempotent(self) -> None:
        sm, wallet = self._sm_and_wallet()
        plugin = self._plugin(sm, wallet)
        plugin._remove_history_relabeller(sm)  # never installed
        plugin._install_history_relabeller(sm)
        plugin._remove_history_relabeller(sm)
        plugin._remove_history_relabeller(sm)

    def test_a_failure_falls_back_to_upstreams_labels(self) -> None:
        # The history must still render if we cannot classify a swap.
        sm, wallet = self._sm_and_wallet()
        self._plugin(sm, wallet)._install_history_relabeller(sm)
        with mock.patch("swapserver_gui.served_swaps.relabel_swap_history_groups",
                        side_effect=RuntimeError("boom")):
            groups = sm.get_groups_for_onchain_history()
        self.assertEqual(groups['tx_claim']['group_label'], 'Reverse swap 0.99 mBTC')

    def test_unbind_wallet_removes_it(self) -> None:
        sm, wallet = self._sm_and_wallet()
        plugin = self._plugin(sm, wallet)
        plugin._install_history_relabeller(sm)
        plugin.unbind_wallet()
        self.assertNotIn('get_groups_for_onchain_history', sm.__dict__)
        self.assertIsNone(plugin.wallet)

    def test_swap_manager_without_the_method(self) -> None:
        sm, wallet = mock.Mock(spec=[]), _make_wallet({})
        plugin = self._plugin(sm, wallet)
        plugin._install_history_relabeller(sm)
        plugin._remove_history_relabeller(sm)


if __name__ == "__main__":
    unittest.main()
