#!/usr/bin/env bash
#
# Headless end-to-end check: launches a REAL Electrum Qt GUI (offscreen QPA)
# with the swapserver_gui plugin loaded and the server set to auto-start, then
# asserts that the swap server's HTTP endpoint serves a valid /getpairs, then
# tears the instance down. This exercises the full stack that the visible GUI
# uses (wallet -> lnworker -> swap_manager -> plugin -> aiohttp), just without
# a display, which is the standard way to e2e-test a Qt GUI in CI.
#
# Prerequisites: run_demo.sh has already prepared WORKDIR (wallet + config +
# plugin symlink) and, ideally, bitcoind is running so mining_fee is real.
#
# Usage: bash contrib/regtest_demo/e2e_check.sh
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"
ELECTRUM_SRC="${ELECTRUM_SRC:-$(cd "$PLUGIN_REPO/../electrum" && pwd)}"
WORKDIR="${WORKDIR:-/tmp/swapserver_demo}"
VENV="${VENV:-$WORKDIR/venv}"
EDATA="${EDATA:-$WORKDIR/electrum}"
WALLET="$EDATA/regtest/wallets/default_wallet"
PORT="${SWAP_PORT:-5455}"
PY="$VENV/bin/python"

# See sitecustomize.py: neutralises a sandbox SIGSTKFLT kill; harmless elsewhere.
export PYTHONPATH="$SCRIPT_DIR:${PYTHONPATH:-}"

# Ensure the server auto-starts and the ToU modal won't block headless startup.
"$PY" "$ELECTRUM_SRC/run_electrum" --regtest -D "$EDATA" --offline setconfig plugins.swapserver_gui.autostart true >/dev/null 2>&1
"$PY" "$ELECTRUM_SRC/run_electrum" --regtest -D "$EDATA" --offline setconfig terms_of_use_accepted 9999999999 >/dev/null 2>&1
"$PY" "$ELECTRUM_SRC/run_electrum" --regtest -D "$EDATA" --offline setconfig auto_connect true >/dev/null 2>&1

pkill -f "run_electrum --regtest -D $EDATA" 2>/dev/null
sleep 1
rm -f "$EDATA/regtest/daemon_rpc_socket" "$EDATA/regtest/daemon"

echo "launching Electrum (offscreen) with the swapserver_gui plugin..."
( ulimit -v 12000000; QT_QPA_PLATFORM=offscreen DISPLAY= \
    "$PY" "$ELECTRUM_SRC/run_electrum" --regtest --nohardening \
    -D "$EDATA" -w "$WALLET" > "$WORKDIR/e2e_gui.log" 2>&1 ) &

rc=1
for i in $(seq 1 45); do
    if timeout 2 bash -c "</dev/tcp/127.0.0.1/$PORT" 2>/dev/null; then
        echo "swap server listening on 127.0.0.1:$PORT after ${i}s"
        rc=0; break
    fi
    sleep 1
done

if [ $rc -eq 0 ]; then
    echo "--- GET /getpairs ---"
    "$PY" - "$PORT" <<'PYEOF'
import sys, json, urllib.request
port = sys.argv[1]
d = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/getpairs", timeout=5))
p = d["pairs"]["BTC/BTC"]
assert p["limits"]["minimal"] == 20000, p
assert "percentage" in p["fees"], p
print(json.dumps({
    "percentage": p["fees"]["percentage"],
    "minimal": p["limits"]["minimal"],
    "max_forward": p["limits"]["max_forward_amount"],
    "max_reverse": p["limits"]["max_reverse_amount"],
    "mining_fee": p["fees"]["minerFees"]["baseAsset"]["mining_fee"],
}, indent=2))
print("E2E OK")
PYEOF
    rc=$?
else
    echo "E2E FAIL: server did not come up; tail of log:"
    tail -20 "$WORKDIR/e2e_gui.log"
fi

pkill -f "run_electrum --regtest -D $EDATA" 2>/dev/null
exit $rc
