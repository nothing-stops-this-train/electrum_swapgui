#!/usr/bin/env python3
"""Unit tests for the cancel-safe, resumable announcement proof-of-work.

The headline test is :class:`CancelSafetyTests` -- it is the regression test for
the bug where quitting Electrum mid-grind wedged the asyncio loop thread and the
process never exited.  See plugins/swapserver_gui/pow.py for the analysis.

Run with:  python3 -m pytest tests/test_pow.py
"""
import asyncio
import json
import os
import sys
import threading
import time
import unittest

# --- make electrum + the plugin importable ---------------------------------
# pow.py itself has no electrum dependency, but importing it as part of the
# swapserver_gui package runs __init__.py, which registers config vars.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))  # /home/user/electrum_swapgui
_ELECTRUM_SRC = os.environ.get("ELECTRUM_SRC", os.path.join(_PROJECT_ROOT, "electrum"))
_PLUGINS_DIR = os.path.join(os.path.dirname(_HERE), "plugins")
for _p in (_ELECTRUM_SRC, _PLUGINS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from swapserver_gui import pow as swap_pow  # noqa: E402

PUBKEY = bytes(range(32))
OTHER_PUBKEY = bytes(range(1, 33))

# A target that is found near-instantly, for the "does it actually work" tests.
EASY_TARGET = 8
# A target that will never be hit during a test, so the grind is guaranteed to
# still be running when we cancel it.
IMPOSSIBLE_TARGET = 96


class _LoopThread:
    """A real asyncio loop on a background thread, like Electrum's 'EventLoop'.

    Non-daemon on purpose in the shutdown tests: that is precisely what makes a
    wedged loop thread able to hang the whole interpreter.
    """

    def __init__(self, *, daemon: bool = True) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, name="EventLoop", daemon=daemon)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def __enter__(self) -> asyncio.AbstractEventLoop:
        self.thread.start()
        return self.loop

    def __exit__(self, *exc: object) -> None:
        # let in-flight cancellations settle before stopping, so the loop does
        # not complain about tasks destroyed while pending
        time.sleep(0.2)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)
        if not self.thread.is_alive():
            self.loop.close()

    def loop_is_responsive(self, timeout: float = 10.0) -> bool:
        """True if the loop can still execute a trivial coroutine."""
        try:
            fut = asyncio.run_coroutine_threadsafe(asyncio.sleep(0), self.loop)
            fut.result(timeout=timeout)
            return True
        except Exception:
            return False


class PowBitsTests(unittest.TestCase):
    def test_matches_upstream_implementation(self):
        # guard against drift from electrum.util.get_nostr_ann_pow_amount
        try:
            from electrum.util import get_nostr_ann_pow_amount
        except ImportError:
            self.skipTest("electrum not importable")
        for nonce in (0, 1, 12345, 2 ** 100 + 7):
            self.assertEqual(swap_pow.pow_bits(PUBKEY, nonce),
                             get_nostr_ann_pow_amount(PUBKEY, nonce))

    def test_zero_nonce_is_zero_bits(self):
        # upstream treats nonce 0 as "no PoW", which is why a fresh server
        # always has to grind
        self.assertEqual(swap_pow.pow_bits(PUBKEY, 0), 0)
        self.assertEqual(swap_pow.pow_bits(PUBKEY, None), 0)


