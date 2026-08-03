#!/usr/bin/env python3
"""Unit tests for telling *served* swaps apart from the operator's own swaps.

The Status tab used to report every swap in the wallet as "swaps served",
because ``SwapManager._swaps`` is a single store shared by both roles and
``SwapData`` carries no field naming the side we were on.  These tests pin down
the two mechanisms that replace that guess: the recorded ledger of swaps our
server created (authoritative), and the field heuristic used for swaps that
predate it.

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
    MIXED_ROW_MARKER, SERVED_SWAPS_DB_KEY, SwapServerGuiPlugin, format_mixed_note,
    format_summary_line, get_swap_history, get_swap_summary, is_served_swap,
    looks_server_side, record_served_swap, served_swaps_ledger,
)


class _Swap:
    """The ``SwapData`` fields the classifier and the history reader look at."""

    def __init__(
            self, *,
            is_reverse: bool,
            prepay_hash: Optional[bytes] = None,
            claim_to_output: Optional[Any] = None,
            funding_txid: Optional[str] = None,
            spending_txid: Optional[str] = None,
    ) -> None:
        self.is_reverse = is_reverse
        self.prepay_hash = prepay_hash
        self.claim_to_output = claim_to_output
        self.funding_txid = funding_txid
        self.spending_txid = spending_txid


# The four ways a swap can end up in the wallet, per electrum/submarine_swaps.py.
def _served_reverse_swap(**kw: Any) -> _Swap:
    """Us serving a taker's reverse swap: create_normal_swap -> prepay=True."""
    return _Swap(is_reverse=False, prepay_hash=b'\x01' * 32, **kw)


def _served_forward_swap(**kw: Any) -> _Swap:
    """Us serving a taker's forward swap: create_reverse_swap."""
    return _Swap(is_reverse=True, **kw)


def _own_forward_swap(**kw: Any) -> _Swap:
    """Our own forward swap: request_normal_swap -> prepay=False."""
    return _Swap(is_reverse=False, **kw)


def _own_reverse_swap(**kw: Any) -> _Swap:
    """Our own reverse swap: reverse_swap, with the server's minerFeeInvoice."""
    return _Swap(is_reverse=True, prepay_hash=b'\x02' * 32, **kw)


class _Value:
    def __init__(self, value: int) -> None:
        self.value = value


class _TxHeight:
    def __init__(self, height: int) -> None:
        self._height = height

    def height(self) -> int:
        return self._height


def _make_wallet(swaps: Dict[str, _Swap], *, history: Optional[Dict[str, Any]] = None,
                 confirmed: bool = True) -> Any:
    """A wallet stand-in with a *real* db, so the ledger is really persisted."""
    wallet = mock.MagicMock()
    wallet.db = WalletDB('', storage=None, upgrade=True)
    wallet.lnworker.swap_manager._swaps = swaps
    wallet.adb.get_tx_height.return_value = _TxHeight(100 if confirmed else 0)
    wallet.get_full_history.return_value = history if history is not None else {}
    return wallet


def _group(label: str, value: int, ts: int = 1_700_000_000) -> Dict[str, Any]:
    return {
        'label': label,
        'value': _Value(value),
        'date': datetime.datetime.fromtimestamp(ts),
        'timestamp': ts,
    }


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


