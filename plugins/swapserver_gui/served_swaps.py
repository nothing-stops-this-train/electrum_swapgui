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
#      swaps in a single transaction -- or say that it cannot.
#
# On (1): three of the four ways a swap reaches the wallet are named by fields
# only one side writes; the fourth -- our own reverse swap against a server that
# sent no ``minerFeeInvoice`` -- is field-identical to a forward swap we served,
# and is settled by :func:`swap_margin_sat` instead, because the server is by
# construction the side that comes out ahead.  See :func:`classify_swap`.
#
# On (3): a leg of ours that is not in the wallet's history has an *unknown*
# value, not a zero one.  Adding up the rest yields a number that reads as a
# result and is not one -- for a served reverse swap missing its prepayment it
# reads as a loss the swap never made, and for a claim with no lightning payment
# behind it, as revenue the swap never earned.  Such a row reports
# ``return_sat=None`` and names the legs it is short of; see
# :attr:`ServedSwapRow.missing_legs` and :func:`get_swap_summary`, which leaves
# those rows out of the total rather than folding a partial sum into it.
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
                    Optional, Sequence, Set, Tuple, TypeVar)

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


class SwapRole(Enum):
    """Which side of a swap this wallet was on."""

    SERVED = 'served'   #: our server created it for a remote taker
    OWN = 'own'         #: we were the customer
    UNKNOWN = 'unknown'  #: the records do not say, and cannot be made to say


def swap_margin_sat(swap: Any) -> int:
    """What this wallet stood to gain from ``swap``, per the agreed amounts.

    The two sides of a swap are not symmetric in one respect that is always
    recorded: the server is, by construction, the side that comes out ahead.
    Both fee calculations in electrum/submarine_swaps.py are one-directional --
    ``_get_send_amount`` adds ``percentage_fee + mining_fee`` and
    ``_get_recv_amount`` subtracts them -- so:

    * we receive on-chain and pay lightning (``is_reverse``):
      ``onchain_amount > lightning_amount`` exactly when we are the server;
    * we pay on-chain and receive lightning:
      ``lightning_amount > onchain_amount`` exactly when we are the server.

    Returns the signed margin, so ``> 0`` means we were the server.  Zero means
    the amounts agree, which only happens against a server charging neither a
    percentage nor a mining fee; the caller has to fall back for that.
    """
    onchain = int(getattr(swap, 'onchain_amount', 0) or 0)
    lightning = int(getattr(swap, 'lightning_amount', 0) or 0)
    return (onchain - lightning) if getattr(swap, 'is_reverse', False) \
        else (lightning - onchain)


def classify_swap(swap: Any, payment_hash_hex: str, ledger: Dict[str, int]) -> SwapRole:
    """Which side of ``swap`` this wallet was on, and how sure we can be.

    In order of authority:

    1. the ledger, which recorded the swap as our server created it;
    2. the fields that only ever one side writes -- see :func:`looks_server_side`
       for the table.  ``claim_to_output`` is set by the customer alone, and a
       prepay hash is set by the server on a swap it funds and by the customer
       on a swap it is funded for, so ``is_reverse`` plus ``prepay_hash``
       settles three of the four creation paths outright;
    3. :func:`swap_margin_sat`, for the one path the fields do not settle: our
       own reverse swap against a server that sent no ``minerFeeInvoice`` is
       field-identical to a forward swap we served.  The docstring of
       :func:`looks_server_side` used to write that case off as not arising in
       practice; it does arise, and it lands a swap we *paid* for in the served
       table with its cost reported as revenue.

    :data:`SwapRole.UNKNOWN` is returned rather than a guess when even the
    margin is silent, so the caller can say so instead of reporting a number
    whose sign it cannot justify.
    """
    if payment_hash_hex in ledger:
        return SwapRole.SERVED
    if not getattr(swap, 'is_reverse', False):
        # create_normal_swap hardcodes prepay=True for the server and
        # request_normal_swap hardcodes prepay=False for the customer.
        return SwapRole.SERVED if getattr(swap, 'prepay_hash', None) is not None \
            else SwapRole.OWN
    if getattr(swap, 'claim_to_output', None) is not None:
        return SwapRole.OWN   # only reverse_swap, the customer's path, sets it
    if getattr(swap, 'prepay_hash', None) is not None:
        return SwapRole.OWN   # create_reverse_swap, the server's path, never does
    margin = swap_margin_sat(swap)
    if margin > 0:
        return SwapRole.SERVED
    if margin < 0:
        return SwapRole.OWN
    return SwapRole.UNKNOWN


