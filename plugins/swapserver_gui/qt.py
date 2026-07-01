#!/usr/bin/env python
#
# swapserver_gui - a Qt GUI plugin for Electrum's submarine swap server.
# This file is released into the public domain (The Unlicense); see LICENSE.
#
# Qt layer: injects a "Swap Server" tab into the main window and wires its
# controls to the transport lifecycle implemented in ``swapserver_gui.py``.

from typing import TYPE_CHECKING, Optional, Dict, Any

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout, QGroupBox,
    QLabel, QPushButton, QSpinBox, QPlainTextEdit, QTreeWidget, QTreeWidgetItem,
    QSizePolicy,
)

from electrum.i18n import _
from electrum.plugin import hook
from electrum.gui.qt.util import read_QIcon

from .swapserver_gui import (
    SwapServerGuiPlugin, SwapServerError, get_swap_history, get_swap_summary,
)

if TYPE_CHECKING:
    from electrum.wallet import Abstract_Wallet
    from electrum.gui.qt.main_window import ElectrumWindow


def _fmt_sat(config, sat: Optional[int]) -> str:
    if sat is None:
        return "—"
    try:
        return config.format_amount_and_units(int(sat))
    except Exception:
        return f"{sat} sat"


class SwapServerTab(QWidget):
    """The 'Swap Server' tab: enable/disable, settings, and live output."""

    def __init__(self, plugin: 'Plugin', window: 'ElectrumWindow') -> None:
        QWidget.__init__(self)
        self.plugin = plugin
        self.window = window
        self.config = plugin.config
        self.wallet = window.wallet

        root = QVBoxLayout(self)

        # ---- header: status + enable/disable toggle -----------------------
        header = QHBoxLayout()
        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.TextFormat.RichText)
        header.addWidget(self.status_label, 1)
        self.toggle_btn = QPushButton()
        self.toggle_btn.clicked.connect(self.on_toggle)
        header.addWidget(self.toggle_btn)
        root.addLayout(header)

        body = QHBoxLayout()
        root.addLayout(body, 1)
        body.addWidget(self._build_settings_group())
        body.addWidget(self._build_output_group(), 1)

        # ---- periodic refresh --------------------------------------------
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(4000)

        self.load_settings_into_widgets()
        self.refresh()

    # -------------------------------------------------------------- widgets
    def _build_settings_group(self) -> QGroupBox:
        box = QGroupBox(_("Settings"))
        box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        form = QFormLayout(box)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(0, 65535)
        self.port_spin.setSpecialValueText(_("disabled"))
        self.port_spin.setToolTip(_("Local HTTP port for the swap server. 0 disables the HTTP endpoint."))
        form.addRow(_("HTTP port:"), self.port_spin)

        self.fee_spin = QSpinBox()
        self.fee_spin.setRange(0, 1_000_000)
        self.fee_spin.setSuffix(" " + _("millionths"))
        self.fee_pct_label = QLabel()
        self.fee_spin.valueChanged.connect(self._update_fee_label)
        fee_row = QHBoxLayout()
        fee_row.addWidget(self.fee_spin)
        fee_row.addWidget(self.fee_pct_label)
        fee_wrap = QWidget()
        fee_wrap.setLayout(fee_row)
        form.addRow(_("Swap fee:"), fee_wrap)

        self.pow_spin = QSpinBox()
        self.pow_spin.setRange(0, 40)
        self.pow_spin.setToolTip(_("Proof-of-work target (in bits) for the nostr announcement."))
        form.addRow(_("Nostr PoW target:"), self.pow_spin)

        self.relays_edit = QPlainTextEdit()
        self.relays_edit.setPlaceholderText("wss://relay.example.com, wss://relay2.example.com")
        self.relays_edit.setToolTip(_("Comma- or newline-separated nostr relay URLs the server announces to."))
        self.relays_edit.setMaximumHeight(90)
        form.addRow(_("Nostr relays:"), self.relays_edit)

        self.save_btn = QPushButton(_("Save settings"))
        self.save_btn.clicked.connect(self.on_save)
        form.addRow(self.save_btn)

        return box

    def _build_output_group(self) -> QGroupBox:
        box = QGroupBox(_("Live output"))
        outer = QVBoxLayout(box)

        grid = QGridLayout()
        self._out_labels: Dict[str, QLabel] = {}
        rows = [
            ("http", _("HTTP endpoint:")),
            ("nostr", _("Nostr announcement:")),
            ("percentage", _("Fee percentage:")),
            ("min_amount", _("Min amount:")),
            ("max_forward", _("Max forward (normal):")),
            ("max_reverse", _("Max reverse:")),
            ("mining_fee", _("Mining fee:")),
            ("can_send", _("Lightning can send:")),
            ("can_receive", _("Lightning can receive:")),
        ]
        for i, (key, text) in enumerate(rows):
            grid.addWidget(QLabel(text), i, 0)
            val = QLabel("—")
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self._out_labels[key] = val
            grid.addWidget(val, i, 1)
        outer.addLayout(grid)

        self.summary_label = QLabel("—")
        outer.addWidget(self.summary_label)

        self.history_tree = QTreeWidget()
        self.history_tree.setHeaderLabels([_("Date"), _("Label"), _("Return (sat)")])
        self.history_tree.setRootIsDecorated(False)
        outer.addWidget(self.history_tree, 1)

        return box

    # ------------------------------------------------------------- settings
    def load_settings_into_widgets(self) -> None:
        self.port_spin.setValue(int(self.config.SWAPSERVER_PORT or 0))
        self.fee_spin.setValue(int(self.config.SWAPSERVER_FEE_MILLIONTHS))
        self.pow_spin.setValue(int(self.config.SWAPSERVER_POW_TARGET))
        self.relays_edit.setPlainText((self.config.NOSTR_RELAYS or "").replace(",", ",\n"))
        self._update_fee_label()

    def _update_fee_label(self) -> None:
        self.fee_pct_label.setText("= {:.4f} %".format(self.fee_spin.value() / 10000))

    def _relays_from_widget(self) -> str:
        raw = self.relays_edit.toPlainText().replace("\n", ",")
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        return ",".join(parts)

    def on_save(self) -> None:
        port = self.port_spin.value()
        relays = self._relays_from_widget()
        # persist
        self.config.SWAPSERVER_PORT = port if port else None
        self.config.SWAPSERVER_FEE_MILLIONTHS = self.fee_spin.value()
        self.config.SWAPSERVER_POW_TARGET = self.pow_spin.value()
        self.config.NOSTR_RELAYS = relays
        self.load_settings_into_widgets()
        # if the server is running, restart so port/relay changes take effect
        if self.plugin.is_running():
            self.plugin.stop_server()
            try:
                self.plugin.start_server()
            except SwapServerError as e:
                self.window.show_error(str(e))
        self.window.show_message(_("Swap server settings saved."))
        self.refresh()

    # ------------------------------------------------------------- controls
    def on_toggle(self) -> None:
        if self.plugin.is_running():
            self.plugin.stop_server()
            self.config.SWAPSERVER_GUI_AUTOSTART = False
        else:
            try:
                self.plugin.start_server()
            except SwapServerError as e:
                self.window.show_error(str(e))
                return
            self.config.SWAPSERVER_GUI_AUTOSTART = True
        self.refresh()

    # -------------------------------------------------------------- refresh
    def refresh(self) -> None:
        self.plugin.request_pairs_update()
        st = self.plugin.status()
        running = st["running"]

        if running:
            self.status_label.setText(
                "<b>" + _("Swap server: running") + "</b>")
            self.toggle_btn.setText(_("Disable swap server"))
        else:
            self.status_label.setText(_("Swap server: stopped"))
            self.toggle_btn.setText(_("Enable swap server"))

        if not st["http_enabled"]:
            http_txt = _("disabled")
        elif st["http_listening"]:
            http_txt = _("listening on localhost:{}").format(st["http_port"])
        elif running:
            http_txt = _("starting on port {}…").format(st["http_port"])
        else:
            http_txt = _("configured (port {})").format(st["http_port"])
        self._out_labels["http"].setText(http_txt)

        if not st["nostr_enabled"]:
            nostr_txt = _("disabled")
        elif running:
            nostr_txt = _("announcing to {} relay(s)").format(st["nostr_relay_count"])
        else:
            nostr_txt = _("{} relay(s) configured").format(st["nostr_relay_count"])
        self._out_labels["nostr"].setText(nostr_txt)

        pct = st["percentage"]
        self._out_labels["percentage"].setText("—" if pct is None else "{:.4f} %".format(pct))
        self._out_labels["min_amount"].setText(_fmt_sat(self.config, st["min_amount"]))
        self._out_labels["max_forward"].setText(_fmt_sat(self.config, st["max_forward"]))
        self._out_labels["max_reverse"].setText(_fmt_sat(self.config, st["max_reverse"]))
        self._out_labels["mining_fee"].setText(_fmt_sat(self.config, st["mining_fee"]))

        lnworker = self.wallet.lnworker
        if lnworker is not None:
            try:
                self._out_labels["can_send"].setText(_fmt_sat(self.config, int(lnworker.num_sats_can_send())))
                self._out_labels["can_receive"].setText(_fmt_sat(self.config, int(lnworker.num_sats_can_receive())))
            except Exception:
                pass

        self._refresh_history()

    def _refresh_history(self) -> None:
        try:
            history = get_swap_history(self.wallet)
        except Exception:
            self.plugin.logger.debug("failed to compute swap history", exc_info=True)
            return
        summary = get_swap_summary(history)
        self.summary_label.setText(
            _("Swaps served: {num} · net return: {ret} · {rate}/day").format(
                num=summary["num_swaps"],
                ret=_fmt_sat(self.config, summary["overall_return_sat"]),
                rate=summary["swaps_per_day"],
            ))
        self.history_tree.clear()
        for item in reversed(history):  # newest first
            self.history_tree.addTopLevelItem(QTreeWidgetItem([
                item["date"], item["label"], str(item["return_sat"]),
            ]))


