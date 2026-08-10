# swapserver_gui — Electrum "Swap Server" tab

A Qt GUI plugin for [Electrum](https://github.com/spesmilo/electrum) that adds a
**Swap Server** tab for managing Electrum's built-in submarine swap server
([docs](https://electrum.readthedocs.io/en/latest/swapserver.html)).

** This software is provided AS-IS without any warranty. You are responsible for the security of your own funds. **

![The Swap Server tab](docs/swap_server_tab.png)

From the tab you can:

- **Enable / disable** the swap server at runtime.
- **Edit every server setting**: HTTP port, swap fee, nostr relays, and the
  nostr announcement proof-of-work target.
- **Watch live output**, split into two sub-tabs:
  - **Status** — your nostr pubkey in **both** encodings (npub and hex, each
    copyable, with the identicon takers see), the advertised pairs (min /
    max-forward / max-reverse / mining fee / fee %), your lightning
    send/receive liquidity, and the history & running P/L of swaps this node
    has served — swaps you initiate yourself as a customer are **not** counted
    (see [which swaps are counted](#a-note-on-which-swaps-are-counted)).
  - **Diagnostics** — why the offer is or is not going out, the three values a
    taker's filter must match, and a one-click **discoverability check**. See
    [Why can nobody see my swap server?](#why-can-nobody-see-my-swap-server)

The server runs two independent transports (either or both):

- **HTTP** — an aiohttp endpoint on `localhost:<port>` (`/getpairs`,
  `/createswap`, …), reusing Electrum's bundled `swapserver` request handlers.
- **Nostr** — announces the offer to the configured `nostr_relays`, on the
  plugin's own announce loop (see
  [Keeping the announcement alive](#keeping-the-announcement-alive)).

## Why a separate plugin?

Electrum ships a `swapserver` plugin, but it is `available_for: ["cmdline"]`
only — it never loads (and never registers its settings) in the Qt GUI. This
plugin is `available_for: ["qt"]`. It **reuses** the upstream request handlers
and config vars (`plugins.swapserver.port`, `.fee_millionths`, `.ann_pow_nonce`)
rather than redeclaring them, and adds its own `plugins.swapserver_gui.autostart`
flag so the server can be restored on the next wallet open.

Because Electrum starts the swap manager with `is_server = False`, the server
tasks are never spawned by Electrum itself in the GUI; the plugin starts/stops
them on the network asyncio loop and keeps the aiohttp `AppRunner` so the HTTP
listener can be shut down cleanly (`swapserver_gui.py:ManagedHttpSwapServer`).

## npub or hex?

The tab shows the server's key in both forms because the two places you meet it
disagree, and they look nothing alike:

| Where | Encoding |
| --- | --- |
| Swap Server tab | npub **and** hex |
| Electrum's *Choose Swap Provider* dialog, **Pubkey** column | hex only |
| `SWAPSERVER_NPUB` (what a taker pins) | npub |

`swap_dialog.py` renders `SwapOffer.server_pubkey` — the raw hex `event.pubkey`
— and keeps the npub only as hidden item data. Same key, two encodings.

The quickest visual check that a provider entry is your server is the
**identicon**: both sides derive it from the *hex* via `pubkey_to_q_icon`, so
the coloured square matches.

## Why can nobody see my swap server?

A swap server can be running, connected, and grinding a perfectly valid proof of
work and still be **completely invisible**, because every rejection on the taker
side is silent — `NostrTransport._get_pairs_loop` in
`electrum/submarine_swaps.py` drops a candidate offer with at most a `debug` log
line. The **Diagnostics** sub-tab exists to make that legible.

A taker only considers an offer when *all* of these line up:

| What | Server side | Taker side |
| --- | --- | --- |
| event kind | `USER_STATUS_NIP38` = `30315` | same |
| `d` tag | `electrum-swapserver-<NOSTR_EVENT_VERSION>` | same — **differs between Electrum versions** |
| `r` tag | `net:<constants.net.NET_NAME>` | same — note `signet` ≠ `mutinynet` |
| `created_at` | now | within ±1 h of the taker's clock |
| proof of work | ground to *your* `SWAPSERVER_POW_TARGET` | at least the *taker's* `SWAPSERVER_POW_TARGET` (**default 30**) |

Ranked causes, most common first:

1. **Nothing was ever published — no liquidity.** `publish_offer` returns early
   unless `max_forward` or `max_reverse` reaches `MIN_SWAP_AMOUNT_SAT`
   (20,000 sat). `max_forward` is capped by *both* your lightning inbound
   capacity and your on-chain spendable balance; `max_reverse` by lightning
   outbound. `server_update_pairs` also rounds both *down* to two leading
   digits, so 19,999 sat becomes 19,000. The Status tab no longer claims to be
   "announcing" in this state.
2. **Proof-of-work asymmetry.** `SWAPSERVER_POW_TARGET` means "bits to grind"
   on the server but "bits I demand" on the taker. Lower it to save CPU and
   every taker still on the default of 30 drops you. The Diagnostics tab warns
   whenever the announcement carries fewer than 30 bits.
3. **Network mismatch.** `signet` and `mutinynet` are separate `NET_NAME`s
   upstream; the `r` tag never matches across them.
4. **Electrum version skew** between the two wallets, via the `d` tag.
5. **The taker is not using nostr at all** — a non-empty `SWAPSERVER_URL` makes
   it build an `HttpTransport`, and a wallet that is itself a server never runs
   the discovery loop.
6. **Relay-side expiry.** Announcements carry a NIP-40 `expiration` of ~10
   minutes, so a server that has been down for longer has no stored offer left.
   Some public relays also silently refuse kind `30315`.
7. **The announcement stopped going out.** The Status tab reports when a relay
   last *acknowledged* an announcement, and warns when that is older than the
   re-announce interval. See below for why upstream's loop could stop silently.

## Keeping the announcement alive

The offer expires after ~10 minutes, so a swap server is only as discoverable
as its last re-announcement. The plugin therefore runs its own announce loop
instead of calling `SwapManager.run_nostr_server`, which can stop announcing
permanently without anything noticing:

- it waits on `transport.is_connected` with **no timeout**. That event is set
  once, only if `relay_manager.connect()` found a live relay on its single
  attempt. If every relay is unreachable at that moment — Electrum starting
  before the network is up, Tor not ready, a resume from suspend — the wait
  never completes, for the rest of the process. `check_direct_messages` blocks
  with it, so the server also stops answering swap requests over nostr.
- `aionostr.Manager.connect` **permanently drops** relays that missed that one
  attempt (`self.relays = connected`) and never runs again, so a blip can
  silently reduce a five-relay server to one.
- it republishes at 600 s against a 610 s expiry, on a 30 s tick, stamping the
  interval *after* the publish returns — so the real period is 600–630 s plus
  latency and drifts later each cycle, leaving the offer expired on strict
  relays for part of every cycle.
- `publish_offer` is `@ignore_exceptions` and swallows `TimeoutError`
  internally, so a server no relay accepts looks exactly like a healthy one.

The plugin's loop re-announces every 5 minutes (comfortably inside the expiry),
rebuilds the transport with a bounded backoff whenever it cannot connect — which
is also what restores pruned relays — recycles it hourly and after repeated
unacknowledged publishes, and composes and sends the event itself so it knows
whether a relay actually acknowledged it. `AnnounceState` gained
`NO_RELAY_CONNECTED`, `PUBLISH_FAILING` and `TASK_DEAD`, each of which the tab
previously rendered as "announcing to N relay(s)".

**Check discoverability** asks each configured relay the taker's own question,
using a throwaway key, and names the first rule that rejects you — per relay,
so "found on 2 of 9" is visible. It runs two queries: one by author (does our
announcement exist here at all?) and one with the taker's verbatim filter (does
it survive it?). That split is what distinguishes a wrong tag from a missing
event from being crowded out of a busy relay's 10-result page.

## Layout

```
plugins/swapserver_gui/
  manifest.json        # available_for: ["qt"], min_electrum_version
  __init__.py          # registers plugins.swapserver_gui.autostart
  _version.py          # plugin version, stamped from the git tag at build time
  swapserver_gui.py    # GUI-agnostic server lifecycle + history helpers
  pow.py               # cancel-safe, resumable announcement proof-of-work
  nostr_check.py       # nostr identity + the discoverability rule engine/check
  qt.py                # the "Swap Server" tab + Plugin(load_wallet/close_wallet)
tests/                 # unit + HTTP/nostr/Qt end-to-end tests
contrib/make_zip.sh    # build the external-plugin zip
contrib/regtest_demo/  # one-command local regtest + nostr demo
```

## Versioning

The tab header shows the installed plugin version, so users can tell which build
they are on. **The git tag is the single source of truth.** `_version.py` is
committed with the placeholder `dev`; `contrib/make_zip.sh` stamps the real value
into a staging copy while building the archive, so the working tree is never
modified:

```bash
bash contrib/make_zip.sh dist 0.2.1   # -> tab shows "v0.2.1"
bash contrib/make_zip.sh dist         # -> tab shows "dev"
```

CI passes `${GITHUB_REF_NAME#v}` for `refs/tags/v*` builds and `dev-g<sha7>`
otherwise, then verifies the stamp landed in the zip. A release asset therefore
can never disagree with the tag it was built from, and any zip from a branch or
PR build is traceable to its commit. To cut a release, push a `vX.Y.Z` tag —
nothing in the tree needs bumping.

## Installing

**As an external zip (for users):**

```bash
bash contrib/make_zip.sh          # -> dist/swapserver_gui.zip
```

Then in Electrum: *Tools → Plugins → Add* the zip and authorise it. The zip is a
plain archive — **no signing key or build secret is required**. Electrum's
authorisation is a local, user-controlled anti-malware step: the first time you
install any third-party plugin, Electrum asks you to pick a "plugin
authorization password", stores the derived pubkey in a root-owned file (one
`pkexec`/sudo prompt), and signs the zip locally with it. Note external plugins
require running Electrum **from source**.

**For development:** symlink the package into an Electrum source checkout's
internal plugins directory (internal plugins are auto-authorised):

```bash
ln -s "$PWD/plugins/swapserver_gui" /path/to/electrum/electrum/plugins/swapserver_gui
```

## Testing

```bash
# ELECTRUM_SRC defaults to ../electrum
ELECTRUM_SRC=/path/to/electrum python3 -m unittest discover -s tests -v
```

- `tests/test_swapserver_gui.py` — start/stop state machine, config gating, and
  history aggregation (mocked swap manager, real asyncio loop).
- `tests/test_http_endpoint.py` — starts the real `ManagedHttpSwapServer`, does a
  live `GET /getpairs`, validates the JSON, and asserts the port is released on
  stop.
- `tests/test_settings_save.py` — persisting settings against a **real**
  `SimpleConfig`, including disabling the HTTP port (see the note below).
- `tests/test_version.py` / `tests/test_zip_plugin_load.py` — version formatting
  and the end-to-end build-time stamping chain.
- `tests/test_nostr_check.py` — the discoverability rule engine, pinned to the
  order of checks `_get_pairs_loop` performs, so the field we blame is the one
  that actually rejects us upstream.
- `tests/test_diagnostics.py` — the announcement state machine, the liquidity
  gate (`publish_offer`'s early return), the npub shown in the Status tab, and
  the offer payload/tags pinned against upstream's `publish_offer` so the event
  we compose ourselves cannot drift out of what takers parse.
- `tests/test_announce_e2e.py` — the announce loop against a **real** in-process
  relay: a started server becomes discoverable to the taker's own filter, keeps
  re-announcing before the offer expires, and recovers when a relay that was
  unreachable at start-up comes back (the case upstream never recovers from).
- `tests/test_discovery_e2e.py` — the check driven against a **real** NIP-01
  relay hosted in-process (`tests/fake_relay.py`, aiohttp WebSocket): offers are
  published with `electrum_aionostr` exactly as the server does, signatures are
  verified on receipt, and each failure mode (low PoW, wrong net, wrong version,
  crowded out, unreachable) is asserted to be named correctly.
- `tests/test_served_swaps.py` — the served-vs-own classifier, the recorded
  ledger, the history filter and the flagging of batched groups.
- `tests/test_served_swaps_e2e.py` — a taker requests swaps over the **real**
  HTTP endpoint against Electrum's **real** `SwapManager` on a **real**
  `WalletDB`; asserts only those reach the history, and that the classification
  survives dumping and reloading the wallet file.
- `tests/test_qt_tab.py` — builds the real `SwapServerTab` on Qt's offscreen
  platform and drives `refresh()`. **Skips when PyQt6 is not installed**; CI
  installs it so these run there.

### A note on which swaps are counted

"Swaps served" means swaps this server provided to *other* wallets. That
distinction is not free: `SwapManager._swaps` (`wallet.db['submarine_swaps']`) is
one store shared by both roles, and `SwapData` has no field naming the side we
were on — the maker and the taker of the same swap write the same attributes.
Upstream's `swapserver.get_history` command counts all of them, so an operator
who also swaps as a customer sees their own activity reported as revenue served.

Two mechanisms replace that guess, in order of authority:

1. **A recorded ledger.** While the server runs, the plugin wraps the only two
   entry points that create a swap for a remote taker — `server_create_swap` and
   `server_create_normal_swap`, which both the HTTP endpoint and the nostr
   transport funnel into — and records each payment hash under the wallet-db key
   `swapserver_gui_served_swaps`. Exact, for every swap created since this
   version was installed.
2. **A field heuristic** (`looks_server_side`) for older swaps, keyed on the
   asymmetric mining-fee prepayment: a server's normal swap always carries a
   `prepay_hash` (`create_normal_swap` hardcodes `prepay=True`) while our own
   forward swap never does (`request_normal_swap` passes `prepay=False`), and the
   reverse pair mirrors it. The e2e test asserts this table against the objects
   upstream actually builds, so it cannot drift silently.

One case survives both: `_claim_swap` feeds the claim input of *every*
`is_reverse` swap into the single `'swaps'` tx batch regardless of role, so one
batched transaction can settle a served swap together with one of your own.
Wallet history aggregates value per group, so there is no per-swap split to
report. Such rows are counted (dropping them would hide real revenue) and marked
`⚠ batched`, with a tooltip saying the value covers both.

### A note on the HTTP port

A disabled HTTP port is stored as `0`, never `None`. `SWAPSERVER_PORT` is the
dotted key `plugins.swapserver.port`, and assigning `None` to it routes into
`SimpleConfig`'s key-*deletion* branch, whose recursion dereferences the
intermediate `plugins.swapserver` dict without checking that it exists — which it
usually does not, since this plugin only writes `plugins.swapserver_gui.*`. The
result was `AttributeError: 'NoneType' object has no attribute 'pop'` on every
"Save settings" with the port disabled. Every reader tests the key for
truthiness, so `0` is equivalent and takes the safe write path. This is an
upstream Electrum bug; the workaround lives here because users run released
AppImages. All writes go through `swapserver_gui.save_settings`, and a static
guard in the tests keeps direct assignments from creeping back into `qt.py`.

For a **full GUI e2e** — a real Electrum Qt GUI (offscreen) with the plugin
loaded, auto-starting the server and serving a real `/getpairs` — see
`contrib/regtest_demo/run_demo.sh e2e`.

CI (`.github/workflows/build-plugin.yml`) runs the tests and publishes
`swapserver_gui.zip` as an artifact (and a release asset on `v*` tags).

## Manual review (regtest + local nostr relay)

```bash
bash contrib/regtest_demo/run_demo.sh          # brings up the stack + GUI
bash contrib/regtest_demo/run_demo.sh stop     # tear down services
```

See [contrib/regtest_demo/README.md](contrib/regtest_demo/README.md).

## License

The Unlicense (public domain). See `LICENSE`.
