#!/usr/bin/env python3
"""Unit tests for the diagnostics export.

The export exists because Electrum's own history export is not usable for
debugging a swap: ``Abstract_Wallet.export_history_to_file`` drops every entry
whose timestamp is unset, which is exactly the unconfirmed transactions and the
local (never broadcast) ones a stranded swap batch leaves behind.  So the
properties under test are mostly about what is *not* left out -- and, on the
other side, about the two things that must never be in the file.

No PyQt6 and no network: ``history_export`` is deliberately Qt-free.

Run with:  python3 -m pytest tests/test_history_export.py
"""
import json
import os
import sys
import tempfile
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

from electrum.address_synchronizer import TX_HEIGHT_LOCAL, TX_HEIGHT_UNCONFIRMED  # noqa: E402
from electrum.util import Satoshis, TxMinedInfo  # noqa: E402
from electrum.wallet_db import WalletDB  # noqa: E402

from swapserver_gui.history_export import (  # noqa: E402
    REDACTED_SWAP_FIELDS, SCHEMA_VERSION, ExportError, build_history_export,
    encode_history_export, write_history_export,
)
from swapserver_gui.served_swaps import SERVED_SWAPS_DB_KEY  # noqa: E402


PRIVKEY = b'\x11' * 32
PREIMAGE = b'\x22' * 32


class _Swap:
    """The ``SwapData`` fields the export reads, secrets included."""

    def __init__(self, **kw: Any) -> None:
        self.is_reverse = kw.get('is_reverse', True)
        self.locktime = kw.get('locktime', 800_000)
        self.onchain_amount = kw.get('onchain_amount', 100_000)
        self.lightning_amount = kw.get('lightning_amount', 99_000)
        self.lockup_address = kw.get('lockup_address', 'bc1qlockup')
        self.funding_txid = kw.get('funding_txid', 'tx_fund')
        self.spending_txid = kw.get('spending_txid', 'tx_claim')
        self.is_redeemed = kw.get('is_redeemed', True)
        self.claim_to_output = kw.get('claim_to_output', None)
        self.prepay_hash = kw.get('prepay_hash', None)
        self.redeem_script = kw.get('redeem_script', b'\x21' * 110)
        self.privkey = kw.get('privkey', PRIVKEY)
        self.preimage = kw.get('preimage', PREIMAGE)


class _Prevout:
    def __init__(self, text: str) -> None:
        self._text = text

    def to_str(self) -> str:
        return self._text


class _TxIn:
    def __init__(self, prevout: str) -> None:
        self.prevout = _Prevout(prevout)


class _TxOut:
    def __init__(self, address: str, value: int) -> None:
        self.address = address
        self.value = value


class _Tx:
    def __init__(self, txid: str, *, outputs: Optional[List[_TxOut]] = None) -> None:
        self._txid = txid
        self.version = 2
        self.locktime = 0
        self._inputs = [_TxIn(f'{txid}_prev:0')]
        self._outputs = outputs or [_TxOut('bc1qdest', 99_800)]

    def inputs(self) -> List[_TxIn]:
        return self._inputs

    def outputs(self) -> List[_TxOut]:
        return self._outputs

    def serialize(self) -> str:
        return '02000000' + self._txid


def _onchain(txid: str, value: int, *, ts: Optional[int] = 1_700_000_000,
             height: int = 100) -> Dict[str, Any]:
    return {
        'txid': txid, 'lightning': False, 'label': '',
        'value': Satoshis(value), 'bc_value': Satoshis(value),
        'ln_value': Satoshis(0), 'fee_sat': 200,
        'timestamp': ts, 'height': height, 'confirmations': 3 if height > 0 else 0,
    }