class Plugin(SwapServerGuiPlugin):
    """Qt entry point. Adds the Swap Server tab and drives the server."""

    def __init__(self, *args: Any) -> None:
        SwapServerGuiPlugin.__init__(self, *args)
        self._tab: Optional[SwapServerTab] = None
        self._window: Optional['ElectrumWindow'] = None

    @hook
    def load_wallet(self, wallet: 'Abstract_Wallet', window: 'ElectrumWindow') -> None:
        if not wallet.has_lightning():
            self.logger.info("wallet has no lightning; not adding Swap Server tab")
            return
        self.bind_wallet(wallet)
        self._add_tab(window)
        if self.config.SWAPSERVER_GUI_AUTOSTART and self.can_run() is None:
            try:
                self.start_server()
            except SwapServerError as e:
                self.logger.info(f"autostart skipped: {e}")

    def _add_tab(self, window: 'ElectrumWindow') -> None:
        if self._tab is not None:
            return
        self._window = window
        self._tab = SwapServerTab(self, window)
        window.tabs.addTab(self._tab, read_QIcon("lightning.png"), _("Swap Server"))

    def _remove_tab(self) -> None:
        if self._tab is None or self._window is None:
            return
        idx = self._window.tabs.indexOf(self._tab)
        if idx != -1:
            self._window.tabs.removeTab(idx)
        self._tab = None
        self._window = None

    @hook
    def close_wallet(self, wallet: 'Abstract_Wallet') -> None:
        self.stop_server()
        self._remove_tab()

    @hook
    def on_close_window(self, window: 'ElectrumWindow') -> None:
        if window is self._window:
            self.stop_server()
            self._remove_tab()

    def close(self) -> None:
        # called when the plugin is disabled from the Plugins dialog
        self.stop_server()
        self._remove_tab()
        super().close()
