#!/usr/bin/env python
#
# swapserver_gui - a Qt GUI plugin for Electrum's submarine swap server.
# This file is released into the public domain (The Unlicense); see LICENSE.
#
# The diagnostics export: everything about this wallet's transaction history
# that someone debugging a swap could need, in one json file.
#
# Electrum already exports a history (History tab -> Export), and this is
# deliberately not that.  ``Abstract_Wallet.export_history_to_file`` drops every
# entry whose timestamp is unset -- "remove unconfirmed/local tx as their
# ordering is not deterministic, and they don't seem useful for a wallet export"
# (electrum/wallet.py) -- which is exactly backwards for debugging: a swap that
# is stuck is stuck *because* of a transaction that never confirmed, and a
# stranded local batch (see :class:`served_swaps.SwapStatus.LOCAL`) has no
# timestamp at all, so upstream's export is silent about the one row the
# operator needs to send someone.  Nothing is dropped here.
#
# What this adds on top of the same history:
#
#   * every transaction the wallet holds, whether or not it reached the
#     history: height, confirmations, inputs, outputs, and -- for the ones that
#     are not confirmed, so cannot be fetched from a block explorer later --
#     the raw hex;
#   * the swap records themselves, which is where a swap's two legs, its
#     lockup address and its timelock live;
#   * this plugin's own view of the same swaps (:func:`build_swap_rows`), so a
#     disagreement between what the plugin says and what the wallet holds is
#     visible in one file rather than needing two;
#   * the labels, both the operator's and the generated ones, because a row
#     reading the wrong thing is a label question;
#   * the server/announcement state at the moment of export.
#
# Secrets are not transaction history and are not exported: see
# :data:`REDACTED_SWAP_FIELDS`.
#
# No PyQt6 here, on purpose -- same reasoning as served_swaps.py: the export can
# then be built and asserted on in a plain unit test.

import hashlib
import importlib
import json
import time
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from electrum import constants
from electrum.address_synchronizer import TX_HEIGHT_LOCAL
from electrum.lnutil import LOCAL, REMOTE
from electrum.util import MyEncoder
from electrum.version import ELECTRUM_VERSION

from .served_swaps import (SERVED_SWAPS_DB_KEY, SwapRole, build_swap_rows,
                           classify_swap, served_swaps_ledger, swap_status)

if TYPE_CHECKING:
    from electrum.wallet import Abstract_Wallet

_version = importlib.import_module('._version', __package__)


#: Bumped when the shape of the document changes, so a file can be read by
#: something that was written against an older plugin.
SCHEMA_VERSION = 1

#: ``SwapData`` fields that would let the holder of the file move money: the
#: key that spends a swap's lockup output, and the preimage that claims the
#: lightning payment of a swap that has not settled yet.  Neither says anything
#: about what a swap *did*, which is what this export is for, so both are
#: replaced by a fingerprint -- enough to tell two swaps apart, or to confirm
#: that two records hold the same secret, without carrying the secret itself.
#:
#: Note this covers the swap records only.  The lightning history is exported
#: exactly as Electrum's own history export writes it, preimages of settled
#: payments included (electrum/util.py: ``LightningHistoryItem.to_dict``).
REDACTED_SWAP_FIELDS = ('privkey', 'preimage')

#: Plain ``SwapData`` fields worth carrying over verbatim.
_SWAP_FIELDS = ('is_reverse', 'locktime', 'onchain_amount', 'lightning_amount',
                'lockup_address', 'funding_txid', 'spending_txid',
                'is_redeemed', 'claim_to_output')


class ExportError(Exception):
    """The export could not be written.  Carries a message fit for a dialog."""