class PowStateTests(unittest.TestCase):
    def test_round_trip(self):
        state = swap_pow.PowState(
            pubkey_hex=PUBKEY.hex(), target=30,
            offsets=[10, 20, 30], best_nonce=None, best_bits=0)
        restored = swap_pow.PowState.load(state.dumps(), pubkey=PUBKEY, target=30)
        self.assertEqual(restored.offsets, [10, 20, 30])
        self.assertEqual(restored.hashes_done(), 60)

    def test_best_nonce_round_trips_as_big_int(self):
        nonce = swap_pow.lane_start(3) + 987654321
        bits = swap_pow.pow_bits(PUBKEY, nonce)
        state = swap_pow.PowState(pubkey_hex=PUBKEY.hex(), target=4,
                                  offsets=[1], best_nonce=nonce, best_bits=bits)
        restored = swap_pow.PowState.load(state.dumps(), pubkey=PUBKEY, target=4)
        self.assertEqual(restored.best_nonce, nonce)
        self.assertEqual(restored.best_bits, bits)

    def test_discarded_on_pubkey_change(self):
        # the pubkey is part of the hash preimage, so cursors from another
        # wallet's nostr key say nothing about this one
        state = swap_pow.PowState(pubkey_hex=PUBKEY.hex(), target=30, offsets=[999])
        restored = swap_pow.PowState.load(state.dumps(), pubkey=OTHER_PUBKEY, target=30)
        self.assertEqual(restored.offsets, [])
        self.assertEqual(restored.hashes_done(), 0)

    def test_kept_when_target_raised(self):
        state = swap_pow.PowState(pubkey_hex=PUBKEY.hex(), target=20, offsets=[500])
        restored = swap_pow.PowState.load(state.dumps(), pubkey=PUBKEY, target=30)
        self.assertEqual(restored.offsets, [500])

    def test_kept_when_target_lowered(self):
        # Safe because best_bits is the maximum over everything scanned: if the
        # best is below the new target, nothing in the scanned range meets it.
        state = swap_pow.PowState(pubkey_hex=PUBKEY.hex(), target=30, offsets=[500],
                                  best_nonce=None, best_bits=0)
        restored = swap_pow.PowState.load(state.dumps(), pubkey=PUBKEY, target=20)
        self.assertEqual(restored.offsets, [500])

    def test_corrupt_input_yields_blank_state(self):
        for raw in ("", None, "not json", "[]", '{"offsets": [-1]}',
                    '{"pubkey": "zz", "offsets": "x"}'):
            restored = swap_pow.PowState.load(raw, pubkey=PUBKEY, target=30)
            self.assertEqual(restored.offsets, [])
            self.assertEqual(restored.pubkey_hex, PUBKEY.hex())

    def test_lying_best_bits_is_recomputed(self):
        # a hand-edited config must never make us publish a bad nonce
        raw = json.dumps({"pubkey": PUBKEY.hex(), "target": 30, "offsets": [1],
                          "best_nonce": "12345", "best_bits": 250})
        restored = swap_pow.PowState.load(raw, pubkey=PUBKEY, target=30)
        self.assertEqual(restored.best_bits, swap_pow.pow_bits(PUBKEY, 12345))
        self.assertLess(restored.best_bits, 250)


class GrindTests(unittest.TestCase):
    def test_finds_a_valid_nonce(self):
        state = swap_pow.PowState(pubkey_hex=PUBKEY.hex(), target=EASY_TARGET)
        result = asyncio.run(swap_pow.grind(
            pubkey=PUBKEY, target_bits=EASY_TARGET, state=state, num_workers=2))
        self.assertTrue(result.found)
        self.assertIsNotNone(result.nonce)
        # the returned nonce must genuinely meet the target
        self.assertGreaterEqual(swap_pow.pow_bits(PUBKEY, result.nonce), EASY_TARGET)
        self.assertGreaterEqual(result.bits, EASY_TARGET)

    def test_thread_fallback_finds_a_valid_nonce(self):
        # num_workers=1 exercises the no-fork path (Windows / Android)
        state = swap_pow.PowState(pubkey_hex=PUBKEY.hex(), target=EASY_TARGET)
        result = asyncio.run(swap_pow.grind(
            pubkey=PUBKEY, target_bits=EASY_TARGET, state=state, num_workers=1))
        self.assertTrue(result.found)
        self.assertGreaterEqual(swap_pow.pow_bits(PUBKEY, result.nonce), EASY_TARGET)


