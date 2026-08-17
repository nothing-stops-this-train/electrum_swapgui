#!/usr/bin/env python
#
# swapserver_gui - a Qt GUI plugin for Electrum's submarine swap server.
# This file is released into the public domain (The Unlicense); see LICENSE.
#
# Served-swap bookkeeping.  Three jobs, none of which need PyQt6 (so they are
# testable on their own, same reasoning as ``save_settings``):
#
#   1. tell the swaps this server provided to other wallets apart from the
#      swaps the operator's own wallet initiated as a customer;
#   2. break each swap into the components it is actually made of -- the
#      lightning payment, the mining-fee prepayment, the funding transaction,
#      the claim transaction -- so one swap is one row and the components are
#      reachable from it;
#   3. attribute a real per-swap return even when Electrum settles several
#      swaps in a single transaction.
#
# On (3): every swap claim and every swap funding output is handed to the *same*
# transaction batch, keyed ``'swaps'`` (electrum/submarine_swaps.py: the
# ``txbatcher.add_sweep_input('swaps', ...)`` in ``_claim_swap`` and the
# ``txbatcher.add_payment_output('swaps', ...)`` in ``hold_invoice_callback``),
# and ``TxBatch._create_batch_tx`` folds everything pending into one
# ``make_unsigned_transaction`` call.  So a single on-chain transaction can
# settle several served swaps *and* several of the operator's own at once.  The
# wallet's history aggregates value per group, which is why this plugin used to
# report the whole batch on one row and flag it as "not purely server revenue".
# :func:`build_served_swap_rows` instead attributes each swap its own legs and
# splits the batch's mining fee across them; see :func:`split_fee` for the
# invariant that makes the result add up.

import math
import time
from datetime import datetime
from enum import Enum
from typing import (TYPE_CHECKING, Any, Dict, Hashable, List, NamedTuple,
                    Optional, Sequence, Tuple, TypeVar)

from electrum.i18n import _
from electrum.address_synchronizer import TX_HEIGHT_UNCONFIRMED

if TYPE_CHECKING:
    from electrum.wallet import Abstract_Wallet
    from electrum.submarine_swaps import SwapManager, SwapData


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


def taker_did_forward_swap(swap: Any, *, served: bool) -> bool:
    """Was this a *forward* swap (on-chain -> lightning) for the taker?

    ``SwapData.is_reverse`` is written from the point of view of whoever stored
    it, and the server stores the mirror image of what the taker asked for:
    ``server_create_normal_swap`` -- the handler for a taker's *forward* swap --
    calls ``create_reverse_swap`` (electrum/submarine_swaps.py).  So the flag
    means the opposite thing depending on which side we were on, which is why
    upstream's history calls a served forward swap a "Reverse swap".
    """
    return bool(getattr(swap, 'is_reverse', False)) if served \
        else not bool(getattr(swap, 'is_reverse', False))


# ---------------------------------------------------------------------------
# Sizing a swap's contribution to a batched transaction
# ---------------------------------------------------------------------------

#: Bytes the DER-encoded ECDSA signature takes in a claim witness.  The same
#: dummy upstream measures the witness with (electrum/submarine_swaps.py,
#: ``SwapManager.add_txin_info``: ``sig_dummy = b'\x00' * 71``).
_CLAIM_SIG_BYTES = 71
_PREIMAGE_BYTES = 32

#: A swap claim/refund input, without its witness: 32-byte prevout hash +
#: 4-byte output index + an empty scriptSig (1 byte for its length) + 4-byte
#: nSequence.
_TXIN_BASE_BYTES = 41

#: A P2WSH funding output: 8-byte value + 1-byte script length + the 34-byte
#: ``OP_0 <32-byte script hash>`` scriptPubKey.  All of it is non-witness data,
#: so its weight is exactly 4x its size and its vbyte count is its byte count.
FUNDING_OUTPUT_VBYTES = 43


def _varint_len(n: int) -> int:
    if n < 0xfd:
        return 1
    if n <= 0xffff:
        return 3
    if n <= 0xffffffff:
        return 5
    return 9


