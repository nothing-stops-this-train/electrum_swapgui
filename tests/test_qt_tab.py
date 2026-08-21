#!/usr/bin/env python3
"""Qt-layer tests for the Status/Diagnostics sub-tabs.

These build the *real* ``SwapServerTab`` against the offscreen QPA platform and
drive ``refresh()``, so the widget wiring is actually exercised rather than
merely imported.

PyQt6 is not installed in CI, so the whole module skips when it is missing --
the same compromise ``test_zip_plugin_load.py`` makes for ``qt.py``.  Run it
locally (or in any environment with PyQt6) to cover the GUI:

    python3 -m pytest tests/test_qt_tab.py

What is deliberately NOT covered here: the visual layout, and the live path
from the check button through the asyncio loop to a relay (that is
``test_discovery_e2e.py``'s job; here the report is injected directly).
"""
import concurrent.futures
import os
import sys
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

# A GUI test needs no display; ask Qt for the offscreen platform *before* it is
# imported anywhere.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication, QLabel, QTabWidget
    HAVE_QT = True
except ImportError:
    HAVE_QT = False

from swapserver_gui.swapserver_gui import (  # noqa: E402
    AnnounceState, ComponentKind, ServedSwapRow, SwapComponent, SwapRole,
    SwapServerGuiPlugin, SwapStatus,
)
from swapserver_gui import nostr_check as nc  # noqa: E402
from swapserver_gui.nostr_check import CheckStatus  # noqa: E402


PUBKEY_33 = bytes([0x02]) + bytes(range(32))


class _Config:
    def __init__(self, *, pow_target=12, nonce=0):
        self.SWAPSERVER_PORT = 5455
        self.NOSTR_RELAYS = "wss://a.example,wss://b.example"
        self.SWAPSERVER_FEE_MILLIONTHS = 5000
        self.SWAPSERVER_POW_TARGET = pow_target
        self.SWAPSERVER_ANN_POW_NONCE = nonce
        self.SWAPSERVER_GUI_AUTOSTART = False
        self.SWAPSERVER_GUI_POW_STATE = None

    def format_amount_and_units(self, sat):
        return f"{int(sat)} sat"


class _SwapManager:
    def __init__(self):
        self.is_server = False
        self.http_server = None
        self.network = None
        self.percentage = 0.5
        self._min_amount = 20000
        self._max_forward = 0
        self._max_reverse = 0
        self.mining_fee = 1000
        self._swaps = {}  # read by get_swap_history via the history refresh

    def server_update_pairs(self):
        pass


def _component(kind, title, *, txid=None, payment_hash=None, value_sat=None,
               in_wallet=True, expected_ours=True):
    return SwapComponent(kind=kind, title=title, txid=txid,
                         payment_hash=payment_hash, value_sat=value_sat,
                         in_wallet=in_wallet, detail="why this leg exists",
                         expected_ours=expected_ours)


def _row(*, label='⇄ Served forward swap 0.2 mBTC', return_sat=205,
         payment_hash='aa' * 32, batched_with=0, role=SwapRole.SERVED,
         missing_legs=(), ln_in_wallet=True, status=SwapStatus.FINAL,
         date='2025-09-04'):
    """A served forward swap: the taker funded, we claimed and paid lightning."""
    return ServedSwapRow(
        payment_hash=payment_hash,
        label=label,
        date=date,
        timestamp=1756982141,
        return_sat=return_sat,
        taker_is_forward=True,
        components=[
            _component(ComponentKind.LN_PAYMENT, "Lightning payment",
                       payment_hash=payment_hash,
                       value_sat=-99_000 if ln_in_wallet else None,
                       in_wallet=ln_in_wallet),
            # The taker funds a forward swap we serve, so this leg is never in
            # our history and its absence is not a gap.
            _component(ComponentKind.FUNDING_TX, "Funding transaction",
                       txid='tx_theirs', in_wallet=False, expected_ours=False),
            _component(ComponentKind.CLAIM_TX, "Claim transaction",
                       txid='tx_claim', value_sat=99_800),
        ],
        lockup_address='bc1qlockup',
        locktime=800_000,
        onchain_amount_sat=100_000,
        lightning_amount_sat=99_000,
        batched_with=batched_with,
        role=role,
        missing_legs=missing_legs,
        status=status,
    )


def _incomplete_row(**kw):
    """The shape behind the reported negative returns: a leg of ours missing."""
    kw.setdefault('return_sat', None)
    kw.setdefault('missing_legs', ("Lightning payment",))
    kw.setdefault('ln_in_wallet', False)
    return _row(**kw)


def _local_row(**kw):
    """A swap whose settling transaction never left the wallet."""
    kw.setdefault('status', SwapStatus.LOCAL)
    kw.setdefault('payment_hash', 'cc' * 32)
    kw.setdefault('label', '⇄ Served reverse swap 0.2 mBTC')
    return _row(**kw)


def _own_row(**kw):
    """One of the operator's own swaps: a cost, not a return."""
    kw.setdefault('role', SwapRole.OWN)
    kw.setdefault('payment_hash', 'bb' * 32)
    kw.setdefault('label', '↪ My reverse swap 0.2 mBTC')
    return _row(**kw)