def _fingerprint(value: Any) -> Optional[str]:
    """A stable, non-reversible stand-in for a secret.

    Truncated to 16 hex characters: long enough that two distinct secrets will
    not collide in one wallet, short enough that nobody mistakes it for the
    thing itself.
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = bytes.fromhex(value)
        except ValueError:
            value = value.encode('utf-8')
    if not isinstance(value, bytes):
        return None
    return hashlib.sha256(value).hexdigest()[:16]


def _hex(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value.hex() if isinstance(value, bytes) else str(value)


def _safe(fn: Callable[[], Any], default: Any = None) -> Any:
    """Run ``fn``, and let a failure cost one field rather than the export.

    A diagnostics dump is asked for when something is already wrong, which is
    the worst moment for it to raise: the wallet may be half-loaded, offline,
    or holding the very record that cannot be read.  Every section is gathered
    through here, and the ones that fail say ``null`` instead of taking the
    file with them.
    """
    try:
        return fn()
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def swap_record(payment_hash_hex: str, swap: Any, *,
                wallet: Optional['Abstract_Wallet'] = None,
                ledger: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """One ``SwapData`` as json, minus the secrets."""
    record: Dict[str, Any] = {'payment_hash': payment_hash_hex}
    for field in _SWAP_FIELDS:
        record[field] = _safe(lambda f=field: getattr(swap, f, None))
    record['prepay_hash'] = _hex(getattr(swap, 'prepay_hash', None))
    record['redeem_script'] = _hex(getattr(swap, 'redeem_script', None))
    for field in REDACTED_SWAP_FIELDS:
        record[f'{field}_sha256'] = _fingerprint(getattr(swap, field, None))
    if ledger is not None:
        record['role'] = _safe(
            lambda: classify_swap(swap, payment_hash_hex, ledger).value,
            SwapRole.UNKNOWN.value)
        record['served_at'] = ledger.get(payment_hash_hex)
    if wallet is not None:
        record['status'] = _safe(lambda: swap_status(wallet, swap).value)
    return record


def transaction_record(wallet: 'Abstract_Wallet', txid: str, *,
                       include_raw: bool = False) -> Dict[str, Any]:
    """One transaction the wallet holds, history entry or not.

    ``include_raw`` forces the hex in.  It is written regardless for anything
    not confirmed, because that is the case where it cannot be looked up
    anywhere else: a local transaction was never broadcast, so this file is the
    only copy outside the wallet.
    """
    record: Dict[str, Any] = {'txid': txid}
    info = _safe(lambda: wallet.adb.get_tx_height(txid))
    height = _safe(lambda: info.height())
    record.update({
        'height': height,
        'confirmations': getattr(info, 'conf', None),
        'timestamp': getattr(info, 'timestamp', None),
        'txpos_in_block': getattr(info, 'txpos', None),
        'wanted_height': getattr(info, 'wanted_height', None),
        # Spelled out rather than left for the reader to derive from the
        # height: "is this the batch that never went out?" is the question
        # this export exists to answer.
        'is_local': height == TX_HEIGHT_LOCAL,
        'is_confirmed': bool(height is not None and height > 0),
        'fee_sat': _safe(lambda: wallet.adb.get_tx_fee(txid)),
        'label': _safe(lambda: wallet.get_label_for_txid(txid), ''),
    })
    tx = _safe(lambda: wallet.adb.get_transaction(txid))
    if tx is None:
        record['tx'] = None
        return record
    record['version'] = _safe(lambda: tx.version)
    record['locktime'] = _safe(lambda: tx.locktime)
    record['inputs'] = _safe(lambda: [
        {'prevout': txin.prevout.to_str(),
         'is_mine': _safe(lambda: wallet.adb.is_mine(
             wallet.adb.get_txin_address(txin)), None)}
        for txin in tx.inputs()], [])
    record['outputs'] = _safe(lambda: [
        {'address': txout.address, 'value_sat': txout.value,
         'is_mine': _safe(lambda: wallet.is_mine(txout.address), None)}
        for txout in tx.outputs()], [])
    if include_raw or not record['is_confirmed']:
        record['raw'] = _safe(lambda: tx.serialize())
    return record


def channel_record(chan: Any) -> Dict[str, Any]:
    """A lightning channel, to the extent it explains a swap's payment leg."""
    return {
        'channel_id': _safe(lambda: chan.channel_id.hex()),
        'short_channel_id': _safe(lambda: str(chan.short_channel_id)),
        'node_id': _safe(lambda: chan.node_id.hex()),
        'state': _safe(lambda: chan.get_state().name),
        'funding_outpoint': _safe(lambda: chan.funding_outpoint.to_str()),
        'capacity_sat': _safe(lambda: chan.get_capacity()),
        'can_send_sat': _safe(lambda: chan.available_to_spend(LOCAL) // 1000),
        'can_receive_sat': _safe(lambda: chan.available_to_spend(REMOTE) // 1000),
        'is_backup': _safe(lambda: chan.is_backup(), False),
    }


def build_history_export(
        wallet: 'Abstract_Wallet',
        *,
        plugin_status: Optional[Dict[str, Any]] = None,
        include_raw_confirmed: bool = False,
        timestamp: Optional[int] = None,
) -> Dict[str, Any]:
    """The whole diagnostics document, as plain json-able data.

    Never raises for a section it cannot read; see :func:`_safe`.  The one
    thing it does not guard is ``wallet`` itself being ``None``, which is a
    caller bug rather than a wallet in a bad state.
    """
    if wallet is None:
        raise ExportError("no wallet to export")
    now = int(time.time()) if timestamp is None else int(timestamp)
    lnworker = getattr(wallet, 'lnworker', None)
    sm = getattr(lnworker, 'swap_manager', None) if lnworker else None
    ledger = _safe(lambda: dict(served_swaps_ledger(wallet)), {}) or {}

    # The history, exactly as the History tab builds it -- groups, children,
    # lightning rows and all -- with nothing filtered out.
    history = _safe(lambda: wallet.get_full_history(fx=None), {}) or {}

    swaps = _safe(lambda: list(sm._swaps.items()), []) if sm is not None else []

    return {
        'schema_version': SCHEMA_VERSION,
        'kind': 'swapserver_gui.diagnostics',
        'exported_at': now,
        'exported_at_utc': _safe(
            lambda: time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))),
        'plugin_version': _safe(lambda: _version.__version__),
        'electrum_version': ELECTRUM_VERSION,
        'network': _safe(lambda: constants.net.NET_NAME),
        'redacted': {
            'swap_fields': list(REDACTED_SWAP_FIELDS),
            'note': ("Swap secrets are exported as sha256 fingerprints only; "
                     "the file is still a full record of this wallet's "
                     "addresses, labels and transactions."),
        },
        'wallet': {
            'basename': _safe(lambda: wallet.basename()),
            'type': _safe(lambda: wallet.wallet_type),
            'txin_type': _safe(lambda: wallet.txin_type),
            'has_lightning': _safe(lambda: wallet.has_lightning(), False),
            'local_height': _safe(lambda: wallet.adb.get_local_height()),
            'num_addresses': _safe(lambda: len(wallet.get_addresses())),
        },
        'server': plugin_status if plugin_status is not None else None,
        'history': _safe(lambda: list(history.values()), []),
        'transactions': _safe(lambda: [
            transaction_record(wallet, txid, include_raw=include_raw_confirmed)
            for txid in wallet.adb.db.list_transactions()], []),
        'lightning_history': _safe(
            lambda: [item.to_dict() for item in
                     lnworker.get_lightning_history().values()], []
        ) if lnworker is not None else [],
        'channels': _safe(
            lambda: [channel_record(chan) for chan in
                     list(lnworker.channels.values())
                     + list(lnworker.channel_backups.values())], []
        ) if lnworker is not None else [],
        'swaps': [swap_record(payment_hash, swap, wallet=wallet, ledger=ledger)
                  for payment_hash, swap in swaps],
        # What this plugin makes of the same swaps.  Own and unsettled swaps
        # included: an export that showed only the ones that count towards the
        # net return would omit every swap anyone would be debugging.
        'swap_rows': _safe(lambda: [
            _row_as_dict(row) for row in
            build_swap_rows(wallet, include_own=True, include_pending=True)], []),
        'served_swaps_ledger': {'db_key': SERVED_SWAPS_DB_KEY, 'entries': ledger},
        'labels': {
            # Two sources, kept apart: what the operator typed is in the wallet
            # file, what Electrum and this plugin generate is not, and a row
            # reading the wrong thing is usually a question of which won.
            'user': _safe(lambda: dict(wallet.get_all_labels()), {}),
            'default': _safe(lambda: dict(wallet._default_labels), {}),
        },
    }