def _make_wallet(*, swaps: Optional[Dict[str, _Swap]] = None,
                 history: Optional[Dict[str, Any]] = None,
                 txs: Optional[Dict[str, int]] = None) -> Any:
    """A wallet stand-in with a real db, so the ledger is really read.

    ``txs`` maps txid -> height, using the ``TX_HEIGHT_*`` constants for the
    ones that never confirmed.
    """
    txs = {'tx_claim': 100} if txs is None else txs
    wallet = mock.MagicMock()
    wallet.db = WalletDB('', storage=None, upgrade=True)
    wallet.basename.return_value = 'test_wallet'
    wallet.wallet_type = 'standard'
    wallet.txin_type = 'p2wpkh'
    wallet.has_lightning.return_value = True
    wallet.get_addresses.return_value = ['bc1qone', 'bc1qtwo']
    wallet.get_all_labels.return_value = {'tx_claim': 'a note'}
    wallet._default_labels = {'group:tx_claim': 'Reverse swap 100000 sat'}
    wallet.get_label_for_txid.side_effect = lambda txid: ''
    wallet.is_mine.return_value = True
    wallet.adb.is_mine.return_value = True
    wallet.adb.get_local_height.return_value = 900_000
    wallet.adb.db.list_transactions.return_value = list(txs)
    wallet.adb.get_tx_height.side_effect = lambda txid: TxMinedInfo(
        _height=txs.get(txid, 0), conf=3 if txs.get(txid, 0) > 0 else 0,
        timestamp=1_700_000_000 if txs.get(txid, 0) > 0 else None)
    wallet.adb.get_transaction.side_effect = lambda txid: _Tx(txid)
    wallet.adb.get_tx_fee.side_effect = lambda txid: 200
    wallet.get_full_history.return_value = history if history is not None else {
        'tx_claim': _onchain('tx_claim', 99_800)}
    wallet.lnworker.swap_manager._swaps = swaps if swaps is not None else {}
    wallet.lnworker.swap_manager.lnwatcher = None
    wallet.lnworker.get_lightning_history.return_value = {}
    wallet.lnworker.channels = {}
    wallet.lnworker.channel_backups = {}
    return wallet


class MetadataTests(unittest.TestCase):

    def test_the_document_says_what_it_is(self) -> None:
        data = build_history_export(_make_wallet(), timestamp=1_700_000_000)
        self.assertEqual(data['schema_version'], SCHEMA_VERSION)
        self.assertEqual(data['kind'], 'swapserver_gui.diagnostics')
        self.assertEqual(data['exported_at'], 1_700_000_000)
        self.assertEqual(data['exported_at_utc'], '2023-11-14T22:13:20Z')
        self.assertTrue(data['electrum_version'])
        self.assertEqual(data['wallet']['basename'], 'test_wallet')
        self.assertEqual(data['wallet']['local_height'], 900_000)

    def test_the_server_state_is_carried_verbatim(self) -> None:
        status = {'running': True, 'nostr_npub': 'npub1xyz'}
        data = build_history_export(_make_wallet(), plugin_status=status)
        self.assertEqual(data['server'], status)

    def test_no_wallet_is_an_error_not_an_empty_file(self) -> None:
        with self.assertRaises(ExportError):
            build_history_export(None)


class NothingIsDroppedTests(unittest.TestCase):
    """The reason this exists: upstream's export drops exactly these rows."""

    def test_a_local_transaction_is_exported_and_named_as_one(self) -> None:
        wallet = _make_wallet(txs={'tx_batch': TX_HEIGHT_LOCAL, 'tx_claim': 100})
        data = build_history_export(wallet)
        by_txid = {tx['txid']: tx for tx in data['transactions']}
        self.assertIn('tx_batch', by_txid)
        self.assertTrue(by_txid['tx_batch']['is_local'])
        self.assertFalse(by_txid['tx_batch']['is_confirmed'])
        self.assertFalse(by_txid['tx_claim']['is_local'])

    def test_an_entry_with_no_timestamp_survives(self) -> None:
        """``export_history_to_file`` filters these out; this must not."""
        wallet = _make_wallet(history={
            'tx_batch': _onchain('tx_batch', -1000, ts=None, height=TX_HEIGHT_LOCAL),
            'tx_claim': _onchain('tx_claim', 99_800)})
        data = build_history_export(wallet)
        self.assertEqual(len(data['history']), 2)
        self.assertIn(None, [item['timestamp'] for item in data['history']])

    def test_a_group_keeps_its_children(self) -> None:
        wallet = _make_wallet(history={'group:tx_claim': {
            'txid': '----', 'label': 'Reverse swap', 'value': Satoshis(0),
            'children': [_onchain('tx_claim', 99_800)]}})
        data = build_history_export(wallet)
        self.assertEqual(len(data['history'][0]['children']), 1)

    def test_the_raw_hex_of_what_cannot_be_looked_up_is_always_there(self) -> None:
        wallet = _make_wallet(txs={'tx_batch': TX_HEIGHT_LOCAL,
                                   'tx_pending': TX_HEIGHT_UNCONFIRMED,
                                   'tx_claim': 100})
        data = build_history_export(wallet)
        by_txid = {tx['txid']: tx for tx in data['transactions']}
        self.assertIn('raw', by_txid['tx_batch'])
        self.assertIn('raw', by_txid['tx_pending'])
        # A confirmed transaction is on the chain; its hex is opt-in because it
        # is what makes the file big.
        self.assertNotIn('raw', by_txid['tx_claim'])

    def test_confirmed_hex_can_be_asked_for(self) -> None:
        wallet = _make_wallet(txs={'tx_claim': 100})
        data = build_history_export(wallet, include_raw_confirmed=True)
        self.assertIn('raw', data['transactions'][0])

    def test_a_transaction_carries_its_inputs_and_outputs(self) -> None:
        data = build_history_export(_make_wallet())
        tx = data['transactions'][0]
        self.assertEqual(tx['inputs'][0]['prevout'], 'tx_claim_prev:0')
        self.assertEqual(tx['outputs'][0]['value_sat'], 99_800)
        self.assertEqual(tx['outputs'][0]['address'], 'bc1qdest')

    def test_a_transaction_the_wallet_cannot_produce_is_still_listed(self) -> None:
        wallet = _make_wallet()
        wallet.adb.get_transaction.side_effect = lambda txid: None
        tx = build_history_export(wallet)['transactions'][0]
        self.assertEqual(tx['txid'], 'tx_claim')
        self.assertIsNone(tx['tx'])

    def test_both_kinds_of_label_are_exported_separately(self) -> None:
        data = build_history_export(_make_wallet())
        self.assertEqual(data['labels']['user'], {'tx_claim': 'a note'})
        self.assertIn('group:tx_claim', data['labels']['default'])