class CancelSafetyTests(unittest.TestCase):
    """Regression tests for the shutdown hang."""

    def _start_grind(self, loop, state, *, num_workers=2):
        return asyncio.run_coroutine_threadsafe(
            swap_pow.grind(pubkey=PUBKEY, target_bits=IMPOSSIBLE_TARGET,
                           state=state, num_workers=num_workers,
                           poll_interval=0.05, persist_interval=0.1),
            loop)

    def test_cancel_leaves_loop_responsive(self):
        # THE regression test: upstream's gen_nostr_ann_pow calls
        # executor.shutdown(wait=True) from __exit__ on the cancel path, which
        # blocks the loop thread forever against workers that never stop.
        state = swap_pow.PowState(pubkey_hex=PUBKEY.hex(), target=IMPOSSIBLE_TARGET)
        harness = _LoopThread()
        with harness as loop:
            fut = self._start_grind(loop, state)
            time.sleep(1.5)  # let the workers actually start grinding
            self.assertTrue(fut.cancel())
            self.assertTrue(
                harness.loop_is_responsive(),
                "event loop thread was wedged by cancelling the proof-of-work")

    def test_cancel_thread_backend_leaves_loop_responsive(self):
        # num_workers=1 selects the no-fork fallback (Windows/Android, and CI
        # runners with 2 cores). It must be just as cancel-safe.
        state = swap_pow.PowState(pubkey_hex=PUBKEY.hex(), target=IMPOSSIBLE_TARGET)
        harness = _LoopThread()
        with harness as loop:
            fut = self._start_grind(loop, state, num_workers=1)
            time.sleep(1.5)
            self.assertTrue(fut.cancel())
            self.assertTrue(harness.loop_is_responsive(),
                            "thread backend wedged the event loop on cancel")

    def test_cancelled_workers_are_reaped(self):
        state = swap_pow.PowState(pubkey_hex=PUBKEY.hex(), target=IMPOSSIBLE_TARGET)
        backend_box = {}
        real_start = swap_pow._Backend.start

        def _spy(self_backend, **kwargs):
            real_start(self_backend, **kwargs)
            backend_box["backend"] = self_backend

        swap_pow._Backend.start = _spy
        try:
            harness = _LoopThread()
            with harness as loop:
                fut = self._start_grind(loop, state)
                time.sleep(1.5)
                backend = backend_box["backend"]
                self.assertTrue(backend.any_alive())
                fut.cancel()
                harness.loop_is_responsive()
                deadline = time.time() + 20
                while backend.any_alive() and time.time() < deadline:
                    time.sleep(0.1)
                self.assertFalse(backend.any_alive(),
                                 "proof-of-work workers survived cancellation")
        finally:
            swap_pow._Backend.start = real_start

    def test_cancel_does_not_block_the_caller(self):
        # stop_server() calls this from the GUI thread during shutdown; it must
        # return promptly rather than joining worker processes.
        state = swap_pow.PowState(pubkey_hex=PUBKEY.hex(), target=IMPOSSIBLE_TARGET)
        harness = _LoopThread()
        with harness as loop:
            fut = self._start_grind(loop, state)
            time.sleep(1.5)
            t0 = time.monotonic()
            fut.cancel()
            self.assertTrue(harness.loop_is_responsive())
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 5.0, f"cancellation blocked for {elapsed:.2f}s")

    def test_non_daemon_loop_thread_still_exits(self):
        # The real failure mode: Electrum's loop thread is non-daemon, so a
        # wedged loop means the interpreter can never exit.
        state = swap_pow.PowState(pubkey_hex=PUBKEY.hex(), target=IMPOSSIBLE_TARGET)
        harness = _LoopThread(daemon=False)
        with harness as loop:
            fut = self._start_grind(loop, state)
            time.sleep(1.5)
            fut.cancel()
        self.assertFalse(harness.thread.is_alive(),
                         "non-daemon EventLoop thread did not exit -> "
                         "the process would hang at interpreter shutdown")

    def test_progress_is_persisted_on_cancel(self):
        saved = []
        state = swap_pow.PowState(pubkey_hex=PUBKEY.hex(), target=IMPOSSIBLE_TARGET)
        harness = _LoopThread()
        with harness as loop:
            fut = asyncio.run_coroutine_threadsafe(
                swap_pow.grind(pubkey=PUBKEY, target_bits=IMPOSSIBLE_TARGET,
                               state=state, num_workers=2, poll_interval=0.05,
                               persist_interval=0.1,
                               on_progress=lambda s: saved.append(s.dumps())),
                loop)
            time.sleep(2.5)
            fut.cancel()
            harness.loop_is_responsive()
        self.assertTrue(saved, "no progress was persisted")
        final = swap_pow.PowState.load(saved[-1], pubkey=PUBKEY, target=IMPOSSIBLE_TARGET)
        self.assertGreater(final.hashes_done(), 0,
                           "cancelled grind saved no scanned-nonce cursor")

    def test_resume_continues_past_saved_cursor(self):
        # work must accumulate across cancels rather than re-scanning the same
        # (deterministically chosen) range, which is what upstream does
        state = swap_pow.PowState(pubkey_hex=PUBKEY.hex(), target=IMPOSSIBLE_TARGET,
                                  offsets=[5_000_000])
        harness = _LoopThread()
        with harness as loop:
            fut = self._start_grind(loop, state, num_workers=1)
            time.sleep(2.5)
            fut.cancel()
            harness.loop_is_responsive()
        self.assertGreater(state.offsets[0], 5_000_000,
                           "resumed grind did not continue past the saved cursor")


class EstimateTests(unittest.TestCase):
    def test_estimate_doubles_per_bit(self):
        a = swap_pow.estimate_seconds(20, num_workers=1, hash_rate=1e6)
        b = swap_pow.estimate_seconds(21, num_workers=1, hash_rate=1e6)
        self.assertAlmostEqual(b / a, 2.0, places=6)

    def test_estimate_scales_with_workers(self):
        one = swap_pow.estimate_seconds(20, num_workers=1, hash_rate=1e6)
        four = swap_pow.estimate_seconds(20, num_workers=4, hash_rate=1e6)
        self.assertAlmostEqual(one / four, 4.0, places=6)

    def test_zero_target_is_free(self):
        self.assertEqual(swap_pow.estimate_seconds(0), 0.0)

    def test_format_duration(self):
        self.assertEqual(swap_pow.format_duration(0.2), "< 1s")
        self.assertEqual(swap_pow.format_duration(30), "30s")
        self.assertIn("min", swap_pow.format_duration(600))
        self.assertIn("hours", swap_pow.format_duration(7200))
        self.assertIn("days", swap_pow.format_duration(400000))


if __name__ == "__main__":
    unittest.main()