def _row_as_dict(row: Any) -> Dict[str, Any]:
    """A ``ServedSwapRow`` (and its components) as json-able data.

    ``_asdict`` alone would leave the enums and the nested ``SwapComponent``
    named tuples in place; a named tuple json-encodes as an *array*, so the
    field names would be lost exactly where they are needed.
    """
    data = dict(row._asdict())
    data['role'] = getattr(row.role, 'value', row.role)
    data['status'] = getattr(row.status, 'value', row.status)
    data['missing_legs'] = list(row.missing_legs)
    data['counts_towards_total'] = row.counts_towards_total
    data['components'] = [
        dict(component._asdict(), kind=getattr(component.kind, 'value', component.kind))
        for component in row.components]
    return data


# ---------------------------------------------------------------------------
# Writing it out
# ---------------------------------------------------------------------------

class _ExportEncoder(MyEncoder):
    """Electrum's encoder, plus the types this document adds.

    Upstream's ``util.json_encode`` catches ``TypeError`` and returns
    ``repr(obj)`` for the *whole document*, which would turn a single
    unexpected value into a file that is not json at all.  Failing one value at
    a time is the point of the fallback here.
    """

    #: What ``json`` can encode without asking :meth:`default` again.
    _PRIMITIVES = (str, int, float, bool, type(None), list, tuple, dict)

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Enum):
            return obj.value
        try:
            value = MyEncoder.default(self, obj)
        except TypeError:
            return repr(obj)
        # ``MyEncoder`` hands anything with a ``to_json`` method over to it.
        # If what comes back still cannot be encoded, json calls default() on
        # *that*, and an object whose to_json keeps producing such values would
        # recurse until the interpreter gives up -- taking the export with it,
        # at the moment it is most needed.  One step out is enough.
        if isinstance(value, self._PRIMITIVES):
            return value
        return repr(obj)


