# Regtest + local nostr demo

`run_demo.sh` brings up a fully local stack for manually reviewing the
**Swap Server** tab, with nothing touching mainnet or public relays:

| Component     | What it is                          | Port |
|---------------|-------------------------------------|------|
| bitcoind      | regtest chain                       | 18554 (RPC) |
| electrumx     | Electrum server (optional)          | 51001 |
| nostr-relay   | local relay for offer announcements | 6969 |
| Electrum Qt   | GUI with this plugin + a LN wallet  | — |

## Prerequisites

- `bitcoind` / `bitcoin-cli` on `PATH` (Bitcoin Core).
- `python3` with `venv` (the script creates its own venvs and installs PyQt6,
  nostr-relay, and — in an isolated venv — spesmilo electrumx).
- A working X display for the GUI (`echo $DISPLAY`).

## Run

```bash
bash contrib/regtest_demo/run_demo.sh
```

First run creates venvs (slow); later runs reuse them and any already-running
services. Then, in the GUI:

1. Open the **Swap Server** tab.
2. Click **Enable swap server**. The status line shows the HTTP endpoint
   listening and the nostr announcement going out to the local relay.
3. In a terminal, confirm the HTTP endpoint:
   ```bash
   curl -s http://127.0.0.1:5455/getpairs | python3 -m json.tool
   ```
4. Edit a setting (e.g. the fee), click **Save settings**, and watch the output
   panel / `getpairs` update. Toggle the server off and on.

To exercise the **external zip** install path instead of the dev symlink, build
`dist/swapserver_gui.zip` (`bash contrib/make_zip.sh`) and add it from
*Tools → Plugins*.

## Headless e2e check

```bash
bash contrib/regtest_demo/run_demo.sh e2e
```

This launches a **real Electrum Qt GUI offscreen** (`QT_QPA_PLATFORM=offscreen`)
with the plugin loaded and the server set to auto-start, then asserts that the
swap server's HTTP endpoint serves a valid `/getpairs`, and tears the instance
down. Offscreen QPA is the standard way to e2e-test a Qt GUI without a display;
it exercises the full stack (wallet → lnworker → swap_manager → plugin →
aiohttp). Example verified output:

```
percentage : 0.5        # from plugins.swapserver.fee_millionths = 5000
minimal    : 20000      # MIN_SWAP_AMOUNT_SAT
mining_fee : 22500      # real regtest fee estimate
```

### Sandbox note (`--nohardening`, `sitecustomize.py`)

On a normal desktop the GUI launches with no special flags. In some restricted
sandboxes (CI runners that monitor processes via ptrace), Electrum's Linux
memory hardening makes the process non-dumpable, which the sandbox reacts to by
killing it with `SIGSTKFLT`. The demo/e2e scripts therefore pass
`--nohardening` and put `sitecustomize.py` (which ignores `SIGSTKFLT`) on
`PYTHONPATH`. Both are no-ops on a normal machine.

## Stop

```bash
bash contrib/regtest_demo/run_demo.sh stop
```

## Notes

- electrumx is optional: without it the wallet won't sync and the advertised
  max amounts reflect 0 liquidity, but the tab, settings, enable/disable, HTTP
  endpoint, and nostr announcement are all fully exercisable. To see non-zero
  pairs, fund the wallet on regtest and open a channel (see Electrum's own
  `tests/regtest/regtest.sh` for the alice/bob/carol channel dance).
- `WORKDIR` defaults to `/tmp/swapserver_demo` (kept short because Electrum's
  daemon uses an `AF_UNIX` socket with a ~108-char path limit). Override with
  `WORKDIR=/short/path bash run_demo.sh`.
- Override the Electrum source location with `ELECTRUM_SRC=/path/to/electrum`.