class SwapSectionTests(unittest.TestCase):

    def test_a_swap_is_exported_with_its_legs(self) -> None:
        wallet = _make_wallet(swaps={'ab' * 32: _Swap()})
        record = build_history_export(wallet)['swaps'][0]
        self.assertEqual(record['payment_hash'], 'ab' * 32)
        self.assertEqual(record['funding_txid'], 'tx_fund')
        self.assertEqual(record['spending_txid'], 'tx_claim')
        self.assertEqual(record['lockup_address'], 'bc1qlockup')
        self.assertEqual(record['locktime'], 800_000)
        # Hex, not bytes: the redeem script is public (it is in the witness of
        # every claim), and json has no bytes.
        self.assertEqual(record['redeem_script'], (b'\x21' * 110).hex())

    def test_the_role_and_the_status_are_resolved(self) -> None:
        wallet = _make_wallet(swaps={'ab' * 32: _Swap()})
        record = build_history_export(wallet)['swaps'][0]
        # A swap we served a taker's forward swap with: is_reverse, no prepay
        # hash, no claim_to_output.
        self.assertEqual(record['role'], 'served')
        self.assertEqual(record['status'], 'final')

    def test_the_ledger_is_exported_with_the_key_it_lives_under(self) -> None:
        wallet = _make_wallet(swaps={'ab' * 32: _Swap()})
        wallet.db.get_dict(SERVED_SWAPS_DB_KEY)['ab' * 32] = 1_700_000_000
        data = build_history_export(wallet)
        self.assertEqual(data['served_swaps_ledger']['db_key'], SERVED_SWAPS_DB_KEY)
        self.assertEqual(data['served_swaps_ledger']['entries'],
                         {'ab' * 32: 1_700_000_000})
        self.assertEqual(data['swaps'][0]['served_at'], 1_700_000_000)

    def test_the_plugins_own_rows_are_json_shaped_not_named_tuples(self) -> None:
        """A named tuple encodes as an array, which would lose every name."""
        wallet = _make_wallet(
            swaps={'ab' * 32: _Swap()},
            history={'tx_claim': _onchain('tx_claim', 99_800),
                     'ab' * 32: {'payment_hash': 'ab' * 32, 'lightning': True,
                                 'value': Satoshis(-99_000),
                                 'ln_value': Satoshis(-99_000),
                                 'bc_value': Satoshis(0),
                                 'timestamp': 1_700_000_000}})
        rows = build_history_export(wallet)['swap_rows']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['payment_hash'], 'ab' * 32)
        self.assertEqual(rows[0]['role'], 'served')
        self.assertEqual(rows[0]['status'], 'final')
        self.assertIsInstance(rows[0]['counts_towards_total'], bool)
        self.assertIsInstance(rows[0]['components'][0], dict)
        self.assertIsInstance(rows[0]['components'][0]['kind'], str)


