#!/usr/bin/env python3
"""End-to-end test of the swap server's HTTP endpoint.

Starts the real ``ManagedHttpSwapServer`` (aiohttp routes inherited from
Electrum's bundled ``HttpSwapServer``) on a loopback port, performs a real
HTTP GET /getpairs, validates the JSON, then stops the server and asserts the
port is released.  No chain, lightning, or GUI required — the swap manager is
a minimal stub that supplies the advertised pair fields.
"""
import asyncio
import json
import os
import socket
import sys
import threading
import unittest
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
_ELECTRUM_SRC = os.environ.get("ELECTRUM_SRC", os.path.join(_PROJECT_ROOT, "electrum"))
_PLUGINS_DIR = os.path.join(os.path.dirname(_HERE), "plugins")
for p in (_ELECTRUM_SRC, _PLUGINS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from swapserver_gui.swapserver_gui import ManagedHttpSwapServer  # noqa: E402


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _StubSwapManager:
    """Supplies exactly what HttpSwapServer.get_pairs reads."""
    def __init__(self):
        self._min_amount = 20_000
        self._max_forward = 4_000_000
        self._max_reverse = 3_000_000
        self.percentage = 0.5
        self.mining_fee = 1_500

    def server_update_pairs(self):
        # in the real manager this recomputes from liquidity; here it's a no-op
        pass


class _StubWallet:
    def __init__(self, sm):
        self.lnworker = type("LN", (), {"swap_manager": sm})()

    def has_password(self):
        return False

    def get_unlocked_password(self):
        return None


class _StubConfig:
    def __init__(self, port):
        self.SWAPSERVER_PORT = port


class HttpEndpointTest(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def tearDown(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=10)

    def test_getpairs_served_then_port_released(self):
        port = _free_port()
        sm = _StubSwapManager()
        wallet = _StubWallet(sm)
        config = _StubConfig(port)

        server = ManagedHttpSwapServer(config, wallet)
        try:
            self._run(server.run())

            with urllib.request.urlopen(f"http://127.0.0.1:{port}/getpairs", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read())

            # Validate the documented pair structure the client relies on.
            pair = data["pairs"]["BTC/BTC"]
            self.assertEqual(pair["limits"]["minimal"], 20_000)
            self.assertEqual(pair["limits"]["max_forward_amount"], 4_000_000)
            self.assertEqual(pair["limits"]["max_reverse_amount"], 3_000_000)
            self.assertEqual(pair["fees"]["percentage"], 0.5)
            self.assertEqual(
                pair["fees"]["minerFees"]["baseAsset"]["mining_fee"], 1_500)
        finally:
            self._run(server.stop())

        # After stop, the port must be free again (connection refused).
        with self.assertRaises((ConnectionRefusedError, urllib.error.URLError)):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/getpairs", timeout=2)


if __name__ == "__main__":
    unittest.main()
