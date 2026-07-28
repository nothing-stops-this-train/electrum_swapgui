#!/usr/bin/env python
#
# swapserver_gui - a Qt GUI plugin for Electrum's submarine swap server.
# This file is released into the public domain (The Unlicense); see LICENSE.
#
# Cancel-safe, resumable proof-of-work for the nostr swap announcement.
#
# WHY THIS MODULE EXISTS
# ----------------------
# Electrum's ``electrum.util.gen_nostr_ann_pow`` deadlocks the asyncio event
# loop if the awaiting task is cancelled mid-grind:
#
#     with ProcessPoolExecutor(max_workers=max_workers) as executor:
#         ...
#         done, pending = await asyncio.wait(tasks, ...)   # <- CancelledError here
#         executor.shutdown(wait=False, cancel_futures=True)  # only on success
#
# On cancellation the ``with`` block's ``__exit__`` runs
# ``executor.shutdown(wait=True)``, a *synchronous* join, on the event loop
# thread.  The workers (``electrum.util.nostr_pow_worker``) only ever exit when
# the shared ``shutdown`` Event is set, and that Event is only set by a worker
# that *finds* a solution.  So the join never returns and the loop thread is
# wedged forever.  Because Electrum's loop runs on a non-daemon thread
# (``electrum/util.py: create_and_start_event_loop``), the whole process then
# refuses to exit -- Electrum "hangs on shutdown".
#
# We therefore never let upstream grind: we compute the nonce here, store it in
# ``config.SWAPSERVER_ANN_POW_NONCE``, and upstream's
# ``SwapManager.set_nostr_proof_of_work`` short-circuits on the cached value.
#
# DESIGN NOTES
# ------------
# * Raw ``multiprocessing.Process`` instead of ``ProcessPoolExecutor``: the
#   executor installs an ``atexit`` hook that joins its workers at interpreter
#   shutdown, which is itself a hang vector.  Raw processes let us SIGTERM/SIGKILL.
# * Cancellation never blocks the event loop thread: we ``terminate()`` (instant)
#   and hand the joining off to a short-lived *daemon* thread.
# * The search is a linear nonce scan, so while there is no such thing as being
#   "partly done" with a PoW (each nonce is an independent 2^-target trial), the
#   set of *already-rejected* nonces is worth keeping.  Upstream restarts every
#   worker from a deterministic start nonce, so a cancelled grind re-tests
#   exactly the range it already rejected.  We persist a per-lane cursor so work
#   accumulates monotonically across restarts.
# * We also track the best nonce seen so far.  That makes the persisted cursors
#   valid for *any* target: if ``best_bits < target`` then no scanned nonce can
#   meet ``target`` either (best is the maximum), so the scanned range stays
#   exhausted even when the user lowers the target.

import asyncio
import hashlib
import json
import multiprocessing
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Sequence

HASH_LEN_BITS = 256
MAX_NONCE = (1 << (32 * 8)) - 1  # 32-byte nonce, as upstream

# The nonce space is split into a fixed number of "lanes" so that a persisted
# cursor keeps its meaning across machines with different core counts.  A lane
# is ~2^250 nonces wide, i.e. unreachable in practice.
NUM_LANES = 64
LANE_SIZE = MAX_NONCE // NUM_LANES

# Hashes a worker computes between two checkpoints.  Checking the shutdown flag
# per hash would dominate the hot loop; per block it is free.  At ~0.7 MH/s this
# bounds a worker's cancellation latency to roughly 1.5s (it is terminated with
# a signal anyway, so this only matters for the clean-exit path).
CHECKPOINT_BLOCK = 1_000_000


def pow_bits(pubkey: bytes, nonce: Optional[int]) -> int:
    """Leading-zero bits of ``sha256(b'electrum-' + pubkey + nonce)``.

    Mirrors ``electrum.util.get_nostr_ann_pow_amount`` exactly; kept local so
    this module can be reasoned about (and unit-tested) on its own.
    """
    if not nonce:
        return 0
    digest = hashlib.sha256(b'electrum-' + pubkey + nonce.to_bytes(32, 'big')).digest()
    return HASH_LEN_BITS - int.from_bytes(digest, 'big').bit_length()


def lane_start(lane_idx: int) -> int:
    return lane_idx * LANE_SIZE