class RedactionTests(unittest.TestCase):
    """What a diagnostics file must never carry out of the wallet."""

    def test_the_secrets_are_replaced_by_fingerprints(self) -> None:
        import hashlib
        wallet = _make_wallet(swaps={'ab' * 32: _Swap()})
        record = build_history_export(wallet)['swaps'][0]
        for field in REDACTED_SWAP_FIELDS:
            self.assertNotIn(field, record)
        self.assertEqual(record['privkey_sha256'],
                         hashlib.sha256(PRIVKEY).hexdigest()[:16])
        self.assertEqual(record['preimage_sha256'],
                         hashlib.sha256(PREIMAGE).hexdigest()[:16])

    def test_no_secret_reaches_the_file(self) -> None:
        """Asserted on the encoded text, not the dict: that is what is written."""
        wallet = _make_wallet(swaps={'ab' * 32: _Swap()})
        text = encode_history_export(build_history_export(wallet))
        self.assertNotIn(PRIVKEY.hex(), text)
        self.assertNotIn(PREIMAGE.hex(), text)

    def test_a_swap_with_no_secrets_recorded_says_null(self) -> None:
        wallet = _make_wallet(swaps={'ab' * 32: _Swap(privkey=None, preimage=None)})
        record = build_history_export(wallet)['swaps'][0]
        self.assertIsNone(record['privkey_sha256'])
        self.assertIsNone(record['preimage_sha256'])

    def test_the_file_says_what_it_left_out(self) -> None:
        data = build_history_export(_make_wallet())
        self.assertEqual(data['redacted']['swap_fields'], list(REDACTED_SWAP_FIELDS))


class ResilienceTests(unittest.TestCase):
    """A diagnostics dump is asked for when something is already wrong."""

    def test_a_section_that_raises_costs_that_section_only(self) -> None:
        wallet = _make_wallet(swaps={'ab' * 32: _Swap()})
        wallet.lnworker.get_lightning_history.side_effect = RuntimeError("no lnworker")
        data = build_history_export(wallet)
        self.assertEqual(data['lightning_history'], [])
        self.assertEqual(len(data['swaps']), 1)         # the rest is still there
        self.assertEqual(len(data['transactions']), 1)

    def test_a_wallet_with_no_lightning_exports_the_on_chain_half(self) -> None:
        wallet = _make_wallet()
        wallet.lnworker = None
        data = build_history_export(wallet)
        self.assertEqual(data['swaps'], [])
        self.assertEqual(data['channels'], [])
        self.assertEqual(len(data['transactions']), 1)

    def test_a_history_that_cannot_be_built_leaves_the_rest(self) -> None:
        wallet = _make_wallet()
        wallet.get_full_history.side_effect = RuntimeError("wallet went away")
        data = build_history_export(wallet)
        self.assertEqual(data['history'], [])
        self.assertEqual(len(data['transactions']), 1)

    def test_an_unencodable_value_does_not_take_the_file_with_it(self) -> None:
        """``util.json_encode`` would return repr() for the *whole* document."""
        text = encode_history_export({'ok': 1, 'weird': object()})
        parsed = json.loads(text)  # still json
        self.assertEqual(parsed['ok'], 1)
        self.assertIn('object object', parsed['weird'])

    def test_a_to_json_that_never_bottoms_out_is_cut_short(self) -> None:
        """Electrum's encoder delegates to any ``to_json`` it finds.

        One that keeps handing back values json cannot encode would recurse
        until the interpreter gives up -- which is what a MagicMock does, and
        what any object built out of them in a half-loaded wallet would do.
        """
        class _Bottomless:
            def to_json(self):
                return _Bottomless()

        parsed = json.loads(encode_history_export({'x': _Bottomless()}))
        self.assertIn('_Bottomless', parsed['x'])

    def test_an_enum_is_exported_by_value(self) -> None:
        from swapserver_gui.served_swaps import SwapRole
        parsed = json.loads(encode_history_export({'role': SwapRole.SERVED}))
        self.assertEqual(parsed['role'], 'served')


class WriteTests(unittest.TestCase):

    def _write(self, wallet: Any, **kw: Any) -> Dict[str, Any]:
        path = os.path.join(tempfile.mkdtemp(), 'diag.json')
        return write_history_export(wallet, path, **kw)

    def test_it_writes_json_and_counts_what_went_in(self) -> None:
        wallet = _make_wallet(swaps={'ab' * 32: _Swap()},
                              txs={'tx_batch': TX_HEIGHT_LOCAL, 'tx_claim': 100})
        summary = self._write(wallet)
        self.assertEqual(summary['num_transactions'], 2)
        self.assertEqual(summary['num_local_transactions'], 1)
        self.assertEqual(summary['num_swaps'], 1)
        self.assertGreater(summary['bytes'], 0)
        with open(summary['path'], encoding='utf-8') as f:
            parsed = json.load(f)
        self.assertEqual(parsed['kind'], 'swapserver_gui.diagnostics')
        self.assertEqual(len(parsed['swaps']), 1)

    def test_an_unwritable_path_is_reported_not_swallowed(self) -> None:
        with self.assertRaises(ExportError):
            write_history_export(_make_wallet(), '/nonexistent-dir/diag.json')


if __name__ == '__main__':
    unittest.main()