def _witness_bytes(item_lengths: Sequence[int]) -> int:
    """Serialized size of a witness stack, as ``construct_witness`` builds it."""
    total = _varint_len(len(item_lengths))
    for n in item_lengths:
        total += _varint_len(n) + n
    return total


def claim_input_vbytes(redeem_script_len: int) -> int:
    """vbytes a swap claim (or refund) input adds to a batch transaction.

    Mirrors the witness upstream sizes the input with in
    ``SwapManager.add_txin_info``: ``construct_witness([sig, preimage,
    witness_script])``, where ``witness_script`` is the swap's redeem script.
    """
    witness = _witness_bytes((_CLAIM_SIG_BYTES, _PREIMAGE_BYTES,
                              max(0, int(redeem_script_len))))
    weight = _TXIN_BASE_BYTES * 4 + witness
    return math.ceil(weight / 4)


_K = TypeVar('_K', bound=Hashable)


def split_fee(weights: Dict[_K, int], fee_sat: int) -> Dict[_K, int]:
    """Apportion ``fee_sat`` over ``weights``, exactly.

    The shares always sum back to ``fee_sat``, which is what makes the per-swap
    values add up to the wallet's own on-chain delta for the batch.  For a batch
    transaction, delta = (outputs to us) - (inputs from us); the lockup inputs a
    claim spends are not ours and the funding outputs are not ours either, so::

        delta = sum(claimed) - sum(funded) - fee

    Attributing every swap ``claimed_i - fee_share_i`` (or ``-funded_j -
    fee_share_j``) therefore reproduces ``delta`` exactly once the shares sum to
    ``fee``.  Any wallet coins the batch also spends contribute nothing net:
    they come back as change.

    Rounding uses the largest-remainder method, with ties broken by key, so the
    result never depends on dictionary ordering.
    """
    if not weights:
        return {}
    if fee_sat <= 0:
        return {key: 0 for key in weights}
    total = sum(weights.values())
    if total <= 0:  # nothing meaningful to weight by: share it out evenly
        weights = {key: 1 for key in weights}
        total = len(weights)
    shares: Dict[_K, int] = {}
    remainders: List[Tuple[int, Any]] = []
    allocated = 0
    for key, weight in weights.items():
        exact = fee_sat * int(weight)
        share = exact // total
        shares[key] = share
        allocated += share
        remainders.append((exact - share * total, key))
    remainders.sort(key=lambda t: (-t[0], repr(t[1])))
    for i in range(fee_sat - allocated):
        shares[remainders[i % len(remainders)][1]] += 1
    return shares


# ---------------------------------------------------------------------------
# The components of a swap
# ---------------------------------------------------------------------------

class ComponentKind(Enum):
    """The pieces a submarine swap is made of, from this wallet's side."""

    LN_PAYMENT = 'ln_payment'
    LN_PREPAYMENT = 'ln_prepayment'
    FUNDING_TX = 'funding_tx'
    CLAIM_TX = 'claim_tx'
    REFUND_TX = 'refund_tx'


class SwapComponent(NamedTuple):
    """One leg of a swap, and whether this wallet can show it in its history."""

    kind: ComponentKind
    title: str
    #: Set when this wallet has the leg in its history, so it can be selected
    #: there.  ``None`` for a leg the counterparty made and we never saw.
    txid: Optional[str]
    payment_hash: Optional[str]
    #: This wallet's own value change from the leg, mining fee included, or
    #: ``None`` when the leg is not ours.
    value_sat: Optional[int]
    in_wallet: bool
    detail: str

    @property
    def is_onchain(self) -> bool:
        return self.kind in (ComponentKind.FUNDING_TX, ComponentKind.CLAIM_TX,
                             ComponentKind.REFUND_TX)