# --------------------------------------------------------------------- state
@dataclass
class PowState:
    """Resumable search state, persisted as JSON in the plugin's config var."""

    pubkey_hex: str = ""
    target: int = 0
    offsets: List[int] = field(default_factory=list)  # hashes done, per lane
    best_nonce: Optional[int] = None
    best_bits: int = 0

    def hashes_done(self) -> int:
        return sum(self.offsets)

    def offset_for(self, lane_idx: int) -> int:
        if lane_idx < len(self.offsets):
            return self.offsets[lane_idx]
        return 0

    def dumps(self) -> str:
        return json.dumps({
            "pubkey": self.pubkey_hex,
            "target": self.target,
            "offsets": self.offsets,
            # decimal string: the nonce is a 256-bit integer
            "best_nonce": str(self.best_nonce) if self.best_nonce is not None else None,
            "best_bits": self.best_bits,
        })

    @classmethod
    def load(cls, raw: Optional[str], *, pubkey: bytes, target: int) -> 'PowState':
        """Restore state for ``pubkey``, or a blank state if it does not apply.

        The cursors are only meaningful for the pubkey they were scanned
        against (it is part of the hash preimage), so a wallet switch discards
        them.  A change of *target* does not invalidate them -- see the module
        docstring.
        """
        fresh = cls(pubkey_hex=pubkey.hex(), target=target)
        if not raw:
            return fresh
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                return fresh
            if data.get("pubkey") != pubkey.hex():
                return fresh  # different nostr key -> different preimage
            offsets = [int(o) for o in data.get("offsets") or []]
            if any(o < 0 or o >= LANE_SIZE for o in offsets):
                return fresh  # corrupt
            raw_nonce = data.get("best_nonce")
            best_nonce = int(raw_nonce) if raw_nonce not in (None, "", "None") else None
            if best_nonce is not None and not (0 <= best_nonce <= MAX_NONCE):
                return fresh
            best_bits = int(data.get("best_bits") or 0)
            # Trust but verify: recompute the claimed best so a corrupted or
            # hand-edited config can never make us publish a bad nonce.
            if best_nonce is not None:
                actual = pow_bits(pubkey, best_nonce)
                if actual != best_bits:
                    best_bits = actual
            return cls(
                pubkey_hex=pubkey.hex(),
                target=target,
                offsets=offsets,
                best_nonce=best_nonce,
                best_bits=best_bits,
            )
        except (ValueError, TypeError):
            return fresh


@dataclass
class PowResult:
    nonce: Optional[int]
    bits: int
    state: PowState
    found: bool


# -------------------------------------------------------------------- worker
def _grind_worker(
    lane_idx: int,
    start_nonce: int,
    initial_offset: int,
    pubkey: bytes,
    target_bits: int,
    shutdown: Any,       # multiprocessing.Event or threading.Event
    progress: Any,       # multiprocessing.Array('Q') or list
    best_bits: Any,      # multiprocessing.Array('i') or list
    best_off: Any,       # multiprocessing.Array('Q') or list
    found: Any,          # multiprocessing.Array('b') or list
) -> None:
    """Scan a lane for a nonce meeting ``target_bits``.

    Shared by the process backend (via fork) and the thread fallback; the
    shared-state arguments only need ``__getitem__``/``__setitem__``.
    """
    sha256 = hashlib.sha256
    preimage = b'electrum-' + pubkey
    threshold = 1 << (HASH_LEN_BITS - target_bits)
    offset = initial_offset
    nonce = start_nonce + offset
    local_best_val = 1 << HASH_LEN_BITS  # sentinel: worse than any real digest
    local_best_off = offset

    while True:
        for _ in range(CHECKPOINT_BLOCK):
            digest = sha256(preimage + nonce.to_bytes(32, 'big')).digest()
            val = int.from_bytes(digest, 'big')
            if val < threshold:
                progress[lane_idx] = offset + 1
                best_off[lane_idx] = offset
                best_bits[lane_idx] = HASH_LEN_BITS - val.bit_length()
                found[lane_idx] = 1
                shutdown.set()
                return
            if val < local_best_val:  # single int compare: cheap in the hot loop
                local_best_val = val
                local_best_off = offset
            nonce += 1
            offset += 1
        progress[lane_idx] = offset
        bits = HASH_LEN_BITS - local_best_val.bit_length()
        if bits > best_bits[lane_idx]:
            best_bits[lane_idx] = bits
            best_off[lane_idx] = local_best_off
        if shutdown.is_set():
            return


