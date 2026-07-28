#!/usr/bin/env python3
"""End-to-end shutdown tests: the wallet must close even mid-proof-of-work.

These reproduce the reported bug ("Electrum hangs on shutdown and never fully
shuts down") against the real plugin, driving the same sequence Electrum uses:

  1. ``create_and_start_event_loop`` starts the asyncio loop on a **non-daemon**
     thread called 'EventLoop' (electrum/util.py).
  2. On quit, the Qt layer runs the ``close_wallet`` / ``on_close_window`` hooks,
     which call ``plugin.stop_server()``.
  3. ``daemon.stop()`` is awaited *on that loop* from the GUI thread with a
     blocking ``.result()`` and no timeout (electrum/daemon.py:694).
  4. ``run_electrum:sys_exit`` resolves ``stopping_fut``; the loop thread then
     cancels every remaining task and exits.

If anything blocks the loop thread in step 2-4, step 3 never returns and the
non-daemon thread in step 1 keeps the interpreter alive forever.  Before the fix
that is exactly what cancelling the proof-of-work did.

The 'subprocess' test is the strongest form: it asserts a *whole interpreter*
running this sequence actually exits, which is what the user observes.

Run with:  python3 -m pytest tests/test_shutdown_e2e.py
"""
import asyncio
import os
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from unittest import mock