def encode_history_export(data: Dict[str, Any]) -> str:
    """The document as json text."""
    return json.dumps(data, cls=_ExportEncoder, sort_keys=True, indent=2)


def write_history_export(
        wallet: 'Abstract_Wallet',
        path: str,
        *,
        plugin_status: Optional[Dict[str, Any]] = None,
        include_raw_confirmed: bool = False,
        timestamp: Optional[int] = None,
) -> Dict[str, Any]:
    """Write the document to ``path``; return a summary for the UI.

    The summary counts what actually went into the file, so the operator is
    told "3 swaps, 214 transactions" rather than just "done" -- an export that
    silently found nothing is otherwise indistinguishable from one that worked.
    """
    data = build_history_export(
        wallet, plugin_status=plugin_status,
        include_raw_confirmed=include_raw_confirmed, timestamp=timestamp)
    text = encode_history_export(data)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
    except (IOError, OSError) as e:
        raise ExportError(str(e)) from e
    return {
        'path': path,
        'bytes': len(text.encode('utf-8')),
        'num_history_entries': len(data.get('history') or []),
        'num_transactions': len(data.get('transactions') or []),
        'num_local_transactions': sum(
            1 for tx in (data.get('transactions') or []) if tx.get('is_local')),
        'num_swaps': len(data.get('swaps') or []),
        'num_channels': len(data.get('channels') or []),
    }