class ServedSwapRow(NamedTuple):
    """One swap this server provided to another wallet: exactly one row."""

    payment_hash: str
    label: str
    date: str
    timestamp: int
    return_sat: int
    taker_is_forward: bool
    components: List[SwapComponent]
    lockup_address: str
    locktime: int
    onchain_amount_sat: int
    lightning_amount_sat: int
    #: How many *other* swaps were settled by the same on-chain transaction(s).
    #: Informational only: this row's value is this swap's alone.
    batched_with: int

    @property
    def wallet_components(self) -> List[SwapComponent]:
        return [c for c in self.components if c.in_wallet]


class _Leg(NamedTuple):
    """An on-chain leg of one swap that lands in one of *our* transactions."""

    payment_hash: str
    txid: str
    kind: ComponentKind
    gross_sat: int
    vbytes: int


# ---------------------------------------------------------------------------
# Reading the wallet
# ---------------------------------------------------------------------------

def flatten_history(full_history: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Index every history entry by txid and by payment hash.

    ``wallet.get_full_history`` returns one entry per *group*, with the members
    of a group under ``'children'`` -- except that a group with a single member
    is replaced by that member outright (electrum/wallet.py).  Walking both
    shapes gives a flat index that does not have to care which happened, which
    is the whole reason a served swap used to be able to appear under either its
    group label or its leg label.
    """
    onchain: Dict[str, Any] = {}
    lightning: Dict[str, Any] = {}

    def index(item: Any) -> None:
        if not isinstance(item, dict):
            return
        payment_hash = item.get('payment_hash')
        if item.get('lightning'):
            if payment_hash:
                lightning[payment_hash] = item
            return
        txid = item.get('txid')
        if txid and txid != '----':  # the placeholder for a group with no on-chain leg
            onchain[txid] = item
        if payment_hash:
            lightning.setdefault(payment_hash, item)

    for entry in (full_history or {}).values():
        index(entry)
        if isinstance(entry, dict):
            for child in entry.get('children') or ():
                index(child)
    return onchain, lightning


def lockup_value_sat(sm: Optional['SwapManager'], swap: Any) -> int:
    """What was actually paid into the swap's lockup address.

    Normally exactly ``swap.onchain_amount`` -- that is the figure the taker was
    told to send and the figure ``create_funding_output`` funds with -- but a
    taker may overpay, and ``_claim_swap`` accepts anything at or above the
    expected amount.  Prefer the real utxo when the watcher still has it.
    """
    fallback = int(getattr(swap, 'onchain_amount', 0) or 0)
    funding_txid = getattr(swap, 'funding_txid', None)
    if not funding_txid:
        return fallback
    try:
        adb = getattr(getattr(sm, 'lnwatcher', None), 'adb', None)
        outputs = adb.get_addr_outputs(swap.lockup_address) if adb is not None else None
        for txin in (outputs or {}).values():
            if txin.prevout.txid.hex() != funding_txid:
                continue
            value = txin.value_sats()
            if isinstance(value, int) and value > 0:
                return value
    except Exception:
        pass  # best effort only; the expected amount is right in every normal case
    return fallback


def _our_onchain_legs(
        sm: Optional['SwapManager'],
        payment_hash: str,
        swap: Any,
        onchain_items: Dict[str, Any],
) -> List[_Leg]:
    """The on-chain legs of ``swap`` that this wallet actually made.

    Membership in ``onchain_items`` is the test for "ours": the counterparty's
    leg of a swap only reaches our history if its address was added to
    ``wallet._accounting_addresses``, which upstream does exactly for the cases
    where the leg really is ours to account for.
    """
    legs: List[_Leg] = []
    redeem_script_len = len(getattr(swap, 'redeem_script', b'') or b'')
    funding_txid = getattr(swap, 'funding_txid', None)
    spending_txid = getattr(swap, 'spending_txid', None)
    if getattr(swap, 'is_reverse', False):
        # We hold the claim key: the spending tx is ours, the funding tx is the
        # counterparty's.
        if spending_txid and spending_txid in onchain_items:
            legs.append(_Leg(payment_hash, spending_txid, ComponentKind.CLAIM_TX,
                             lockup_value_sat(sm, swap),
                             claim_input_vbytes(redeem_script_len)))
    else:
        # We fund the lockup and the counterparty claims it -- unless the swap
        # timed out, in which case we took the funds back ourselves and that
        # refund is a second leg of ours.
        if funding_txid and funding_txid in onchain_items:
            legs.append(_Leg(payment_hash, funding_txid, ComponentKind.FUNDING_TX,
                             -int(getattr(swap, 'onchain_amount', 0) or 0),
                             FUNDING_OUTPUT_VBYTES))
        if spending_txid and spending_txid in onchain_items \
                and getattr(swap, 'preimage', None) is None:
            # No preimage means nobody revealed one, so this is our refund and
            # not the taker's claim (``_claim_swap`` stores the preimage as soon
            # as it can extract one from the spending tx).
            legs.append(_Leg(payment_hash, spending_txid, ComponentKind.REFUND_TX,
                             lockup_value_sat(sm, swap),
                             claim_input_vbytes(redeem_script_len)))
    return legs


def _tx_fee_sat(item: Optional[Dict[str, Any]]) -> int:
    if not item:
        return 0
    fee = item.get('fee_sat')
    return int(fee) if isinstance(fee, int) else 0


def _ln_value_sat(item: Optional[Dict[str, Any]]) -> Optional[int]:
    if not item:
        return None
    value = item.get('ln_value')
    if value is None:
        value = item.get('value')
    try:
        return int(value.value)
    except Exception:
        return None


def _onchain_value_sat(item: Optional[Dict[str, Any]]) -> Optional[int]:
    if not item:
        return None
    try:
        return int(item['value'].value)
    except Exception:
        return None


def _item_timestamp(item: Optional[Dict[str, Any]]) -> Optional[int]:
    if not item:
        return None
    ts = item.get('timestamp')
    return int(ts) if isinstance(ts, (int, float)) and ts else None


def build_served_swap_rows(wallet: 'Abstract_Wallet') -> List[ServedSwapRow]:
    """One row per confirmed swap this node served to another wallet.

    Rows are keyed on the swap's payment hash rather than on the wallet's
    history groups, which is what makes a swap exactly one row: a group both
    collapses into its single member (so the row would be labelled after a leg
    instead of the swap) and merges swaps that happened to share a batch
    transaction (so several swaps would be one row).  Values are attributed per
    swap, with the batch mining fee split across the legs that caused it.

    Swaps the operator's own wallet initiated as a customer are left out.  They
    still take part in the fee split, because their legs are in the same
    transaction and the split has to account for all of them.
    """
    lnworker = getattr(wallet, 'lnworker', None)
    sm = getattr(lnworker, 'swap_manager', None) if lnworker else None
    if sm is None:
        return []
    swaps: List[Tuple[str, Any]] = list(sm._swaps.items())
    ledger = served_swaps_ledger(wallet)
    onchain_items, ln_items = flatten_history(wallet.get_full_history())

    # 1. every on-chain leg, of served *and* own swaps, grouped by transaction:
    #    the fee split is only correct if it accounts for all of them.
    legs_by_tx: Dict[str, List[_Leg]] = {}
    for payment_hash, swap in swaps:
        for leg in _our_onchain_legs(sm, payment_hash, swap, onchain_items):
            legs_by_tx.setdefault(leg.txid, []).append(leg)

    # 2. split each transaction's mining fee across the legs that caused it.
    net_by_leg: Dict[Tuple[str, str], int] = {}
    swaps_per_tx: Dict[str, int] = {}
    for txid, legs in legs_by_tx.items():
        swaps_per_tx[txid] = len({leg.payment_hash for leg in legs})
        shares = split_fee({(leg.payment_hash, leg.kind.value): leg.vbytes for leg in legs},
                           _tx_fee_sat(onchain_items.get(txid)))
        for leg in legs:
            key = (leg.payment_hash, leg.kind.value)
            net_by_leg[(leg.payment_hash, leg.txid)] = leg.gross_sat - shares[key]

    rows: List[ServedSwapRow] = []
    for payment_hash, swap in swaps:
        if not is_served_swap(swap, payment_hash, ledger):
            continue
        spending_txid = getattr(swap, 'spending_txid', None)
        if spending_txid is None:
            continue  # still in flight
        if wallet.adb.get_tx_height(spending_txid).height() <= TX_HEIGHT_UNCONFIRMED:
            continue  # only final swaps, so the history is stable
        row = _build_row(
            sm=sm, wallet=wallet, payment_hash=payment_hash, swap=swap,
            onchain_items=onchain_items, ln_items=ln_items,
            net_by_leg=net_by_leg, legs_by_tx=legs_by_tx, swaps_per_tx=swaps_per_tx,
        )
        if row is not None:
            rows.append(row)
    return sorted(rows, key=lambda r: (r.timestamp, r.payment_hash))


def _build_row(
        *,
        sm: Optional['SwapManager'],
        wallet: 'Abstract_Wallet',
        payment_hash: str,
        swap: Any,
        onchain_items: Dict[str, Any],
        ln_items: Dict[str, Any],
        net_by_leg: Dict[Tuple[str, str], int],
        legs_by_tx: Dict[str, List[_Leg]],
        swaps_per_tx: Dict[str, int],
) -> Optional[ServedSwapRow]:
    taker_is_forward = taker_did_forward_swap(swap, served=True)
    funding_txid = getattr(swap, 'funding_txid', None)
    spending_txid = getattr(swap, 'spending_txid', None)
    prepay_hash = getattr(swap, 'prepay_hash', None)
    prepay_hex = prepay_hash.hex() if isinstance(prepay_hash, bytes) else prepay_hash

    components: List[SwapComponent] = []
    total = 0
    timestamps: List[int] = []

    # ---- lightning legs -------------------------------------------------
    ln_item = ln_items.get(payment_hash)
    ln_value = _ln_value_sat(ln_item)
    if ln_value is not None:
        total += ln_value
    ts = _item_timestamp(ln_item)
    if ts:
        timestamps.append(ts)
    components.append(SwapComponent(
        kind=ComponentKind.LN_PAYMENT,
        title=_("Lightning payment") if taker_is_forward else _("Lightning payment (hold invoice)"),
        txid=None,
        payment_hash=payment_hash,
        value_sat=ln_value,
        in_wallet=ln_item is not None,
        detail=(_("Paid to the taker in exchange for their on-chain funds.")
                if taker_is_forward
                else _("Received from the taker in exchange for our on-chain funds.")),
    ))
    if prepay_hex:
        prepay_item = ln_items.get(prepay_hex)
        prepay_value = _ln_value_sat(prepay_item)
        if prepay_value is not None:
            total += prepay_value
        ts = _item_timestamp(prepay_item)
        if ts:
            timestamps.append(ts)
        components.append(SwapComponent(
            kind=ComponentKind.LN_PREPAYMENT,
            title=_("Lightning prepayment (mining fee)"),
            txid=None,
            payment_hash=prepay_hex,
            value_sat=prepay_value,
            in_wallet=prepay_item is not None,
            detail=_("Settled immediately, so the funding transaction's mining "
                     "fee is covered even if the swap is never completed."),
        ))

    # ---- on-chain legs --------------------------------------------------
    def add_onchain(kind: ComponentKind, title: str, txid: Optional[str],
                    theirs_detail: str, ours_detail: str) -> None:
        nonlocal total
        if not txid:
            return
        value = net_by_leg.get((payment_hash, txid))
        ours = value is not None
        if ours:
            total += value
            ts = _item_timestamp(onchain_items.get(txid))
            if ts:
                timestamps.append(ts)
        components.append(SwapComponent(
            kind=kind, title=title, txid=txid, payment_hash=None,
            value_sat=value, in_wallet=ours,
            detail=ours_detail if ours else theirs_detail,
        ))

    add_onchain(
        ComponentKind.FUNDING_TX, _("Funding transaction"), funding_txid,
        theirs_detail=_("Made by the taker; this wallet only watches the lockup "
                        "address, so it is not in its history."),
        ours_detail=_("Pays the swap amount into the lockup address."),
    )
    is_refund = (not getattr(swap, 'is_reverse', False)
                 and getattr(swap, 'preimage', None) is None
                 and spending_txid in onchain_items)
    add_onchain(
        ComponentKind.REFUND_TX if is_refund else ComponentKind.CLAIM_TX,
        _("Refund transaction") if is_refund else _("Claim transaction"),
        spending_txid,
        theirs_detail=_("Made by the taker; this wallet only watches the lockup "
                        "address, so it is not in its history."),
        ours_detail=(_("Takes the swap amount back after the swap timed out.")
                     if is_refund
                     else _("Spends the lockup output, revealing the preimage.")),
    )

    if not timestamps:
        return None  # nothing of ours in the history yet; not reportable
    timestamp = max(timestamps)
    batched_with = max(
        (swaps_per_tx.get(txid, 1) - 1
         for txid in {c.txid for c in components if c.is_onchain and c.in_wallet and c.txid}),
        default=0)
    return ServedSwapRow(
        payment_hash=payment_hash,
        label=format_swap_label(
            served=True, taker_is_forward=taker_is_forward,
            is_submarine_payment=bool(getattr(swap, 'claim_to_output', None)),
            amount_str=_format_amount(sm, swap_amount_sat(swap))),
        date=_format_date(timestamp),
        timestamp=timestamp,
        return_sat=total,
        taker_is_forward=taker_is_forward,
        components=components,
        lockup_address=getattr(swap, 'lockup_address', '') or '',
        locktime=int(getattr(swap, 'locktime', 0) or 0),
        onchain_amount_sat=int(getattr(swap, 'onchain_amount', 0) or 0),
        lightning_amount_sat=int(getattr(swap, 'lightning_amount', 0) or 0),
        batched_with=batched_with,
    )


def _format_date(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")


def _format_amount(sm: Optional['SwapManager'], amount_sat: int) -> str:
    """Format ``amount_sat`` the way upstream's swap labels do."""
    try:
        return sm.config.format_amount_and_units(amount_sat)
    except Exception:
        return f"{amount_sat} sat"


def swap_amount_sat(swap: Any) -> int:
    """The amount upstream puts in a swap's history label.

    Kept identical to ``SwapManager.get_groups_for_onchain_history`` so a
    relabelled row still shows the number the operator saw before.
    """
    claim_to_output = getattr(swap, 'claim_to_output', None)
    if getattr(swap, 'is_reverse', False):
        if claim_to_output:
            return int(claim_to_output[1])
        return int(getattr(swap, 'lightning_amount', 0) or 0)
    return int(getattr(swap, 'onchain_amount', 0) or 0)


# ---------------------------------------------------------------------------
# History-tab labels
# ---------------------------------------------------------------------------

#: Prefixed to the history label of a swap this server provided to someone else.
SERVED_LABEL_MARK = "⇄"   # ⇄
#: Prefixed to the history label of a swap the operator initiated themselves.
OWN_LABEL_MARK = "↪"      # ↪


def format_swap_label(
        *,
        served: bool,
        taker_is_forward: bool,
        is_submarine_payment: bool,
        amount_str: str,
) -> str:
    """The history label for a swap, from the operator's point of view.

    Upstream names a swap after ``SwapData.is_reverse``, which is stored from
    the point of view of whoever stored it -- so a *served* forward swap reads
    "Reverse swap" in the operator's history (see
    :func:`taker_did_forward_swap`).  These labels say the role out loud
    instead, and lead with a word ("Served" / "My") that the history tab's
    search box can filter on and that survives CSV export.
    """
    if is_submarine_payment:
        kind = _("submarine payment")
    elif taker_is_forward:
        kind = _("forward swap")
    else:
        kind = _("reverse swap")
    if served:
        text = _("Served {kind}").format(kind=kind)
        mark = SERVED_LABEL_MARK
    else:
        text = _("My {kind}").format(kind=kind)
        mark = OWN_LABEL_MARK
    return f"{mark} {text} {amount_str}".rstrip()


def relabel_swap_history_groups(
        wallet: 'Abstract_Wallet',
        sm: 'SwapManager',
        groups: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Rewrite the ``group_label`` of every swap group in ``groups`` in place.

    ``groups`` is what ``SwapManager.get_groups_for_onchain_history`` returns:
    ``{txid: {'group_id':…, 'label':…, 'group_label':…}}``.  Only the group
    label changes; the per-leg ``label`` ("Funding transaction", "Claim
    transaction") is already the component name and stays as it is.

    Upstream rebuilds this mapping -- and re-applies the labels it carries -- on
    every history refresh, which is why this has to run as a wrapper around it
    rather than as a one-off write of ``wallet.set_group_label``.
    """
    ledger = served_swaps_ledger(wallet)
    for payment_hash, swap in list(sm._swaps.items()):
        group_id = swap.spending_txid if swap.is_reverse else swap.funding_txid
        if group_id is None:
            continue
        served = is_served_swap(swap, payment_hash, ledger)
        label = format_swap_label(
            served=served,
            taker_is_forward=taker_did_forward_swap(swap, served=served),
            is_submarine_payment=bool(getattr(swap, 'claim_to_output', None)),
            amount_str=_format_amount(sm, swap_amount_sat(swap)),
        )
        for entry in groups.values():
            if entry.get('group_id') == group_id:
                entry['group_label'] = label
    return groups


# ---------------------------------------------------------------------------
# What the Status tab shows
# ---------------------------------------------------------------------------

def get_swap_history(wallet: 'Abstract_Wallet') -> List[Dict[str, Any]]:
    """:func:`build_served_swap_rows` as plain dicts, oldest first."""
    return [row._asdict() for row in build_served_swap_rows(wallet)]


def get_swap_summary(rows: Sequence[Any]) -> Dict[str, Any]:
    """Aggregate stats for a list produced by :func:`build_served_swap_rows`.

    Accepts either the rows themselves or the dicts :func:`get_swap_history`
    returns.
    """
    def field(row: Any, name: str, default: Any = 0) -> Any:
        if isinstance(row, dict):
            return row.get(name, default)
        return getattr(row, name, default)

    if not rows:
        return {'num_swaps': 0, 'overall_return_sat': 0, 'swaps_per_day': 0.0,
                'num_batched': 0}
    num_batched = sum(1 for row in rows if field(row, 'batched_with'))
    profit_loss_sum = sum(int(field(row, 'return_sat')) for row in rows)
    timestamps = [int(field(row, 'timestamp')) for row in rows]
    days = (max(timestamps) - min(timestamps)) // 86400
    swaps_per_day = (len(rows) / days) if days > 0 else 0.0
    return {
        'num_swaps': len(rows),
        'overall_return_sat': profit_loss_sum,
        'swaps_per_day': round(swaps_per_day, 2),
        'num_batched': num_batched,
    }


def format_summary_line(
        *,
        num_swaps: int,
        net_return: str,
        swaps_per_day: float,
        num_batched: int,
) -> str:
    """The one-line summary above the history table.

    Pure, and here rather than in ``qt.py``, so it is testable without PyQt6
    (same reasoning as :func:`save_settings`).  ``net_return`` arrives
    pre-formatted because only the GUI knows the user's unit settings.
    """
    text = _("Swaps served: {num} · net return: {ret} · {rate}/day").format(
        num=num_swaps, ret=net_return, rate=swaps_per_day)
    if num_batched:
        text += " · " + _("{n} settled in a shared transaction").format(n=num_batched)
    return text


def format_batched_note(*, batched_with: int) -> str:
    """Why a row says its transaction also settled other swaps.

    Unlike the note this replaces, the row's value *is* this swap's alone: the
    shared transaction's mining fee is split across the swaps that caused it
    (see :func:`split_fee`).  The count is worth showing anyway, because it
    explains why the wallet's history shows one transaction where the Swap
    Server tab shows several rows.
    """
    return _(
        "This swap was settled by a transaction that also settled {n} other "
        "swap(s).\nThe return shown is this swap's own share: its legs, minus "
        "its share of that transaction's mining fee."
    ).format(n=batched_with)