class SwapHistoryTests(unittest.TestCase):
    """``get_swap_history`` must report served swaps only."""

    def test_own_swaps_are_excluded(self) -> None:
        swaps = {
            'aa' * 32: _own_forward_swap(funding_txid='tx_own_f', spending_txid='tx_own_f2'),
            'bb' * 32: _own_reverse_swap(funding_txid='tx_own_r', spending_txid='tx_own_r2'),
        }
        wallet = _make_wallet(swaps, history={
            'group:tx_own_f': _group('Forward swap', -205),
            'group:tx_own_r2': _group('Reverse swap', 64),
        })
        self.assertEqual(get_swap_history(wallet), [])

    def test_served_swaps_are_reported(self) -> None:
        swaps = {
            'aa' * 32: _served_reverse_swap(funding_txid='tx_a', spending_txid='tx_a2'),
            'bb' * 32: _served_forward_swap(funding_txid='tx_b', spending_txid='tx_b2'),
        }
        wallet = _make_wallet(swaps, history={
            'group:tx_a': _group('Forward swap', 111, ts=1_700_000_000),
            'group:tx_b2': _group('Reverse swap', 222, ts=1_700_000_100),
        })
        history = get_swap_history(wallet)
        self.assertEqual([h['return_sat'] for h in history], [111, 222])  # oldest first
        self.assertEqual([h['is_mixed'] for h in history], [False, False])
        self.assertEqual([h['num_served_swaps'] for h in history], [1, 1])

    def test_mixed_group_is_reported_and_flagged(self) -> None:
        # A batched claim tx: _claim_swap feeds every is_reverse swap's claim
        # input into the one 'swaps' batch, so a served swap and one of our own
        # can share a spending_txid.
        swaps = {
            'aa' * 32: _served_forward_swap(funding_txid='tx_a', spending_txid='batch'),
            'bb' * 32: _own_reverse_swap(funding_txid='tx_b', spending_txid='batch'),
        }
        wallet = _make_wallet(swaps, history={'group:batch': _group('Reverse swap', 300)})
        history = get_swap_history(wallet)
        self.assertEqual(len(history), 1)
        self.assertTrue(history[0]['is_mixed'])
        self.assertEqual(history[0]['num_served_swaps'], 1)
        self.assertEqual(history[0]['num_own_swaps'], 1)
        self.assertEqual(history[0]['return_sat'], 300)  # value covers both
        self.assertEqual(get_swap_summary(history)['num_mixed'], 1)

    def test_batched_served_swaps_are_one_entry(self) -> None:
        # Two served swaps funded in one batch tx: a single group, not mixed.
        swaps = {
            'aa' * 32: _served_reverse_swap(funding_txid='batch', spending_txid='tx_a2'),
            'bb' * 32: _served_reverse_swap(funding_txid='batch', spending_txid='tx_b2'),
        }
        wallet = _make_wallet(swaps, history={'group:batch': _group('Forward swap', 40)})
        history = get_swap_history(wallet)
        self.assertEqual(len(history), 1)
        self.assertFalse(history[0]['is_mixed'])
        self.assertEqual(history[0]['num_served_swaps'], 2)

    def test_pending_swaps_are_skipped(self) -> None:
        swaps = {'aa' * 32: _served_forward_swap(funding_txid='tx_a', spending_txid='tx_a2')}
        wallet = _make_wallet(swaps, history={'group:tx_a2': _group('Reverse swap', 1)},
                              confirmed=False)
        self.assertEqual(get_swap_history(wallet), [])

    def test_ledger_rescues_a_misclassified_swap(self) -> None:
        # Fields say "ours", the ledger says we served it: the ledger wins.
        swaps = {'aa' * 32: _own_reverse_swap(funding_txid='tx_a', spending_txid='tx_a2')}
        wallet = _make_wallet(swaps, history={'group:tx_a2': _group('Reverse swap', 7)})
        self.assertEqual(get_swap_history(wallet), [])
        record_served_swap(wallet, 'aa' * 32)
        self.assertEqual(len(get_swap_history(wallet)), 1)

    def test_no_lightning(self) -> None:
        wallet = mock.MagicMock()
        wallet.lnworker = None
        self.assertEqual(get_swap_history(wallet), [])


class SummaryLineTests(unittest.TestCase):
    """The strings the operator reads; pure, so testable without PyQt6."""

    def test_no_batched_entries(self) -> None:
        line = format_summary_line(num_swaps=12, net_return="1,234 sat",
                                   swaps_per_day=0.78, num_mixed=0)
        self.assertIn("Swaps served: 12", line)
        self.assertIn("1,234 sat", line)
        self.assertIn("0.78/day", line)
        self.assertNotIn("batched", line)

    def test_batched_entries_are_called_out(self) -> None:
        line = format_summary_line(num_swaps=12, net_return="1,234 sat",
                                   swaps_per_day=0.78, num_mixed=2)
        self.assertIn("2 batched with own swaps", line)

    def test_mixed_note_names_both_counts(self) -> None:
        note = format_mixed_note(num_served=2, num_own=1)
        self.assertIn("2 swap(s) served", note)
        self.assertIn("1 swap(s) this wallet initiated", note)
        # the point of the note: the number shown is not purely server revenue
        self.assertIn("not purely server revenue", note)

    def test_row_marker_is_short_enough_for_a_column(self) -> None:
        self.assertLess(len(MIXED_ROW_MARKER), 20)


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


if __name__ == "__main__":
    unittest.main()
