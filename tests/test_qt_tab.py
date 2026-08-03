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
import os
import sys
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
    from PyQt6.QtWidgets import QApplication, QTabWidget
    HAVE_QT = True
except ImportError:
    HAVE_QT = False

from swapserver_gui.swapserver_gui import AnnounceState, SwapServerGuiPlugin  # noqa: E402
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


@unittest.skipUnless(HAVE_QT, "PyQt6 not installed")
class QtTabTests(unittest.TestCase):

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
    # The filtering itself lives in test_served_swaps.py; these cover what the
    # operator actually reads off the Status tab.
    def _history_rows(self, tab):
        tree = tab.history_tree
        return [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]

    def test_history_counts_only_served_swaps(self):
        tab, _, _, _ = self._make_tab()
        history = [{'label': 'Forward swap', 'return_sat': 205, 'date': '2025-09-04',
                    'timestamp': 1756982141, 'num_served_swaps': 1,
                    'num_own_swaps': 0, 'is_mixed': False}]
        with mock.patch("swapserver_gui.qt.get_swap_history", return_value=history):
            tab.refresh()
        self.assertIn("Swaps served: 1", tab.summary_label.text())
        self.assertNotIn("batched", tab.summary_label.text())
        rows = self._history_rows(tab)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].text(1), 'Forward swap')
        self.assertEqual(rows[0].toolTip(1), "")

    def test_batched_row_is_flagged_not_hidden(self):
        """A group covering a served swap AND one of ours still counts, but its
        value is not purely server revenue, so the row has to say so."""
        tab, _, _, _ = self._make_tab()
        history = [{'label': 'Reverse swap', 'return_sat': 64, 'date': '2025-09-04',
                    'timestamp': 1756983236, 'num_served_swaps': 1,
                    'num_own_swaps': 1, 'is_mixed': True}]
        with mock.patch("swapserver_gui.qt.get_swap_history", return_value=history):
            tab.refresh()
        self.assertIn("1 batched with own swaps", tab.summary_label.text())
        rows = self._history_rows(tab)
        self.assertEqual(len(rows), 1)                 # reported, not dropped
        self.assertIn("batched", rows[0].text(1))
        self.assertEqual(rows[0].text(2), "64")        # value still shown in full
        self.assertIn("not purely", rows[0].toolTip(1))
        self.assertIn("not purely", rows[0].toolTip(2))

    def test_history_survives_a_failing_swap_manager(self):
        tab, _, _, _ = self._make_tab()
        with mock.patch("swapserver_gui.qt.get_swap_history",
                        side_effect=RuntimeError("boom")):
            tab.refresh()  # must not raise into the 4s timer


if __name__ == '__main__':
    unittest.main()
