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
    has served — one row per swap, **double-click a row** to see the components
    that swap is made of and click through to any of them in Electrum's History
    tab. Swaps you initiate yourself as a customer are **not** counted
    (see [which swaps are counted](#a-note-on-which-swaps-are-counted)).
  - **Diagnostics** — why the offer is or is not going out, the three values a
    taker's filter must match, and a one-click **discoverability check**. See
    [Why can nobody see my swap server?](#why-can-nobody-see-my-swap-server)

The plugin also adds a second top-level tab:

- **History (Swaps)** — your wallet's history reorganised per swap instead of
  per transaction: one row per swap, its components expandable underneath,
  **double-click a component** to jump to it in Electrum's History tab. Unlike
  the Status table this one includes the swaps you made as a customer and the
  ones that have not settled, each marked with its status — including `Local`,
  the batch that was never broadcast. Electrum's own History tab is left
  **exactly** as upstream renders it. See
  [the History (Swaps) tab](#the-history-swaps-tab-and-why-electrums-history-tab-is-left-alone).

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
  swapserver_gui.py    # GUI-agnostic server lifecycle
  served_swaps.py      # served-vs-own classifier, per-swap rows, swap status
  pow.py               # cancel-safe, resumable announcement proof-of-work
  nostr_check.py       # nostr identity + the discoverability rule engine/check
  qt.py                # "Swap Server" + "History (Swaps)" tabs + Plugin hooks
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
  ledger, one-row-per-swap assembly, the batch mining-fee split (including that
  the shares always add back up), the swap labels, and the status each height
  maps to (a never-broadcast spend is `Local`, and never counts).
- `tests/test_served_swaps_e2e.py` — a taker requests swaps over the **real**
  HTTP endpoint against Electrum's **real** `SwapManager` on a **real**
  `WalletDB`; asserts only those reach the history, that a swap is one row with
  all of its legs, that a batched claim is split per swap, that the real group
  builder comes back **unmodified** (Electrum's History tab is upstream's), that
  a batch stranded as a local transaction is reported as `Local` rather than
  counted as served, and that the classification survives dumping and reloading
  the wallet file.
- `tests/test_qt_tab.py` — builds the real `SwapServerTab` and the real
  `SwapHistoryTab` on Qt's offscreen platform and drives `refresh()`, opens the
  component window, drives the jump into a real `CustomModel` history tree, and
  asserts both tabs are registered and that binding a wallet leaves the swap
  manager untouched. **Skips when PyQt6 is not installed**; CI installs it so
  these run there.

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
2. **Fields only one side writes**, for older swaps. A server's normal swap
   always carries a `prepay_hash` (`create_normal_swap` hardcodes `prepay=True`)
   while our own forward swap never does (`request_normal_swap` passes
   `prepay=False`); `claim_to_output` is set by the customer alone. That names
   three of the four ways a swap reaches the wallet.
3. **The sign of the margin** (`swap_margin_sat`), for the fourth. Our own
   reverse swap against a server that sent no `minerFeeInvoice` records exactly
   what our own server records for a forward swap it serves — same
   `is_reverse`, no `prepay_hash`, no `claim_to_output`. The amounts still tell
   them apart, because the server is by construction the side that comes out
   ahead: `_get_send_amount` adds `percentage_fee + mining_fee` and
   `_get_recv_amount` subtracts them, and each is used in one direction only. So
   `onchain_amount > lightning_amount` on a swap we claim (and the reverse on
   one we fund) means we were the server. Without this, a reverse swap you *paid
   for* lands in the served table with its cost reported as a negative return.

When even the margin is silent — a server charging neither a percentage nor a
mining fee — the swap is reported as **unattributed** rather than guessed at: it
stays visible, and stays out of the net return.

The e2e test asserts this table against the objects upstream actually builds, so
it cannot drift silently.

### A note on returns that are not shown

A leg of a swap that is yours but is **not in the wallet's history** has an
unknown value, not a zero one. It happens: the taker never paid the hold
invoice, or the channel that carried the payment is gone (`get_lightning_history`
walks the wallet's *current* channels). Adding up the remaining legs then
produces a number that reads as a result and is not one — a served reverse swap
missing its prepayment reads as a loss it never made, and a claim transaction
with no lightning payment behind it reads as revenue it never earned.

Such a row shows `— incomplete` instead of a figure, names the legs it is short
of in its tooltip and detail window, and is left out of the net return, which
says how many rows it excluded. The leg the *counterparty* made is a different
thing entirely: it is absent by design, and does not make a row incomplete.

### A note on one row per swap

A swap is never one transaction. It is a lightning payment, sometimes a
mining-fee prepayment, a funding transaction and a claim transaction — and only
some of those are yours, depending on which side of the swap you were on. Rows
are therefore keyed on the swap's **payment hash**, not on the wallet's history
groups, because a group does two things that break the mapping: it collapses
into its single member (`wallet.get_full_history` replaces a one-member group
with the member itself, so the row ends up named after a leg — "Claim
transaction" — instead of after the swap), and it merges swaps that happened to
share a transaction.

Sharing is normal, not an edge case: `_claim_swap` feeds every claim into the
single `'swaps'` tx batch and `hold_invoice_callback` feeds every funding output
into the same one, and `TxBatch._create_batch_tx` folds everything pending into
one transaction. So one transaction can settle several served swaps *and*
several of your own. Each swap still gets its own row and its own value: its
legs, minus its share of that transaction's mining fee, apportioned by the
vbytes each leg contributes (a claim input carries a witness — signature,
preimage, redeem script — a funding output is 43 bytes). The shares are computed
by largest remainder so they sum back to the fee **exactly**, which is what
makes the rows reconcile against the wallet's own delta for the transaction.

### The History (Swaps) tab, and why Electrum's History tab is left alone

Electrum's History tab is transaction-shaped, and a swap is not a transaction:
it is a lightning payment, sometimes a mining-fee prepayment, a funding
transaction, and a claim or refund transaction. The wallet's history scatters
those across a group — or collapses the group into a single leg, or merges two
swaps that shared a batch transaction. Reading a swap out of it is work.

This plugin used to do that work *in place*: it wrapped
`SwapManager.get_groups_for_onchain_history` on the instance and rewrote the
group labels to say the role out loud, because upstream labels a swap after
`SwapData.is_reverse`, and that flag is stored from the point of view of
whoever stored it — `server_create_normal_swap`, the handler for a taker's
**forward** swap, calls `create_reverse_swap`, so a forward swap you served
reads "Reverse swap" in your own history.

It no longer does. **Electrum's History tab now renders exactly as upstream
renders it**, confusing label and all; nothing here wraps the group builder or
writes to the wallet. The reorganised view lives in a tab of its own instead:

**History (Swaps)** — one top-level row per swap, the components it is made of
underneath it, and a value that is that swap's own:

| | |
|---|---|
| `⇄ Served forward swap 0.2 mBTC` | provided by your server to someone else |
| `↪ My forward swap 0.1 mBTC` | initiated by you as a customer |
| `⇄? Unattributed forward swap 0.2 mBTC` | the records do not say which |

Unlike the Swap Server tab's table, this one lists your own swaps beside the
served ones, and unsettled swaps beside the settled ones — a swap you can see in
the History tab should never be missing from the swap-shaped view of that same
history. Only served, settled, complete swaps contribute to the net return
(`ServedSwapRow.counts_towards_total`); everything left out is counted out loud
under the total rather than silently dropped.

Double-clicking a component jumps to that leg in Electrum's History tab, which
is what makes leaving that tab untouched workable: it is still where a
transaction is inspected, this tab is where a *swap* is.

### Swap status, and the "Local" transaction that will not go away

Each row carries a `SwapStatus`, decided by the transaction that ends the swap:

| | |
|---|---|
| `Confirmed` | the spending tx confirmed; the swap is over and it counts |
| `Unconfirmed` | in the mempool; it will settle on the next block |
| `In flight` | the lockup is unspent, or the spend is timelocked |
| `Local` | **the spending tx is in the wallet but was never broadcast** |

The last one is worth its own name. `TxBatch.run_iteration`
(`electrum/txbatcher.py`) adds every batch transaction to the wallet as local
*before* broadcasting it, in order to reserve its UTXOs. If the broadcast fails
and there is no base transaction to fall back on, it deliberately keeps it —
"it is dangerous to remove the transaction... it might have been broadcast" —
and the only retry lives in `find_base_tx`, which returns early once
`self._prevout` is unset. So a batch stranded that way is never rebroadcast and
never removed, and sits in the History tab as a "Local" row indefinitely.

That is upstream's behaviour, not this plugin's: nothing here creates,
broadcasts or removes transactions. What this plugin owes you is not to make it
worse. The old relabeller had no confirmation gate, so it dressed such a
transaction up as `⇄ Served … swap N sat` in the History tab while the Swap
Server tab — which *does* gate on height — left it out of the count, and the two
views disagreed about the same swap. Now the History tab does not describe it at
all, and the History (Swaps) tab names it `Local`, marks it red, explains the
stranded-batch case in its tooltip, and keeps it out of the net return.

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