def is_served_swap(swap: Any, payment_hash_hex: str, ledger: Dict[str, int]) -> bool:
    """True when ``swap`` was created for a remote taker by our swap server.

    An unclassifiable swap counts as served, so it stays visible in the Swap
    Server tab; :attr:`ServedSwapRow.role` carries the uncertainty on to the
    row, which reports itself as uncertain rather than contributing a number.
    """
    return classify_swap(swap, payment_hash_hex, ledger) is not SwapRole.OWN


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
    #: Whether this leg is one this wallet made, and so one whose value has to
    #: be known before the swap's return adds up.  False for the leg the
    #: counterparty made, which is absent from our history by design.
    expected_ours: bool = True

    @property
    def is_onchain(self) -> bool:
        return self.kind in (ComponentKind.FUNDING_TX, ComponentKind.CLAIM_TX,
                             ComponentKind.REFUND_TX)

    @property
    def is_missing(self) -> bool:
        """Ours, but not in the wallet's history -- so its value is unknown."""
        return self.expected_ours and not self.in_wallet


class ServedSwapRow(NamedTuple):
    """One swap this server provided to another wallet: exactly one row."""

    payment_hash: str
    label: str
    date: str
    timestamp: int
    #: This swap's return, or ``None`` when a leg of ours is missing from the
    #: wallet's history and the sum would therefore be a partial one.  Never a
    #: number that is not the whole of what happened; see :attr:`missing_legs`.
    return_sat: Optional[int]
    taker_is_forward: bool
    components: List[SwapComponent]
    lockup_address: str
    locktime: int
    onchain_amount_sat: int
    lightning_amount_sat: int
    #: How many *other* swaps were settled by the same on-chain transaction(s).
    #: Informational only: this row's value is this swap's alone.
    batched_with: int
    #: Which side of the swap we were on, and whether that could be established.
    role: 'SwapRole' = SwapRole.SERVED
    #: Titles of the legs that are ours but not in the wallet's history.
    missing_legs: Tuple[str, ...] = ()

    @property
    def wallet_components(self) -> List[SwapComponent]:
        return [c for c in self.components if c.in_wallet]

    @property
    def is_complete(self) -> bool:
        """True when every leg of ours is accounted for, so the return is real."""
        return not self.missing_legs

    @property
    def counts_towards_total(self) -> bool:
        """True when this row may be added into the net return.

        A row is excluded when a leg is missing (the sum would be partial) or
        when we could not establish which side of the swap we were on (the sign
        would be meaningless).
        """
        return self.is_complete and self.role is SwapRole.SERVED


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
        role = classify_swap(swap, payment_hash, ledger)
        if role is SwapRole.OWN:
            continue
        spending_txid = getattr(swap, 'spending_txid', None)
        if spending_txid is None:
            continue  # still in flight
        if wallet.adb.get_tx_height(spending_txid).height() <= TX_HEIGHT_UNCONFIRMED:
            continue  # only final swaps, so the history is stable
        row = _build_row(
            sm=sm, wallet=wallet, payment_hash=payment_hash, swap=swap, role=role,
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
        role: SwapRole,
        onchain_items: Dict[str, Any],
        ln_items: Dict[str, Any],
        net_by_leg: Dict[Tuple[str, str], int],
        legs_by_tx: Dict[str, List[_Leg]],
        swaps_per_tx: Dict[str, int],
) -> Optional[ServedSwapRow]:
    taker_is_forward = taker_did_forward_swap(swap, served=True)
    is_reverse = bool(getattr(swap, 'is_reverse', False))
    funding_txid = getattr(swap, 'funding_txid', None)
    spending_txid = getattr(swap, 'spending_txid', None)
    prepay_hash = getattr(swap, 'prepay_hash', None)
    prepay_hex = prepay_hash.hex() if isinstance(prepay_hash, bytes) else prepay_hash

    components: List[SwapComponent] = []
    total = 0
    timestamps: List[int] = []

    # ---- lightning legs -------------------------------------------------
    # Both sides of a swap have a lightning leg, so ours is always expected.
    # When it is absent the swap did not settle the way the amounts say it did
    # -- the taker never paid, or the payment is in a channel this wallet no
    # longer has -- and the value of that leg is not zero, it is unknown.
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
        expected_ours=True,
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
            expected_ours=True,
        ))

    # ---- on-chain legs --------------------------------------------------
    def add_onchain(kind: ComponentKind, title: str, txid: Optional[str],
                    theirs_detail: str, ours_detail: str, expected_ours: bool) -> None:
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
            expected_ours=expected_ours,
        ))

    # Which on-chain leg is ours follows from the direction: we fund the lockup
    # of a swap we serve a taker's reverse swap with, and we claim the lockup of
    # one we serve their forward swap with.  The other leg is the taker's, and
    # its absence from our history is normal rather than a gap.
    add_onchain(
        ComponentKind.FUNDING_TX, _("Funding transaction"), funding_txid,
        theirs_detail=_("Made by the taker; this wallet only watches the lockup "
                        "address, so it is not in its history."),
        ours_detail=_("Pays the swap amount into the lockup address."),
        expected_ours=not is_reverse,
    )
    is_refund = (not is_reverse
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
        expected_ours=is_reverse or is_refund,
    )

    if not timestamps:
        return None  # nothing of ours in the history yet; not reportable
    timestamp = max(timestamps)
    batched_with = max(
        (swaps_per_tx.get(txid, 1) - 1
         for txid in {c.txid for c in components if c.is_onchain and c.in_wallet and c.txid}),
        default=0)
    # A leg of ours that is not in the history has an unknown value, not a zero
    # one, so ``total`` is a partial sum and must not be presented as a return.
    missing_legs = tuple(c.title for c in components if c.is_missing)
    return ServedSwapRow(
        payment_hash=payment_hash,
        label=format_swap_label(
            role=role, taker_is_forward=taker_is_forward,
            is_submarine_payment=bool(getattr(swap, 'claim_to_output', None)),
            amount_str=_format_amount(sm, swap_amount_sat(swap))),
        date=_format_date(timestamp),
        timestamp=timestamp,
        return_sat=None if missing_legs else total,
        taker_is_forward=taker_is_forward,
        components=components,
        lockup_address=getattr(swap, 'lockup_address', '') or '',
        locktime=int(getattr(swap, 'locktime', 0) or 0),
        onchain_amount_sat=int(getattr(swap, 'onchain_amount', 0) or 0),
        lightning_amount_sat=int(getattr(swap, 'lightning_amount', 0) or 0),
        batched_with=batched_with,
        role=role,
        missing_legs=missing_legs,
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
#: Prefixed to the history label of a swap whose side could not be established.
UNKNOWN_LABEL_MARK = "⇄?"


def format_swap_label(
        *,
        role: SwapRole,
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
    if role is SwapRole.SERVED:
        text = _("Served {kind}").format(kind=kind)
        mark = SERVED_LABEL_MARK
    elif role is SwapRole.OWN:
        text = _("My {kind}").format(kind=kind)
        mark = OWN_LABEL_MARK
    else:
        text = _("Unattributed {kind}").format(kind=kind)
        mark = UNKNOWN_LABEL_MARK
    return f"{mark} {text} {amount_str}".rstrip()


def lightning_payment_hashes(wallet: 'Abstract_Wallet') -> Set[str]:
    """The payment hashes the wallet's lightning history will show.

    ``LNWallet.get_lightning_history`` is what ``get_full_history`` itself
    calls, so membership here is exactly the question "will this swap have a
    lightning row to be grouped with".  It walks settled htlcs in the wallet's
    *current* channels, so a payment whose channel is gone is absent from it
    even though the swap record survives.
    """
    lnworker = getattr(wallet, 'lnworker', None)
    try:
        return {key for key in lnworker.get_lightning_history()}
    except Exception:
        return set()


def relabel_swap_history_groups(
        wallet: 'Abstract_Wallet',
        sm: 'SwapManager',
        groups: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Rewrite the labels of every swap group in ``groups`` in place.

    ``groups`` is what ``SwapManager.get_groups_for_onchain_history`` returns:
    ``{txid: {'group_id':…, 'label':…, 'group_label':…}}``.  Normally only the
    group label changes, because the per-leg ``label`` ("Funding transaction",
    "Claim transaction") is the component name and is what the operator wants to
    read *inside* an expanded group.

    The exception is a group that will not survive to be expanded.
    ``Abstract_Wallet.get_full_history`` replaces a group by its single member
    when it has only one (``if len(children) == 1: transactions[key] =
    children[0]``), and the replacement is displayed under the *leg* label --
    the group label, and with it everything this function does, never reaches
    the screen.  That is why a swap whose lightning payment is missing from the
    wallet shows up in the history as a bare "Claim transaction" with no swap
    row anywhere: on-chain leg alone, group of one, collapsed.  So when a group
    is going to collapse, the leg label is overwritten too.

    Upstream rebuilds this mapping -- and re-applies the labels it carries -- on
    every history refresh, which is why this has to run as a wrapper around it
    rather than as a one-off write of ``wallet.set_group_label``.
    """
    ledger = served_swaps_ledger(wallet)
    onchain_members: Dict[str, int] = {}
    for entry in groups.values():
        group_id = entry.get('group_id')
        if group_id:
            onchain_members[group_id] = onchain_members.get(group_id, 0) + 1
    ln_hashes: Optional[Set[str]] = None

    for payment_hash, swap in list(sm._swaps.items()):
        group_id = swap.spending_txid if swap.is_reverse else swap.funding_txid
        if group_id is None:
            continue
        role = classify_swap(swap, payment_hash, ledger)
        label = format_swap_label(
            role=role,
            taker_is_forward=taker_did_forward_swap(
                swap, served=role is not SwapRole.OWN),
            is_submarine_payment=bool(getattr(swap, 'claim_to_output', None)),
            amount_str=_format_amount(sm, swap_amount_sat(swap)),
        )
        collapses = False
        if onchain_members.get(group_id, 0) <= 1:
            # Only then can the lightning legs make the difference, so only then
            # is it worth asking the lnworker for them.
            if ln_hashes is None:
                ln_hashes = lightning_payment_hashes(wallet)
            prepay_hash = getattr(swap, 'prepay_hash', None)
            prepay_hex = prepay_hash.hex() if isinstance(prepay_hash, bytes) else prepay_hash
            collapses = not (payment_hash in ln_hashes
                             or (prepay_hex is not None and prepay_hex in ln_hashes))
        for txid, entry in groups.items():
            if entry.get('group_id') != group_id:
                continue
            entry['group_label'] = label
            if collapses and txid == group_id:
                entry['label'] = label
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
                'num_batched': 0, 'num_incomplete': 0, 'num_unattributed': 0}
    num_batched = sum(1 for row in rows if field(row, 'batched_with'))
    # Only rows whose every leg is in the wallet, and whose side we could
    # establish, may be added up: a partial sum is not a smaller return, and an
    # unattributed swap's sign says nothing.  Counting them separately is what
    # keeps the total honest instead of quietly wrong.
    num_incomplete = sum(1 for row in rows if field(row, 'missing_legs', ()))
    num_unattributed = sum(
        1 for row in rows
        if field(row, 'role', SwapRole.SERVED) is SwapRole.UNKNOWN)
    profit_loss_sum = sum(
        int(field(row, 'return_sat'))
        for row in rows
        if field(row, 'return_sat') is not None
        and field(row, 'role', SwapRole.SERVED) is SwapRole.SERVED)
    timestamps = [int(field(row, 'timestamp')) for row in rows]
    days = (max(timestamps) - min(timestamps)) // 86400
    swaps_per_day = (len(rows) / days) if days > 0 else 0.0
    return {
        'num_swaps': len(rows),
        'overall_return_sat': profit_loss_sum,
        'swaps_per_day': round(swaps_per_day, 2),
        'num_batched': num_batched,
        'num_incomplete': num_incomplete,
        'num_unattributed': num_unattributed,
    }


def format_summary_line(
        *,
        num_swaps: int,
        net_return: str,
        swaps_per_day: float,
        num_batched: int,
        num_incomplete: int = 0,
        num_unattributed: int = 0,
) -> str:
    """The one-line summary above the history table.

    Pure, and here rather than in ``qt.py``, so it is testable without PyQt6
    (same reasoning as :func:`save_settings`).  ``net_return`` arrives
    pre-formatted because only the GUI knows the user's unit settings.

    The net return covers only the rows it can: any row left out of it is
    counted out loud on a second line, because a total that silently drops rows
    reads exactly like a total that includes them.
    """
    text = _("Swaps served: {num} · net return: {ret} · {rate}/day").format(
        num=num_swaps, ret=net_return, rate=swaps_per_day)
    if num_batched:
        text += " · " + _("{n} settled in a shared transaction").format(n=num_batched)
    notes = []
    if num_incomplete:
        notes.append(_("{n} incomplete (a leg is not in this wallet)").format(
            n=num_incomplete))
    if num_unattributed:
        notes.append(_("{n} unattributed (could not tell which side we were on)").format(
            n=num_unattributed))
    if notes:
        text += "\n" + _("Not counted in the net return: {what}").format(
            what=" · ".join(notes))
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


def format_incomplete_note(*, missing_legs: Sequence[str]) -> str:
    """Why a row shows no return.

    Naming the leg matters: "the lightning payment is not in this wallet" is
    something the operator can act on -- the taker never paid, or the channel
    that carried the payment is gone -- whereas a number that quietly left the
    leg out at zero would just look like a loss the swap never made.
    """
    return _(
        "No return is shown because {n} leg(s) of this swap are not in this "
        "wallet's history:\n{legs}\n\nTheir value is unknown, not zero, so "
        "adding up the remaining legs would understate or overstate the swap."
    ).format(n=len(missing_legs), legs="\n".join("· " + leg for leg in missing_legs))


def format_unattributed_note() -> str:
    """Why a row will not say whose swap it was.

    A reverse swap we made as a customer against a server that sent no
    ``minerFeeInvoice`` records exactly the fields our own server records for a
    forward swap it serves, and the amounts agree too, so nothing in the wallet
    distinguishes them.  See :func:`classify_swap`.
    """
    return _(
        "This swap's records do not say which side of it this wallet was on, "
        "and the amounts do not settle it either.\nIt is shown here in case it "
        "was served, but it is left out of the net return.")