# --- make electrum + the plugin importable ---------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))  # /home/user/electrum_swapgui
_ELECTRUM_SRC = os.environ.get("ELECTRUM_SRC", os.path.join(_PROJECT_ROOT, "electrum"))
_PLUGINS_DIR = os.path.join(os.path.dirname(_HERE), "plugins")
for _p in (_ELECTRUM_SRC, _PLUGINS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from swapserver_gui.swapserver_gui import SwapServerGuiPlugin  # noqa: E402
from swapserver_gui import pow as swap_pow  # noqa: E402

# High enough that the grind is guaranteed to still be running when we shut down.
IMPOSSIBLE_TARGET = 96
# A real 33-byte compressed pubkey shape; the plugin uses pubkey[1:].
PUBKEY_33 = bytes([2]) + bytes(range(32))


class _Keypair:
    def __init__(self, pubkey: bytes) -> None:
        self.pubkey = pubkey


class _Config:
    """Minimal SimpleConfig stand-in, including the PoW state var."""

    def __init__(self, *, port=None, relays="wss://relay.one", pow_target=IMPOSSIBLE_TARGET):
        self.SWAPSERVER_PORT = port
        self.NOSTR_RELAYS = relays
        self.SWAPSERVER_FEE_MILLIONTHS = 5000
        self.SWAPSERVER_POW_TARGET = pow_target
        self.SWAPSERVER_ANN_POW_NONCE = 0
        self.SWAPSERVER_GUI_AUTOSTART = False
        self.SWAPSERVER_GUI_POW_STATE = ''


class _SwapManager:
    def __init__(self):
        self.is_server = False
        self.http_server = None
        self.percentage = None
        self._min_amount = 20000
        self._max_forward = None
        self._max_reverse = None
        self.mining_fee = None
        self.pow_calls = 0
        self.nostr_started = threading.Event()

    async def run_nostr_server(self):
        self.nostr_started.set()
        await asyncio.Event().wait()

    async def set_nostr_proof_of_work(self):
        self.pow_calls += 1

    def server_update_pairs(self):
        self.percentage = 0.5


def _make_wallet(sm, *, pubkey=PUBKEY_33):
    wallet = mock.MagicMock()
    wallet.lnworker.swap_manager = sm
    wallet.lnworker.nostr_keypair = _Keypair(pubkey)
    wallet.has_password.return_value = False
    return wallet


class _ElectrumLikeLoop:
    """Mirrors electrum.util.create_and_start_event_loop closely enough to matter."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.stopping_fut = self.loop.create_future()
        # NON-daemon, exactly like Electrum's 'EventLoop' thread. This is the
        # property that turns a wedged loop into a process that never exits.
        self.thread = threading.Thread(target=self._run, name="EventLoop", daemon=False)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.stopping_fut)
        finally:
            pending = asyncio.gather(*asyncio.all_tasks(self.loop), return_exceptions=True)
            pending.cancel()
            try:
                self.loop.run_until_complete(pending)
            except asyncio.CancelledError:
                pass
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.close()

    def __enter__(self):
        self.thread.start()
        t0 = time.monotonic()
        while not self.loop.is_running():
            time.sleep(0.01)
            if time.monotonic() - t0 > 5:
                raise RuntimeError("loop would not start")
        return self.loop

    def stop_and_join(self, timeout=20.0):
        """The run_electrum:sys_exit sequence. Returns True if the thread exited."""
        self.loop.call_soon_threadsafe(self.stopping_fut.set_result, 1)
        self.thread.join(timeout=timeout)
        return not self.thread.is_alive()

    def __exit__(self, *exc):
        if self.thread.is_alive():
            self.stop_and_join()


class WalletCloseDuringPowTests(unittest.TestCase):
    """The wallet must close cleanly while the proof-of-work is still running."""

    def _running_plugin(self, loop, config, sm):
        plugin = SwapServerGuiPlugin(mock.MagicMock(), config, "swapserver_gui")
        plugin.bind_wallet(_make_wallet(sm))
        plugin.start_server()
        return plugin

    def test_stop_server_mid_pow_leaves_loop_responsive(self):
        config = _Config()
        sm = _SwapManager()
        harness = _ElectrumLikeLoop()
        with harness as loop:
            with mock.patch("swapserver_gui.swapserver_gui.get_asyncio_loop", return_value=loop):
                plugin = self._running_plugin(loop, config, sm)
                # wait until the grind is genuinely underway
                deadline = time.time() + 20
                while not plugin.status()["pow_grinding"] and time.time() < deadline:
                    time.sleep(0.05)
                self.assertTrue(plugin.status()["pow_grinding"], "PoW never started")
                self.assertFalse(sm.nostr_started.is_set(),
                                 "nostr server started before the PoW was ready")

                plugin.stop_server()  # the close_wallet / on_close_window hook

                probe = asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop)
                probe.result(timeout=15)  # raises if the loop thread is wedged
                self.assertFalse(plugin.is_running())
                self.assertFalse(sm.is_server)

    def test_stop_server_mid_pow_does_not_block_gui_thread(self):
        # stop_server() runs on the Qt GUI thread; blocking it there is what
        # makes Electrum's window freeze instead of closing.
        config = _Config()
        sm = _SwapManager()
        harness = _ElectrumLikeLoop()
        with harness as loop:
            with mock.patch("swapserver_gui.swapserver_gui.get_asyncio_loop", return_value=loop):
                plugin = self._running_plugin(loop, config, sm)
                deadline = time.time() + 20
                while not plugin.status()["pow_grinding"] and time.time() < deadline:
                    time.sleep(0.05)
                t0 = time.monotonic()
                plugin.stop_server()
                elapsed = time.monotonic() - t0
                self.assertLess(elapsed, 2.0,
                                f"stop_server blocked the GUI thread for {elapsed:.2f}s")

    def test_event_loop_thread_exits_after_close_mid_pow(self):
        # The reported symptom: Electrum "never fully shuts down".
        config = _Config()
        sm = _SwapManager()
        harness = _ElectrumLikeLoop()
        with harness as loop:
            with mock.patch("swapserver_gui.swapserver_gui.get_asyncio_loop", return_value=loop):
                plugin = self._running_plugin(loop, config, sm)
                deadline = time.time() + 20
                while not plugin.status()["pow_grinding"] and time.time() < deadline:
                    time.sleep(0.05)
                plugin.stop_server()
            exited = harness.stop_and_join(timeout=25)
        self.assertTrue(exited,
                        "the non-daemon EventLoop thread never exited: the "
                        "Electrum process would hang forever on shutdown")

    def test_shutdown_without_stop_server_still_exits(self):
        # Defence in depth: even if the plugin hooks never fire (e.g. the window
        # is destroyed without close_wallet), the loop's own cancel-everything
        # teardown must not wedge on the proof-of-work task.
        config = _Config()
        sm = _SwapManager()
        harness = _ElectrumLikeLoop()
        with harness as loop:
            with mock.patch("swapserver_gui.swapserver_gui.get_asyncio_loop", return_value=loop):
                plugin = self._running_plugin(loop, config, sm)
                deadline = time.time() + 20
                while not plugin.status()["pow_grinding"] and time.time() < deadline:
                    time.sleep(0.05)
                self.assertTrue(plugin.status()["pow_grinding"])
                # no stop_server() call at all
            exited = harness.stop_and_join(timeout=25)
        self.assertTrue(exited, "loop teardown wedged on the proof-of-work task")

    def test_daemon_stop_style_blocking_result_returns(self):
        # This is the precise call that hangs in the field: daemon.run_gui()'s
        # finally-block does
        #     asyncio.run_coroutine_threadsafe(self.stop(), self.asyncio_loop).result()
        # from the GUI thread, with NO timeout (electrum/daemon.py:694). If the
        # loop thread is wedged by the proof-of-work, this never returns and the
        # Electrum window just sits there.
        config = _Config()
        sm = _SwapManager()
        harness = _ElectrumLikeLoop()
        with harness as loop:
            with mock.patch("swapserver_gui.swapserver_gui.get_asyncio_loop", return_value=loop):
                plugin = self._running_plugin(loop, config, sm)
                deadline = time.time() + 20
                while not plugin.status()["pow_grinding"] and time.time() < deadline:
                    time.sleep(0.05)
                self.assertTrue(plugin.status()["pow_grinding"])

                plugin.stop_server()  # close_wallet hook

                async def _fake_daemon_stop():
                    # stands in for stopping wallets / network on the loop
                    await asyncio.sleep(0.1)
                    return "stopped"

                fut = asyncio.run_coroutine_threadsafe(_fake_daemon_stop(), loop)
                # bounded here only so the test fails instead of hanging forever
                self.assertEqual(fut.result(timeout=20), "stopped")
            self.assertTrue(harness.stop_and_join(timeout=25))

    def test_pow_progress_survives_the_shutdown(self):
        # Quitting mid-grind must not throw the scanned range away.
        config = _Config()
        sm = _SwapManager()
        harness = _ElectrumLikeLoop()
        with harness as loop:
            with mock.patch("swapserver_gui.swapserver_gui.get_asyncio_loop", return_value=loop), \
                 mock.patch.object(swap_pow, "CHECKPOINT_BLOCK", 20_000):
                plugin = self._running_plugin(loop, config, sm)
                deadline = time.time() + 25
                while config.SWAPSERVER_GUI_POW_STATE == '' and time.time() < deadline:
                    time.sleep(0.1)
                plugin.stop_server()
            harness.stop_and_join(timeout=25)
        self.assertNotEqual(config.SWAPSERVER_GUI_POW_STATE, '',
                            "no proof-of-work progress was persisted")
        state = swap_pow.PowState.load(config.SWAPSERVER_GUI_POW_STATE,
                                       pubkey=PUBKEY_33[1:], target=IMPOSSIBLE_TARGET)
        self.assertGreater(state.hashes_done(), 0)

    def test_cached_nonce_skips_the_grind_entirely(self):
        # The whole point: with a good cached nonce, upstream's
        # set_nostr_proof_of_work short-circuits and never enters the deadlocking
        # gen_nostr_ann_pow, so the nostr server starts immediately.
        config = _Config(pow_target=4)
        pubkey = PUBKEY_33[1:]
        nonce = next(n for n in range(1, 100000) if swap_pow.pow_bits(pubkey, n) >= 4)
        config.SWAPSERVER_ANN_POW_NONCE = nonce
        sm = _SwapManager()
        harness = _ElectrumLikeLoop()
        with harness as loop:
            with mock.patch("swapserver_gui.swapserver_gui.get_asyncio_loop", return_value=loop):
                plugin = self._running_plugin(loop, config, sm)
                self.assertTrue(sm.nostr_started.wait(timeout=10))
                self.assertFalse(plugin.status()["pow_grinding"])
                self.assertTrue(plugin.status()["pow_ready"])
                plugin.stop_server()
            self.assertTrue(harness.stop_and_join(timeout=25))


class InterpreterExitTests(unittest.TestCase):
    """The end-to-end claim: a whole interpreter doing this actually exits."""

    SCRIPT = textwrap.dedent(
        """
        import asyncio, os, sys, threading, time
        from unittest import mock
        sys.path.insert(0, {electrum!r})
        sys.path.insert(0, {plugins!r})
        sys.path.insert(0, {tests!r})
        from test_shutdown_e2e import (
            _Config, _SwapManager, _make_wallet, _ElectrumLikeLoop, IMPOSSIBLE_TARGET)
        from swapserver_gui.swapserver_gui import SwapServerGuiPlugin

        config = _Config(pow_target=IMPOSSIBLE_TARGET)
        sm = _SwapManager()
        harness = _ElectrumLikeLoop()
        loop = harness.__enter__()
        with mock.patch("swapserver_gui.swapserver_gui.get_asyncio_loop", return_value=loop):
            plugin = SwapServerGuiPlugin(mock.MagicMock(), config, "swapserver_gui")
            plugin.bind_wallet(_make_wallet(sm))
            plugin.start_server()
            deadline = time.time() + 20
            while not plugin.status()["pow_grinding"] and time.time() < deadline:
                time.sleep(0.05)
            assert plugin.status()["pow_grinding"], "PoW never started"
            print("GRINDING", flush=True)
            plugin.stop_server()          # close_wallet hook
        harness.loop.call_soon_threadsafe(harness.stopping_fut.set_result, 1)
        harness.thread.join(timeout=1)    # run_electrum:sys_exit uses timeout=1
        print("EXITING", flush=True)
        sys.exit(0)
        """
    )

    def test_process_exits_while_pow_is_running(self):
        script = self.SCRIPT.format(
            electrum=_ELECTRUM_SRC, plugins=_PLUGINS_DIR, tests=_HERE)
        try:
            proc = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True, text=True, timeout=90,
            )
        except subprocess.TimeoutExpired:
            self.fail("the interpreter never exited while the proof-of-work was "
                      "running -- this is the reported shutdown hang")
        self.assertIn("GRINDING", proc.stdout)
        self.assertIn("EXITING", proc.stdout)
        self.assertEqual(proc.returncode, 0, f"stderr:\n{proc.stderr}")

    def test_no_orphan_worker_processes_survive(self):
        # A daemonic multiprocessing.Process is killed with its parent, so the
        # grind must not leave CPU-burning orphans behind after Electrum exits.
        script = self.SCRIPT.format(
            electrum=_ELECTRUM_SRC, plugins=_PLUGINS_DIR, tests=_HERE)
        before = self._child_count()
        proc = subprocess.run([sys.executable, "-c", script],
                              capture_output=True, text=True, timeout=90)
        self.assertEqual(proc.returncode, 0, f"stderr:\n{proc.stderr}")
        time.sleep(2)
        self.assertLessEqual(self._child_count(), before,
                             "proof-of-work worker processes outlived the parent")

    @staticmethod
    def _child_count():
        try:
            out = subprocess.run(["pgrep", "-fc", "swap-pow"],
                                 capture_output=True, text=True, timeout=10)
            return int(out.stdout.strip() or 0)
        except Exception:
            return 0


if __name__ == "__main__":
    unittest.main()