# ------------------------------------------------------------------ backends
def _fork_available() -> bool:
    try:
        return "fork" in multiprocessing.get_all_start_methods()
    except (AttributeError, ValueError, ImportError):
        return False


def default_worker_count() -> int:
    """All but one CPU, as upstream, so the UI stays responsive."""
    try:
        return max(multiprocessing.cpu_count() - 1, 1)
    except NotImplementedError:
        return 1


class _Backend:
    """Owns the running workers and the shared progress state."""

    def __init__(self, num_workers: int) -> None:
        self.num_workers = num_workers
        self.procs: List[Any] = []
        self.shutdown: Any = None
        self.progress: Any = None
        self.best_bits: Any = None
        self.best_off: Any = None
        self.found: Any = None
        self.uses_processes = False

    def start(self, *, pubkey: bytes, target_bits: int, state: PowState) -> None:
        n = self.num_workers
        # 'fork' lets the children inherit the shared primitives (they cannot be
        # pickled, which is why upstream needed a Manager process).  Without it
        # we fall back to a single in-process thread: the plugin dir is not
        # guaranteed to be importable in a 'spawn'ed child (it may be loaded
        # from a zip as electrum_external_plugins.swapserver_gui), so a process
        # backend there would fail at unpickling.
        #
        # Python 3.12 warns that forking a multi-threaded process (which
        # Electrum is) can deadlock the child.  That is safe here because
        # _grind_worker touches nothing that could hold an inherited lock: no
        # imports, no logging, no allocator-heavy library calls -- only hashlib,
        # int arithmetic, and the inherited shared primitives (which are POSIX
        # semaphores / shared memory, not Python locks).
        if _fork_available() and n > 1:
            ctx = multiprocessing.get_context("fork")
            self.shutdown = ctx.Event()
            self.progress = ctx.Array('Q', n)     # unsigned long long
            self.best_bits = ctx.Array('i', n)
            self.best_off = ctx.Array('Q', n)
            self.found = ctx.Array('b', n)
            self.uses_processes = True
            factory: Callable[..., Any] = ctx.Process
        else:
            self.num_workers = n = 1
            self.shutdown = threading.Event()
            self.progress = [0]
            self.best_bits = [0]
            self.best_off = [0]
            self.found = [0]
            self.uses_processes = False
            factory = threading.Thread

        for lane_idx in range(n):
            offset = state.offset_for(lane_idx)
            self.progress[lane_idx] = offset
            proc = factory(
                target=_grind_worker,
                args=(lane_idx, lane_start(lane_idx), offset, pubkey, target_bits,
                      self.shutdown, self.progress, self.best_bits,
                      self.best_off, self.found),
                daemon=True,  # must never keep the interpreter alive
                name=f"swap-pow-{lane_idx}",
            )
            proc.start()
            self.procs.append(proc)

    def any_alive(self) -> bool:
        return any(p.is_alive() for p in self.procs)

    def winner(self) -> Optional[int]:
        """Lane index that found a solution, if any."""
        for lane_idx in range(self.num_workers):
            if self.found[lane_idx]:
                return lane_idx
        return None

    def snapshot(self, state: PowState) -> PowState:
        """Fold the live shared counters back into a persistable state."""
        offsets = list(state.offsets)
        while len(offsets) < self.num_workers:
            offsets.append(0)
        for lane_idx in range(self.num_workers):
            offsets[lane_idx] = max(offsets[lane_idx], int(self.progress[lane_idx]))
            bits = int(self.best_bits[lane_idx])
            if bits > state.best_bits:
                state.best_bits = bits
                state.best_nonce = lane_start(lane_idx) + int(self.best_off[lane_idx])
        state.offsets = offsets
        return state

    def stop(self) -> None:
        """Tear the workers down WITHOUT ever blocking the caller.

        This runs on the asyncio loop thread during cancellation, so it must
        return immediately -- that is the entire point of this module.
        """
        if self.shutdown is not None:
            self.shutdown.set()
        procs = list(self.procs)
        self.procs = []
        if not procs:
            return
        if self.uses_processes:
            for p in procs:
                try:
                    if p.is_alive():
                        p.terminate()  # SIGTERM, returns immediately
                except (OSError, ValueError, AttributeError):
                    pass

        def _reap() -> None:
            for p in procs:
                try:
                    p.join(timeout=2.0)
                    if self.uses_processes and p.is_alive():
                        p.kill()
                        p.join(timeout=2.0)
                except (OSError, ValueError, AttributeError):
                    pass

        # daemon thread: reaping must not delay shutdown either
        threading.Thread(target=_reap, name="swap-pow-reap", daemon=True).start()


