#!/usr/bin/env bash
#
# One-command regtest + local-nostr-relay demo for the swapserver_gui plugin.
#
# Brings up a fully local stack so you can manually review the "Swap Server"
# tab end to end:
#   * bitcoind (regtest)          - the chain
#   * electrumx (regtest)         - the Electrum server (optional; the plugin
#                                   is reviewable without it, pairs just show
#                                   liquidity of 0)
#   * nostr-relay (localhost)     - so the server can announce over nostr
#   * Electrum Qt GUI             - with this plugin loaded and a LN wallet
#
# The plugin source is symlinked into the Electrum clone's internal plugins
# directory, which loads it with no signing/authorisation step (internal
# plugins are auto-authorised). To instead exercise the *external zip* install
# path, build the zip (contrib/make_zip.sh) and install it via the GUI's
# Plugins dialog.
#
# Usage:
#   bash contrib/regtest_demo/run_demo.sh          # start everything + GUI
#   bash contrib/regtest_demo/run_demo.sh stop     # stop background services
#
# Override paths via env vars (see below). Requires: bitcoind/bitcoin-cli on
# PATH, python3-venv, and a working X display for the GUI.
set -euo pipefail

# --- paths -----------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"                    # electrum_swapgui/ (our repo)
ELECTRUM_SRC="${ELECTRUM_SRC:-$(cd "$PLUGIN_REPO/../electrum" && pwd)}"
# Keep WORKDIR short: Electrum's daemon uses an AF_UNIX socket (~108 char cap).
WORKDIR="${WORKDIR:-/tmp/swapserver_demo}"
VENV="$WORKDIR/venv"           # electrum GUI + nostr-relay
VENVX="$WORKDIR/venvx"         # electrumx (isolated: different deps)
BTC_DIR="$WORKDIR/bitcoin"
EX_DB="$WORKDIR/electrumx_db"
NOSTR_DIR="$WORKDIR/nostr"
EDATA="$WORKDIR/electrum"
WALLET="$EDATA/regtest/wallets/default_wallet"
NOSTR_URL="ws://127.0.0.1:6969"
SWAP_PORT=5455

BCLI="bitcoin-cli -datadir=$BTC_DIR -rpcuser=doggman -rpcpassword=donkey -rpcport=18554 -regtest"

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

stop_services() {
    log "stopping demo services"
    pkill -f "run_electrum --regtest -D $EDATA" 2>/dev/null || true
    pkill -f "electrumx_server" 2>/dev/null || true
    pkill -f "nostr-relay -c $NOSTR_DIR" 2>/dev/null || true
    $BCLI stop 2>/dev/null || true
    echo "stopped."
}

if [ "${1:-}" = "stop" ]; then stop_services; exit 0; fi
if [ "${1:-}" = "e2e" ]; then exec bash "$SCRIPT_DIR/e2e_check.sh"; fi

mkdir -p "$WORKDIR" "$BTC_DIR" "$EX_DB" "$NOSTR_DIR" "$EDATA"

# --- venvs -----------------------------------------------------------------
if [ ! -x "$VENV/bin/python" ]; then
    log "creating GUI venv (PyQt6, nostr-relay, ...)"
    python3 -m venv --system-site-packages "$VENV"
    "$VENV/bin/pip" install -q --upgrade pip
    "$VENV/bin/pip" install -q -r "$ELECTRUM_SRC/contrib/requirements/requirements.txt"
    "$VENV/bin/pip" install -q PyQt6 dnspython aiohttp nostr-relay aiosqlite
fi
if [ ! -x "$VENVX/bin/electrumx_server" ]; then
    log "creating electrumx venv"
    python3 -m venv "$VENVX"
    "$VENVX/bin/pip" install -q --upgrade pip
    "$VENVX/bin/pip" install -q "git+https://github.com/spesmilo/electrumx.git" plyvel || \
        echo "WARN: electrumx install failed; continuing without it (pairs will show 0 liquidity)"
fi
PY="$VENV/bin/python"

# --- plugin symlink --------------------------------------------------------
log "symlinking plugin into Electrum's internal plugins dir"
ln -sfn "$PLUGIN_REPO/plugins/swapserver_gui" "$ELECTRUM_SRC/electrum/plugins/swapserver_gui"

# --- bitcoind --------------------------------------------------------------
if ! $BCLI getblockcount >/dev/null 2>&1; then
    log "starting bitcoind (regtest)"
    cat > "$BTC_DIR/bitcoin.conf" <<EOF