class _TabHarness:
    """Builds a real SwapServerTab against the offscreen QPA platform."""

    @classmethod
    def setUpClass(cls):
        # exactly one QApplication per process
        cls.app = QApplication.instance() or QApplication([])

    def _make_tab(self, config=None, *, keypair=mock.sentinel.default):
        from swapserver_gui import qt as qt_mod
        config = config or _Config()
        plugin = SwapServerGuiPlugin(mock.MagicMock(), config, "swapserver_gui")
        # no asyncio loop in this harness; the GUI's periodic pairs update hops
        # onto it, which is covered by test_swapserver_gui.py instead.
        plugin.request_pairs_update = lambda: None
        sm = _SwapManager()
        wallet = mock.MagicMock()
        wallet.lnworker.swap_manager = sm
        wallet.lnworker.nostr_keypair = (
            mock.Mock(pubkey=PUBKEY_33) if keypair is mock.sentinel.default else keypair)
        wallet.lnworker.num_sats_can_send.return_value = 0
        wallet.lnworker.num_sats_can_receive.return_value = 0
        wallet.has_password.return_value = False
        wallet.get_full_history.return_value = {}
        plugin.bind_wallet(wallet)
        window = mock.MagicMock()
        window.wallet = wallet
        window.config = config
        tab = qt_mod.SwapServerTab(plugin, window)
        self.addCleanup(tab.clean_up)
        return tab, plugin, sm, window


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class QtTabTests(_TabHarness, unittest.TestCase):

    # ------------------------------------------------------------ structure
    def test_output_pane_has_status_and_diagnostics_subtabs(self):
        tab, _, _, _ = self._make_tab()
        self.assertIsInstance(tab.output_tabs, QTabWidget)
        titles = [tab.output_tabs.tabText(i) for i in range(tab.output_tabs.count())]
        self.assertEqual(titles, ["Status", "Diagnostics"])

    def test_settings_box_stays_outside_the_subtabs(self):
        """It must be reachable from both sub-tabs, so it cannot live in one."""
        tab, _, _, _ = self._make_tab()
        for i in range(tab.output_tabs.count()):
            page = tab.output_tabs.widget(i)
            self.assertFalse(page.isAncestorOf(tab.save_btn))
            self.assertFalse(page.isAncestorOf(tab.relays_edit))
        # ...but the diagnostics widgets do live inside a sub-tab
        diagnostics = tab.output_tabs.widget(1)
        self.assertTrue(diagnostics.isAncestorOf(tab.check_btn))
        status = tab.output_tabs.widget(0)
        self.assertTrue(status.isAncestorOf(tab.npub_label))

    # ------------------------------------------------------------- identity
    def test_both_encodings_are_shown_with_the_identicon(self):
        """npub AND hex: the provider list shows hex, SWAPSERVER_NPUB is npub,
        and showing only one reads as the GUI displaying the wrong key."""
        tab, plugin, _, _ = self._make_tab()
        expected_hex, expected_npub = plugin.nostr_identity()
        self.assertEqual(tab.npub_label.text(), expected_npub)
        self.assertTrue(tab.npub_label.text().startswith("npub1"))
        self.assertEqual(tab.pubkey_hex_label.text(), expected_hex)
        self.assertFalse(tab.npub_icon.pixmap().isNull())
        self.assertTrue(tab.npub_copy_btn.isEnabled())
        self.assertTrue(tab.pubkey_hex_copy_btn.isEnabled())

    def test_the_two_encodings_are_the_same_key(self):
        """Guards the actual confusion: they must never drift apart."""
        from electrum_aionostr.util import from_nip19
        tab, _, _, _ = self._make_tab()
        shown_npub = tab.npub_label.text()
        shown_hex = tab.pubkey_hex_label.text()
        self.assertEqual(from_nip19(shown_npub)['object'].hex(), shown_hex)
        # and the hex is exactly what SwapOffer.server_pubkey would carry:
        # NostrTransport.nostr_pubkey == keypair.pubkey.hex()[2:]
        self.assertEqual(shown_hex, PUBKEY_33.hex()[2:])

    def test_hex_copy_button_copies_the_hex(self):
        tab, plugin, _, window = self._make_tab()
        tab.on_copy_pubkey_hex()
        window.do_copy.assert_called_once()
        self.assertEqual(window.do_copy.call_args[0][0], plugin.nostr_identity()[0])

    def test_identity_is_shown_while_the_server_is_stopped(self):
        # the key is seed-derived; an operator must be able to read it before
        # ever starting the server
        tab, plugin, _, _ = self._make_tab()
        self.assertFalse(plugin.is_running())
        self.assertTrue(tab.npub_label.text().startswith("npub1"))

    def test_copy_button_copies_the_npub(self):
        tab, plugin, _, window = self._make_tab()
        tab.on_copy_npub()
        window.do_copy.assert_called_once()
        self.assertEqual(window.do_copy.call_args[0][0], plugin.nostr_identity()[1])

    def test_wallet_without_nostr_key_degrades_gracefully(self):
        tab, _, _, _ = self._make_tab(keypair=None)
        self.assertNotIn("npub1", tab.npub_label.text())
        self.assertEqual(tab.pubkey_hex_label.text(), "—")
        self.assertFalse(tab.npub_copy_btn.isEnabled())
        self.assertFalse(tab.pubkey_hex_copy_btn.isEnabled())
        # copying must be a no-op rather than pasting an empty string
        tab.on_copy_npub()
        tab.on_copy_pubkey_hex()

    def test_identicon_uses_the_hex_like_the_provider_list_does(self):
        """swap_dialog.py feeds x.server_pubkey (hex) to pubkey_to_q_icon; if we
        fed it the npub the colours would differ and the visual check would lie."""
        from electrum.gui.qt.util import pubkey_to_q_icon
        tab, plugin, _, _ = self._make_tab()
        pubkey_hex, npub = plugin.nostr_identity()
        expected = pubkey_to_q_icon(pubkey_hex).pixmap(16, 16).toImage()
        self.assertEqual(tab.npub_icon.pixmap().toImage(), expected)
        # upstream's colour helper only accepts the 64-char hex, so feeding it
        # the npub is not merely a different colour -- it is a hard error
        with self.assertRaises(AssertionError):
            pubkey_to_q_icon(npub)

    # --------------------------------------------------------- announcement
    def test_running_without_liquidity_does_not_claim_to_announce(self):
        """The reported symptom: 'running' everywhere, nothing published."""
        tab, plugin, sm, _ = self._make_tab()
        plugin._running = True
        sm._max_forward = 0
        sm._max_reverse = 0
        tab.refresh()
        self.assertIs(plugin.status()["announce_state"], AnnounceState.NO_LIQUIDITY)
        self.assertIn("no liquidity", tab._out_labels["nostr"].text())
        self.assertIn("Not announcing", tab.announce_label.text())
        self.assertIn("20,000 sat", tab.announce_reason.text())

    def test_with_liquidity_it_announces(self):
        tab, plugin, sm, _ = self._make_tab()
        plugin._running = True
        sm._max_forward = 150000
        tab.refresh()
        self.assertIn("announcing to 2 relay(s)", tab._out_labels["nostr"].text())
        self.assertIn("Announcing", tab.announce_label.text())

    def test_locked_wallet_is_reported(self):
        tab, plugin, sm, _ = self._make_tab()
        plugin.wallet.has_password.return_value = True
        plugin.wallet.get_unlocked_password.return_value = None
        plugin._running = True
        sm._max_forward = 150000
        tab.refresh()
        self.assertIn("unlock", tab._out_labels["nostr"].text())
        self.assertIn("password", tab.announce_reason.text())

    # ----------------------------------------------- last-announcement report
    def _announcing_tab(self, **plugin_state):
        tab, plugin, sm, _ = self._make_tab()
        plugin._running = True
        sm._max_forward = 150000
        for name, value in plugin_state.items():
            setattr(plugin, name, value)
        return tab, plugin, sm

    def test_never_announced_says_so(self):
        tab, _, _ = self._announcing_tab()
        tab.refresh()
        self.assertIn("has not announced yet", tab.last_publish_label.text())

    def test_reports_the_acknowledged_announcement(self):
        # The reported bug: this line only ever showed the moment the server was
        # started, so a healthy server that had been up for days reported its
        # last announcement as days old. It must report the last publish a relay
        # actually acknowledged.
        tab, _, _ = self._announcing_tab(
            _last_publish_attempt_at=time.time() - 120,
            _last_publish_success_at=time.time() - 120,
            _last_publish_note="accepted by a relay")
        tab.refresh()
        text = tab.last_publish_label.text()
        self.assertIn("accepted by a relay", text)
        self.assertIn("2 min ago", text)
        # a successful attempt is not also reported separately as an "attempt"
        self.assertNotIn("last attempt", text)

    def test_a_failed_attempt_after_a_success_is_shown_separately(self):
        tab, _, _ = self._announcing_tab(
            _last_publish_success_at=time.time() - 600,
            _last_publish_attempt_at=time.time() - 30,
            _last_publish_note="failed: TimeoutError: no relay acknowledged")
        tab.refresh()
        text = tab.last_publish_label.text()
        self.assertIn("Last announcement accepted by a relay: 10 min ago", text)
        self.assertIn("last attempt 30s ago", text)
        self.assertIn("TimeoutError", text)

    def test_a_stale_announcement_is_flagged_even_while_announcing(self):
        # Everything else can look healthy while the offer has quietly expired
        # on the relays; the age is the only thing that gives it away.
        interval = SwapServerGuiPlugin.REPUBLISH_INTERVAL_SEC
        tab, plugin, _ = self._announcing_tab(
            _last_publish_success_at=time.time() - 3 * interval,
            _last_publish_attempt_at=time.time() - 3 * interval)
        tab.refresh()
        self.assertIs(plugin.status()["announce_state"], AnnounceState.ANNOUNCING)
        self.assertIn("⚠", tab.announce_reason.text())
        self.assertIn("longer than", tab.announce_reason.text())

    def test_a_fresh_announcement_is_not_flagged(self):
        tab, _, _ = self._announcing_tab(
            _last_publish_success_at=time.time() - 10,
            _last_publish_attempt_at=time.time() - 10)
        tab.refresh()
        self.assertNotIn("⚠", tab.announce_reason.text())

    # ------------------------------------------------- announce-loop failures
    def test_loop_failure_states_are_rendered(self):
        # Each of these used to render as "announcing to 2 relay(s)", which is
        # the worst possible answer: the operator believes they are visible.
        dead_fut = concurrent.futures.Future()
        dead_fut.set_exception(RuntimeError("boom"))
        cases = [
            ("no relay reachable", AnnounceState.NO_RELAY_CONNECTED,
             dict(_relay_connected=False)),
            ("relays reject the offer", AnnounceState.PUBLISH_FAILING,
             dict(_consecutive_publish_failures=3)),
            ("task died", AnnounceState.TASK_DEAD,
             dict(_nostr_fut=dead_fut)),
        ]
        for expected, state, plugin_state in cases:
            with self.subTest(state=state):
                tab, plugin, _ = self._announcing_tab(**plugin_state)
                # supervise() would resurrect the dead task on a real loop
                plugin.supervise = lambda: None
                tab.refresh()
                self.assertIs(plugin.status()["announce_state"], state)
                self.assertIn("Not announcing", tab.announce_label.text())
                self.assertIn(expected, tab._out_labels["nostr"].text())
                self.assertTrue(tab.announce_reason.text())

    def test_refresh_supervises_the_announce_task(self):
        tab, plugin, _ = self._announcing_tab()
        with mock.patch.object(plugin, "supervise") as supervise:
            tab.refresh()
        supervise.assert_called_once()

    # ---------------------------------------------------------- match fields
    def test_match_fields_are_rendered(self):
        tab, plugin, _, _ = self._make_tab()
        tab.refresh()
        fields = plugin.nostr_match_fields()
        self.assertEqual(tab._match_labels["net"].text(), fields["r_tag"])
        self.assertEqual(tab._match_labels["version"].text(), fields["d_tag"])
        self.assertEqual(tab._match_labels["kind"].text(), str(fields["kind"]))

    def test_low_pow_warning_appears_and_clears(self):
        tab, plugin, _, _ = self._make_tab()
        tab.refresh()
        # nonce 0 -> 0 bits of work, far below the default taker target of 30
        self.assertFalse(tab.pow_warning.isHidden())
        self.assertIn(str(nc.DEFAULT_TAKER_POW_TARGET), tab.pow_warning.text())
        self.assertIn("0 bits", tab._match_labels["pow"].text())

        with mock.patch.object(
                sys.modules['swapserver_gui.pow'], 'pow_bits', return_value=30):
            tab.refresh()
        self.assertTrue(tab.pow_warning.isHidden())
        self.assertIn("30 bits", tab._match_labels["pow"].text())

    # ----------------------------------------------------------- the report
    def _report(self, results, *, taker_pow_target=30):
        report = nc.DiscoveryReport(
            pubkey_hex="aa" * 32, npub="npub1x", net_name="signet",
            event_version=5, kind=30315, taker_pow_target=taker_pow_target)
        report.results = results
        return report

    def test_report_renders_one_row_per_relay(self):
        tab, _, _, _ = self._make_tab()
        tab.on_check_finished(self._report([
            nc.RelayResult("wss://a", CheckStatus.DISCOVERABLE, "announced 2 min ago", pow_bits=30),
            nc.RelayResult("wss://b", CheckStatus.LOW_POW, "only 12 bits", pow_bits=12),
        ]))
        self.assertIn("Discoverable on 1 of 2", tab.check_headline.text())
        rows = [(tab.check_tree.topLevelItem(i).text(0),
                 tab.check_tree.topLevelItem(i).text(1),
                 tab.check_tree.topLevelItem(i).text(2))
                for i in range(tab.check_tree.topLevelItemCount())]
        self.assertEqual(rows[0], ("wss://a", "discoverable", "announced 2 min ago"))
        self.assertEqual(rows[1], ("wss://b", "low pow", "only 12 bits"))

    def test_report_warnings_toggle(self):
        tab, _, _, _ = self._make_tab()
        low = nc.RelayResult("wss://a", CheckStatus.DISCOVERABLE, "ok", pow_bits=12)
        tab.on_check_finished(self._report([low], taker_pow_target=12))
        self.assertFalse(tab.check_warnings.isHidden())
        good = nc.RelayResult("wss://a", CheckStatus.DISCOVERABLE, "ok", pow_bits=30)
        tab.on_check_finished(self._report([good], taker_pow_target=30))
        self.assertTrue(tab.check_warnings.isHidden())

    def test_check_failure_is_surfaced_not_swallowed(self):
        tab, _, _, window = self._make_tab()
        tab._check_running = True
        tab.check_btn.setEnabled(False)
        tab.on_check_finished(RuntimeError("boom"))
        self.assertTrue(tab.check_btn.isEnabled())
        window.show_error.assert_called_once()
        self.assertIn("boom", window.show_error.call_args[0][0])

    def test_result_landing_after_teardown_is_dropped(self):
        """The check outlives the refresh timer, so it must not emit into a
        tab that close_wallet already tore down."""
        import concurrent.futures
        tab, plugin, _, _ = self._make_tab()
        fut = concurrent.futures.Future()
        plugin.check_discoverability = lambda: fut
        plugin.cancel_discovery_check = lambda: None  # simulate "too late to cancel"
        tab.on_check_discoverability()
        self.assertTrue(tab._check_running)
        self.assertIn("Querying", tab.check_headline.text())

        # spy on the signal itself: the done-callback's only job is to emit it
        seen = []
        tab.checkFinished.connect(seen.append)

        tab.clean_up()
        fut.set_result(self._report([]))  # fires the done-callback
        self.app.processEvents()
        self.assertEqual(seen, [])
        # and nothing repainted the torn-down tab
        self.assertIn("Querying", tab.check_headline.text())

    def test_button_reports_missing_prerequisites(self):
        config = _Config()
        config.NOSTR_RELAYS = ""
        tab, _, _, window = self._make_tab(config)
        tab.on_check_discoverability()
        window.show_error.assert_called_once()
        self.assertIn("relay", window.show_error.call_args[0][0])
        self.assertTrue(tab.check_btn.isEnabled())  # not left stuck on "Checking…"

    # --------------------------------------------------------------- history
    # Which swaps are counted, and what each row's value means, is pinned down
    # in test_served_swaps.py; these cover what the operator actually reads off
    # the Status tab and what a double-click does.
    def _history_rows(self, tab):
        tree = tab.history_tree
        return [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]

    def test_history_shows_one_row_per_swap(self):
        tab, _, _, _ = self._make_tab()
        rows = [_row(label='⇄ Served forward swap 0.2 mBTC', return_sat=205)]
        with mock.patch("swapserver_gui.qt.build_served_swap_rows", return_value=rows):
            tab.refresh()
        self.assertIn("Swaps served: 1", tab.summary_label.text())
        self.assertNotIn("shared transaction", tab.summary_label.text())
        self.assertNotIn("Not counted", tab.summary_label.text())
        items = self._history_rows(tab)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].text(1), '⇄ Served forward swap 0.2 mBTC')
        self.assertEqual(items[0].text(2), '205')
        self.assertIn("component", items[0].toolTip(1))

    def test_a_row_with_a_missing_leg_shows_no_number(self):
        """No partial sum reaches the screen; it would read as a real result."""
        tab, _, _, _ = self._make_tab()
        with mock.patch("swapserver_gui.qt.build_served_swap_rows",
                        return_value=[_incomplete_row()]):
            tab.refresh()
        item = self._history_rows(tab)[0]
        self.assertNotIn("205", item.text(2))
        self.assertIn("incomplete", item.text(2))
        self.assertIn("Lightning payment", item.toolTip(2))
        self.assertIn("unknown, not zero", item.toolTip(2))

    def test_the_summary_says_what_it_left_out(self):
        tab, _, _, _ = self._make_tab()
        rows = [_row(return_sat=205),
                _incomplete_row(payment_hash='bb' * 32),
                _row(payment_hash='cc' * 32, return_sat=99, role=SwapRole.UNKNOWN)]
        with mock.patch("swapserver_gui.qt.build_served_swap_rows", return_value=rows):
            tab.refresh()
        text = tab.summary_label.text()
        self.assertIn("Swaps served: 3", text)
        self.assertIn("Not counted", text)
        self.assertIn("incomplete", text)
        self.assertIn("unattributed", text)

    def test_an_unattributed_row_says_so_and_is_not_counted(self):
        tab, _, _, _ = self._make_tab()
        row = _row(label='⇄? Unattributed forward swap 0.2 mBTC', return_sat=99,
                   role=SwapRole.UNKNOWN)
        with mock.patch("swapserver_gui.qt.build_served_swap_rows",
                        return_value=[row]):
            tab.refresh()
        item = self._history_rows(tab)[0]
        self.assertIn("Unattributed", item.text(1))
        self.assertIn("which side", item.toolTip(2))

    def test_two_swaps_sharing_a_transaction_are_two_rows(self):
        """The whole point of the rewrite: a batch tx is not one row."""
        tab, _, _, _ = self._make_tab()
        rows = [_row(payment_hash='aa' * 32, return_sat=900, batched_with=1),
                _row(payment_hash='bb' * 32, return_sat=400, batched_with=1)]
        with mock.patch("swapserver_gui.qt.build_served_swap_rows", return_value=rows):
            tab.refresh()
        items = self._history_rows(tab)
        self.assertEqual(len(items), 2)
        self.assertIn("2 settled in a shared transaction", tab.summary_label.text())
        # the value shown is the swap's own share, so the note explains the
        # shared transaction rather than disclaiming the number
        self.assertIn("this swap's own share", items[0].toolTip(2))

    def test_history_survives_a_failing_swap_manager(self):
        tab, _, _, _ = self._make_tab()
        with mock.patch("swapserver_gui.qt.build_served_swap_rows",
                        side_effect=RuntimeError("boom")):
            tab.refresh()  # must not raise into the 4s timer

    def test_double_clicking_a_row_opens_the_component_window(self):
        tab, _, _, _ = self._make_tab()
        rows = [_row()]
        with mock.patch("swapserver_gui.qt.build_served_swap_rows", return_value=rows):
            tab.refresh()
        tab.on_history_row_activated(self._history_rows(tab)[0], 0)
        self.assertEqual(len(tab._detail_dialogs), 1)
        dialog = tab._detail_dialogs[0]
        self.assertIs(dialog.row, rows[0])
        # It must not be modal: the window it sends the user to is the one a
        # window-modal dialog would block.
        self.assertEqual(dialog.windowModality(), Qt.WindowModality.NonModal)

    def test_closing_the_component_window_drops_our_reference(self):
        tab, _, _, _ = self._make_tab()
        with mock.patch("swapserver_gui.qt.build_served_swap_rows", return_value=[_row()]):
            tab.refresh()
        tab.on_history_row_activated(self._history_rows(tab)[0], 0)
        tab._detail_dialogs[0].close()
        self.assertEqual(tab._detail_dialogs, [])

    def test_tearing_down_the_tab_closes_open_component_windows(self):
        tab, _, _, _ = self._make_tab()
        with mock.patch("swapserver_gui.qt.build_served_swap_rows", return_value=[_row()]):
            tab.refresh()
        tab.on_history_row_activated(self._history_rows(tab)[0], 0)
        tab.clean_up()
        self.assertEqual(tab._detail_dialogs, [])


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class SwapComponentsDialogTests(_TabHarness, unittest.TestCase):
    """The window a double-click opens: every leg of one swap."""

    def _dialog(self, row=None):
        from swapserver_gui import qt as qt_mod
        tab, _, _, window = self._make_tab()
        dialog = qt_mod.SwapComponentsDialog(tab, row or _row())
        self.addCleanup(dialog.deleteLater)
        return dialog, window

    def _component_items(self, dialog):
        tree = dialog.tree
        return [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]

    def test_lists_every_component(self):
        dialog, _ = self._dialog()
        titles = [item.text(0) for item in self._component_items(dialog)]
        self.assertEqual(titles, ["Lightning payment", "Funding transaction",
                                  "Claim transaction"])

    def test_a_leg_the_taker_made_is_marked_as_such(self):
        dialog, _ = self._dialog()
        by_title = {i.text(0): i for i in self._component_items(dialog)}
        self.assertEqual(by_title["Funding transaction"].text(2), "made by the taker")
        self.assertEqual(by_title["Claim transaction"].text(2),
                         "in this wallet's history")

    def test_clicking_our_leg_goes_to_the_history_tab(self):
        dialog, _ = self._dialog()
        item = {i.text(0): i for i in self._component_items(dialog)}["Claim transaction"]
        with mock.patch("swapserver_gui.qt.show_in_history", return_value=True) as go:
            dialog.on_component_activated(item, 0)
        go.assert_called_once()
        self.assertEqual(go.call_args.kwargs["txid"], "tx_claim")
        self.assertFalse(dialog.status_label.isVisible())

    def test_clicking_the_takers_leg_explains_why_nothing_happens(self):
        dialog, _ = self._dialog()
        item = {i.text(0): i for i in self._component_items(dialog)}["Funding transaction"]
        with mock.patch("swapserver_gui.qt.show_in_history") as go:
            dialog.on_component_activated(item, 0)
        go.assert_not_called()
        self.assertIn("not in this wallet", dialog.status_label.text())

    def test_a_leg_of_ours_that_is_missing_reads_differently(self):
        # "made by the taker" is normal; "missing from this wallet" is the gap
        # that costs the row its return, so the two must not look the same.
        dialog, _ = self._dialog(_incomplete_row())
        by_title = {i.text(0): i for i in self._component_items(dialog)}
        self.assertEqual(by_title["Lightning payment"].text(2),
                         "missing from this wallet")
        self.assertEqual(by_title["Funding transaction"].text(2),
                         "made by the taker")

    def test_clicking_a_missing_leg_says_why_there_is_no_return(self):
        dialog, _ = self._dialog(_incomplete_row())
        item = {i.text(0): i for i in self._component_items(dialog)}["Lightning payment"]
        with mock.patch("swapserver_gui.qt.show_in_history") as go:
            dialog.on_component_activated(item, 0)
        go.assert_not_called()
        self.assertIn("no return", dialog.status_label.text())

    def test_the_summary_box_withholds_the_return_and_says_why(self):
        dialog, _ = self._dialog(_incomplete_row())
        labels = [w.text() for w in dialog.findChildren(QLabel)]
        self.assertTrue(any("incomplete" in t for t in labels), labels)
        self.assertTrue(any("Lightning payment" in t and "unknown, not zero" in t
                            for t in labels), labels)

    def test_an_unattributed_swap_explains_itself(self):
        dialog, _ = self._dialog(_row(role=SwapRole.UNKNOWN))
        labels = [w.text() for w in dialog.findChildren(QLabel)]
        self.assertTrue(any("which side" in t for t in labels), labels)

    def test_a_leg_that_cannot_be_found_says_so(self):
        dialog, _ = self._dialog()
        item = {i.text(0): i for i in self._component_items(dialog)}["Claim transaction"]
        with mock.patch("swapserver_gui.qt.show_in_history", return_value=False):
            dialog.on_component_activated(item, 0)
        self.assertIn("Could not find", dialog.status_label.text())

    def test_a_shared_transaction_is_explained(self):
        dialog, _ = self._dialog(_row(batched_with=2))
        texts = [w.text() for w in dialog.findChildren(QLabel)]
        self.assertTrue(any("2 other" in t for t in texts), texts)


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class ShowInHistoryTests(unittest.TestCase):
    """Selecting a transaction in Electrum's History tab.

    ``history_list.py`` has no plugin hook, so this drives HistoryModel's tree
    directly.  The stand-in below is a real ``CustomModel`` -- the same class
    HistoryModel is built on -- so the index arithmetic is the real thing.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _window(self, top_level):
        """A window whose History tab holds ``top_level`` = [(item, children)]."""
        from electrum.gui.qt.custom_model import CustomModel, CustomNode
        from PyQt6.QtCore import QSortFilterProxyModel
        from PyQt6.QtWidgets import QTreeView, QVBoxLayout, QWidget

        class _Node(CustomNode):
            def get_data_for_role(self, index, role):
                return None

        model = CustomModel(None, 3)
        for item, children in top_level:
            node = _Node(model, item)
            model._root.addChild(node)
            for child in children:
                node.addChild(_Node(model, child))
        proxy = QSortFilterProxyModel()
        proxy.setSourceModel(model)
        view = QTreeView()
        view.setModel(proxy)

        window = QWidget()
        self.addCleanup(window.deleteLater)
        tabs = QTabWidget(window)
        page = QWidget()
        QVBoxLayout(page).addWidget(view)  # the list is nested, as in Electrum
        tabs.addTab(QWidget(), "Send")
        tabs.addTab(page, "History")
        window.tabs = tabs
        window.history_list = view
        window.history_model = model
        return window

    def test_finds_a_top_level_transaction_and_switches_tab(self):
        from swapserver_gui import qt as qt_mod
        window = self._window([({'txid': 'tx1', 'lightning': False}, [])])
        self.assertTrue(qt_mod.show_in_history(window, txid='tx1'))
        self.assertEqual(window.tabs.currentIndex(), 1)
        index = window.history_list.currentIndex()
        self.assertEqual(index.row(), 0)
        # CustomModel.parent() hands back a *valid* index for a top-level row
        # (it points at the root node), so "has a parent" is not the test for
        # "is inside a group" -- nothing here should have been expanded.
        self.assertFalse(window.history_list.isExpanded(index))

    def test_finds_a_transaction_folded_into_a_group(self):
        from swapserver_gui import qt as qt_mod
        window = self._window([
            ({'txid': '----', 'lightning': False},
             [{'txid': 'tx_child', 'lightning': False},
              {'payment_hash': 'ph1', 'lightning': True}]),
        ])
        self.assertTrue(qt_mod.show_in_history(window, txid='tx_child'))
        index = window.history_list.currentIndex()
        self.assertTrue(index.parent().isValid())  # it really is the child row
        self.assertTrue(window.history_list.isExpanded(index.parent()))

    def test_finds_a_lightning_payment_by_hash(self):
        from swapserver_gui import qt as qt_mod
        window = self._window([({'payment_hash': 'ph1', 'lightning': True}, [])])
        self.assertTrue(qt_mod.show_in_history(window, payment_hash='ph1'))

    def test_missing_transaction_reports_failure(self):
        from swapserver_gui import qt as qt_mod
        window = self._window([({'txid': 'tx1', 'lightning': False}, [])])
        self.assertFalse(qt_mod.show_in_history(window, txid='nope'))

    def test_window_without_a_history_tab(self):
        from swapserver_gui import qt as qt_mod
        self.assertFalse(qt_mod.show_in_history(mock.Mock(spec=[]), txid='tx1'))

    def test_tab_lookup_walks_up_from_a_nested_widget(self):
        from swapserver_gui import qt as qt_mod
        window = self._window([({'txid': 'tx1', 'lightning': False}, [])])
        self.assertEqual(
            qt_mod._tab_index_for_widget(window.tabs, window.history_list), 1)
        self.assertEqual(qt_mod._tab_index_for_widget(window.tabs, None), -1)


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class SwapHistoryTabTests(_TabHarness, unittest.TestCase):
    """The "History (Swaps)" tab: the wallet's history, reorganised per swap.

    The reason this tab exists is that Electrum's own History tab is now left
    exactly as upstream renders it, so everything the operator used to read
    from relabelled rows has to be readable here instead.
    """

    #: mirrors SwapHistoryTab.COL_*
    DATE, STATUS, LABEL, RETURN = range(4)

    def _make_history_tab(self, rows):
        from swapserver_gui import qt as qt_mod
        _tab, plugin, _sm, window = self._make_tab()
        with mock.patch("swapserver_gui.qt.build_swap_rows", return_value=rows):
            tab = qt_mod.SwapHistoryTab(plugin, window)
        self.addCleanup(tab.clean_up)
        return tab, window

    def _top_items(self, tab):
        tree = tab.swap_tree
        return [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]

    def _by_label(self, tab):
        return {item.text(self.LABEL): item for item in self._top_items(tab)}

    # ------------------------------------------------------------------ shape
    def test_one_top_level_row_per_swap_with_its_components_underneath(self):
        tab, _ = self._make_history_tab([_row(), _own_row()])
        items = self._top_items(tab)
        self.assertEqual(len(items), 2)
        for item in items:
            self.assertEqual(item.childCount(), 3)
        titles = [items[0].child(i).text(self.LABEL) for i in range(3)]
        self.assertEqual(titles, ["Lightning payment", "Funding transaction",
                                  "Claim transaction"])

    def test_newest_swap_first(self):
        old = _row(payment_hash='11' * 32, date='2025-01-01')
        new = _row(payment_hash='22' * 32, date='2025-09-04')
        tab, _ = self._make_history_tab([old, new])  # builder returns oldest first
        self.assertEqual(self._top_items(tab)[0].text(self.DATE), '2025-09-04')

    def test_own_swaps_are_listed_alongside_served_ones(self):
        tab, _ = self._make_history_tab([_row(), _own_row()])
        labels = list(self._by_label(tab))
        self.assertTrue(any(l.startswith('⇄') for l in labels), labels)
        self.assertTrue(any(l.startswith('↪') for l in labels), labels)

    def test_it_asks_for_own_and_pending_swaps(self):
        # The Swap Server tab's table deliberately asks for neither.
        from swapserver_gui import qt as qt_mod
        _tab, plugin, _sm, window = self._make_tab()
        with mock.patch("swapserver_gui.qt.build_swap_rows",
                        return_value=[]) as build:
            tab = qt_mod.SwapHistoryTab(plugin, window)
            self.addCleanup(tab.clean_up)
        self.assertEqual(build.call_args.kwargs,
                         {'include_own': True, 'include_pending': True})

    # ----------------------------------------------------------- the columns
    def test_the_status_column_names_the_state(self):
        tab, _ = self._make_history_tab([_row(), _local_row()])
        by_label = self._by_label(tab)
        self.assertEqual(by_label['⇄ Served forward swap 0.2 mBTC'].text(self.STATUS),
                         "Confirmed")
        self.assertEqual(by_label['⇄ Served reverse swap 0.2 mBTC'].text(self.STATUS),
                         "Local")

    def test_a_local_swap_is_the_one_status_marked_in_red(self):
        # It is the only one that is a problem rather than a wait, and the
        # operator cannot tell it apart in Electrum's History tab.
        from electrum.gui.qt.util import ColorScheme
        tab, _ = self._make_history_tab([_row(), _local_row()])
        by_label = self._by_label(tab)
        local = by_label['⇄ Served reverse swap 0.2 mBTC']
        settled = by_label['⇄ Served forward swap 0.2 mBTC']
        self.assertEqual(local.foreground(self.STATUS).color(),
                         ColorScheme.RED.as_color())
        self.assertNotEqual(settled.foreground(self.STATUS).color(),
                            ColorScheme.RED.as_color())

    def test_the_four_silences_in_the_return_column_read_differently(self):
        # A swap that has not settled, one of the operator's own, and one with a
        # missing leg are three different things; printing "—" for all of them
        # would hide which one the operator is looking at.
        rows = [_row(), _own_row(),
                _incomplete_row(payment_hash='dd' * 32,
                                label='⇄ Served forward swap 0.3 mBTC'),
                _local_row()]
        tab, _ = self._make_history_tab(rows)
        returns = {item.text(self.LABEL): item.text(self.RETURN)
                   for item in self._top_items(tab)}
        self.assertEqual(len(returns), 4, returns)
        self.assertEqual(returns['⇄ Served forward swap 0.2 mBTC'], "205 sat")
        self.assertEqual(returns['↪ My reverse swap 0.2 mBTC'], "— own swap")
        self.assertEqual(returns['⇄ Served reverse swap 0.2 mBTC'], "—")
        self.assertEqual(returns['⇄ Served forward swap 0.3 mBTC'], "— incomplete")

    def test_a_swap_with_no_date_prints_a_dash_not_1970(self):
        tab, _ = self._make_history_tab([_row(date='', status=SwapStatus.IN_FLIGHT)])
        self.assertEqual(self._top_items(tab)[0].text(self.DATE), "—")

    def test_components_say_whose_leg_they_are(self):
        tab, _ = self._make_history_tab([_row()])
        item = self._top_items(tab)[0]
        locations = {item.child(i).text(self.LABEL): item.child(i).text(self.STATUS)
                     for i in range(item.childCount())}
        self.assertEqual(locations["Claim transaction"], "in this wallet")
        self.assertEqual(locations["Funding transaction"], "the taker's")

    def test_a_missing_leg_of_ours_reads_differently_from_the_takers(self):
        tab, _ = self._make_history_tab([_incomplete_row()])
        item = self._top_items(tab)[0]
        locations = {item.child(i).text(self.LABEL): item.child(i).text(self.STATUS)
                     for i in range(item.childCount())}
        self.assertEqual(locations["Lightning payment"], "missing")
        self.assertEqual(locations["Funding transaction"], "the taker's")

    # ------------------------------------------------------------- the totals
    def test_the_summary_counts_own_swaps_in_and_says_so(self):
        tab, _ = self._make_history_tab([_row(), _own_row()])
        text = tab.summary_label.text()
        self.assertNotIn("Swaps served:", text)
        self.assertIn("served and own", text)

    def test_the_summary_leaves_an_unsettled_swap_out_of_the_return(self):
        tab, _ = self._make_history_tab([_row(), _local_row()])
        text = tab.summary_label.text()
        self.assertIn("205 sat", text)          # only the settled one
        self.assertIn("1 not settled yet", text)

    def test_a_local_row_explains_itself_in_its_tooltip(self):
        tab, _ = self._make_history_tab([_local_row()])
        tip = self._top_items(tab)[0].toolTip(self.LABEL)
        self.assertIn("never broadcast", tip)

    # --------------------------------------------------------- interaction
    def test_double_clicking_a_component_goes_to_the_history_tab(self):
        tab, _ = self._make_history_tab([_row()])
        item = self._top_items(tab)[0]
        claim = [item.child(i) for i in range(item.childCount())
                 if item.child(i).text(self.LABEL) == "Claim transaction"][0]
        with mock.patch("swapserver_gui.qt.show_in_history",
                        return_value=True) as go:
            tab.on_item_activated(claim, 0)
        go.assert_called_once()
        self.assertEqual(go.call_args.kwargs["txid"], "tx_claim")

    def test_double_clicking_the_takers_leg_does_nothing(self):
        tab, _ = self._make_history_tab([_row()])
        item = self._top_items(tab)[0]
        theirs = [item.child(i) for i in range(item.childCount())
                  if item.child(i).text(self.LABEL) == "Funding transaction"][0]
        with mock.patch("swapserver_gui.qt.show_in_history") as go:
            tab.on_item_activated(theirs, 0)
        go.assert_not_called()

    def test_double_clicking_a_swap_opens_its_detail_window(self):
        tab, _ = self._make_history_tab([_row()])
        item = self._top_items(tab)[0]
        tab.on_item_activated(item, 0)
        self.assertEqual(len(tab._detail_dialogs), 1)
        dialog = tab._detail_dialogs[0]
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.row.payment_hash, 'aa' * 32)

    def test_a_refresh_keeps_the_row_the_operator_expanded_open(self):
        # The table rebuilds itself every few seconds; collapsing the row being
        # read would make the components unreachable in practice.
        rows = [_row()]
        tab, _ = self._make_history_tab(rows)
        self._top_items(tab)[0].setExpanded(True)
        with mock.patch("swapserver_gui.qt.build_swap_rows", return_value=rows):
            tab.refresh()
        self.assertTrue(self._top_items(tab)[0].isExpanded())

    def test_a_failing_builder_leaves_the_last_good_table_alone(self):
        tab, _ = self._make_history_tab([_row()])
        with mock.patch("swapserver_gui.qt.build_swap_rows",
                        side_effect=RuntimeError("wallet went away")):
            tab.refresh()  # must not raise
        self.assertEqual(len(self._top_items(tab)), 1)

    def test_clean_up_stops_the_timer(self):
        tab, _ = self._make_history_tab([_row()])
        self.assertTrue(tab._timer.isActive())
        tab.clean_up()
        self.assertIsNone(tab._timer)
        tab.clean_up()  # idempotent


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class TabRegistrationTests(_TabHarness, unittest.TestCase):
    """Both tabs go in, both come out, and Electrum's History tab is untouched."""

    def _plugin_with_window(self):
        from swapserver_gui import qt as qt_mod
        config = _Config()
        plugin = qt_mod.Plugin(mock.MagicMock(), config, "swapserver_gui")
        plugin.request_pairs_update = lambda: None
        wallet = mock.MagicMock()
        wallet.lnworker.swap_manager = _SwapManager()
        wallet.lnworker.nostr_keypair = mock.Mock(pubkey=PUBKEY_33)
        wallet.lnworker.num_sats_can_send.return_value = 0
        wallet.lnworker.num_sats_can_receive.return_value = 0
        wallet.has_password.return_value = False
        wallet.get_full_history.return_value = {}
        window = mock.MagicMock()
        window.wallet = wallet
        window.config = config
        window.tabs = QTabWidget()
        self.addCleanup(window.tabs.deleteLater)
        return plugin, window

    def test_both_tabs_are_added(self):
        plugin, window = self._plugin_with_window()
        plugin.bind_wallet(window.wallet)
        plugin._add_tab(window)
        self.addCleanup(plugin._remove_tab)
        titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
        self.assertEqual(titles, ["Swap Server", "History (Swaps)"])

    def test_removing_takes_both_out_and_stops_both_timers(self):
        plugin, window = self._plugin_with_window()
        plugin.bind_wallet(window.wallet)
        plugin._add_tab(window)
        history_tab = plugin._history_tab
        plugin._remove_tab()
        self.assertEqual(window.tabs.count(), 0)
        self.assertIsNone(plugin._history_tab)
        self.assertIsNone(history_tab._timer)

    def test_binding_a_wallet_does_not_touch_electrums_history(self):
        # This plugin used to wrap SwapManager.get_groups_for_onchain_history in
        # order to rewrite rows in Electrum's own History tab. It must not any
        # more: that tab is upstream's, and reorganising it is this plugin's
        # "History (Swaps)" tab's job.
        plugin, window = self._plugin_with_window()
        sm = window.wallet.lnworker.swap_manager
        before = dict(sm.__dict__)
        plugin.bind_wallet(window.wallet)
        self.addCleanup(plugin.unbind_wallet)
        self.assertEqual(dict(sm.__dict__), before)
        self.assertFalse(hasattr(sm, 'get_groups_for_onchain_history'))


if __name__ == '__main__':
    unittest.main()