# --------------------------------------------------------------------- grind
async def grind(
    *,
    pubkey: bytes,
    target_bits: int,
    state: PowState,
    num_workers: Optional[int] = None,
    on_progress: Optional[Callable[[PowState], None]] = None,
    poll_interval: float = 0.25,
    persist_interval: float = 10.0,
) -> PowResult:
    """Search for a nonce with at least ``target_bits`` of work.

    Fully cancellable: on ``CancelledError`` the workers are signalled and
    terminated, the search cursors are folded back into ``state`` and handed to
    ``on_progress`` (so the caller can persist them), and the error is re-raised.
    The event loop thread is never blocked, on any path.
    """
    if num_workers is None:
        num_workers = default_worker_count()
    num_workers = max(1, min(num_workers, NUM_LANES))

    backend = _Backend(num_workers)
    last_persist = 0.0

    def _publish(force: bool = False) -> None:
        nonlocal last_persist
        backend.snapshot(state)
        if on_progress is None:
            return
        now = time.monotonic()
        if force or (now - last_persist) >= persist_interval:
            last_persist = now
            on_progress(state)

    try:
        backend.start(pubkey=pubkey, target_bits=target_bits, state=state)
        last_persist = time.monotonic()
        while True:
            await asyncio.sleep(poll_interval)
            lane = backend.winner()
            if lane is not None:
                break
            if not backend.any_alive():
                # every worker exited without a hit (only possible if something
                # external killed them); report what we scanned and give up.
                _publish(force=True)
                return PowResult(nonce=None, bits=state.best_bits, state=state, found=False)
            _publish()
    except asyncio.CancelledError:
        backend.stop()
        _publish(force=True)
        raise
    except BaseException:
        backend.stop()
        _publish(force=True)
        raise

    # success path
    nonce = lane_start(lane) + int(backend.best_off[lane])
    bits = pow_bits(pubkey, nonce)
    backend.stop()
    if bits > state.best_bits:
        state.best_bits = bits
        state.best_nonce = nonce
    _publish(force=True)
    return PowResult(nonce=nonce, bits=bits, state=state, found=True)


# ------------------------------------------------------------------ estimates
_HASH_RATE: Optional[float] = None


def benchmark_hash_rate(samples: int = 40_000) -> float:
    """Single-core hashes/second, measured once and cached.

    Used only to show the user an estimate before they commit to a target.
    """
    global _HASH_RATE
    if _HASH_RATE is not None:
        return _HASH_RATE
    preimage = b'electrum-' + bytes(32)
    sha256 = hashlib.sha256
    nonce = 0
    t0 = time.perf_counter()
    for _ in range(samples):
        sha256(preimage + nonce.to_bytes(32, 'big')).digest()
        nonce += 1
    elapsed = time.perf_counter() - t0
    _HASH_RATE = samples / elapsed if elapsed > 0 else 1e6
    return _HASH_RATE


def estimate_seconds(target_bits: int, *, num_workers: Optional[int] = None,
                     hash_rate: Optional[float] = None) -> float:
    """Expected seconds to find a nonce with ``target_bits`` of work."""
    if target_bits <= 0:
        return 0.0
    if num_workers is None:
        num_workers = default_worker_count()
    if hash_rate is None:
        hash_rate = benchmark_hash_rate()
    return (1 << target_bits) / max(hash_rate * num_workers, 1.0)


def format_duration(seconds: float) -> str:
    if seconds < 1:
        return "< 1s"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 90 * 60:
        return f"{seconds / 60:.1f} min"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.1f} hours"
    return f"{seconds / 86400:.1f} days"