regtest=1
txindex=1
server=1
rpcuser=doggman
rpcpassword=donkey
rpcallowip=127.0.0.1
fallbackfee=0.0002
zmqpubrawblock=tcp://127.0.0.1:28332
zmqpubrawtx=tcp://127.0.0.1:28333
[regtest]
rpcbind=0.0.0.0
rpcport=18554
EOF
    bitcoind -datadir="$BTC_DIR" -daemon
    sleep 5
    $BCLI createwallet demo >/dev/null 2>&1 || $BCLI loadwallet demo >/dev/null 2>&1 || true
    $BCLI generatetoaddress 150 "$($BCLI getnewaddress)" >/dev/null
fi
log "bitcoind height: $($BCLI getblockcount)"

# --- electrumx (optional) --------------------------------------------------
if [ -x "$VENVX/bin/electrumx_server" ] && ! (exec 3<>/dev/tcp/127.0.0.1/51001) 2>/dev/null; then
    log "starting electrumx (regtest)"
    setsid env COST_SOFT_LIMIT=0 COST_HARD_LIMIT=0 COIN=Bitcoin NET=regtest DB_ENGINE=leveldb \
        SERVICES="tcp://127.0.0.1:51001,rpc://127.0.0.1:8000" \
        DAEMON_URL="http://doggman:donkey@127.0.0.1:18554" \
        DB_DIRECTORY="$EX_DB" \
        "$VENVX/bin/electrumx_server" </dev/null >"$WORKDIR/electrumx.log" 2>&1 &
    disown || true
fi

# --- nostr relay -----------------------------------------------------------
if ! (exec 3<>/dev/tcp/127.0.0.1/6969) 2>/dev/null; then
    log "starting local nostr relay on $NOSTR_URL"
    DEFCFG="$("$PY" -c 'import os,nostr_relay; print(os.path.join(os.path.dirname(nostr_relay.__file__),"config.yaml"))')"
    sed "s#sqlite+aiosqlite:///nostr.sqlite3#sqlite+aiosqlite:///$NOSTR_DIR/nostr.sqlite3#" \
        "$DEFCFG" > "$NOSTR_DIR/relay.yaml"
    setsid bash -c "cd '$NOSTR_DIR' && exec '$VENV/bin/nostr-relay' -c '$NOSTR_DIR/relay.yaml' serve" \
        </dev/null >"$WORKDIR/nostr.log" 2>&1 &
    disown || true
    sleep 5
fi

# --- electrum wallet + config ---------------------------------------------
ecli() { "$PY" "$ELECTRUM_SRC/run_electrum" --regtest -D "$EDATA" --offline "$@"; }
if [ ! -f "$WALLET" ]; then
    log "creating regtest wallet (segwit -> lightning enabled)"
    ecli create >/dev/null
fi
log "applying swap-server config"
ecli setconfig plugins.swapserver_gui.enabled true   >/dev/null
ecli setconfig plugins.swapserver.port "$SWAP_PORT"  >/dev/null
ecli setconfig plugins.swapserver.fee_millionths 5000 >/dev/null
ecli setconfig nostr_relays "$NOSTR_URL"             >/dev/null
ecli setconfig swapserver_url ""                     >/dev/null
# Leave the server OFF at launch so you can click "Enable swap server" yourself.
ecli setconfig plugins.swapserver_gui.autostart false >/dev/null

# --- launch GUI ------------------------------------------------------------
log "launching Electrum Qt GUI — open the 'Swap Server' tab"
cat <<EOF

  Swap server HTTP port : $SWAP_PORT   (curl http://127.0.0.1:$SWAP_PORT/getpairs once enabled)
  Nostr relay           : $NOSTR_URL
  Wallet                : $WALLET
  Headless e2e check    : bash $SCRIPT_DIR/run_demo.sh e2e
  Stop services later   : bash $SCRIPT_DIR/run_demo.sh stop

EOF
# --nohardening + the sitecustomize on PYTHONPATH keep the GUI alive in
# ptrace-restricted sandboxes; both are harmless on a normal desktop.
export PYTHONPATH="$SCRIPT_DIR:${PYTHONPATH:-}"
exec "$PY" "$ELECTRUM_SRC/run_electrum" --regtest --nohardening -D "$EDATA" -w "$WALLET"
