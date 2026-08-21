#!/usr/bin/env python
#
# swapserver_gui - a Qt GUI plugin for Electrum's submarine swap server.
# This file is released into the public domain (The Unlicense); see LICENSE.
#
# Qt layer: injects a "Swap Server" tab into the main window and wires its
# controls to the transport lifecycle implemented in ``swapserver_gui.py``.

import importlib
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Union

import time
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QModelIndex
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout, QGroupBox,
    QLabel, QPushButton, QSpinBox, QPlainTextEdit, QTreeWidget, QTreeWidgetItem,
    QSizePolicy, QTabWidget, QAbstractItemView, QDialog,
)
from PyQt6.QtCore import QItemSelectionModel

from electrum.i18n import _
from electrum.plugin import hook
from electrum.gui.qt.util import (
    read_QIcon, pubkey_to_q_icon, WWLabel, ColorScheme, Buttons, CloseButton,
)

from .swapserver_gui import (
    SwapServerGuiPlugin, SwapServerError, AnnounceState, ComponentKind,
    ServedSwapRow, SwapComponent, SwapRole, SwapStatus, build_served_swap_rows,
    build_swap_rows, format_batched_note, format_incomplete_note,
    format_local_note, format_summary_line, format_swap_status,
    format_unattributed_note, get_swap_summary, save_settings,
)
# NB: not ``from . import pow as swap_pow`` -- that form breaks when Electrum
# loads this plugin from a zip.  See the long comment in swapserver_gui.py.
swap_pow = importlib.import_module('.pow', __package__)
nostr_check = importlib.import_module('.nostr_check', __package__)
_version = importlib.import_module('._version', __package__)

# Above this many bits a proof-of-work grind stops being a "wait a few minutes"
# affair (see the estimate shown next to the spinbox), so we ask for confirmation.
POW_TARGET_WARN_ABOVE = 30

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


# ---------------------------------------------------------------------------
# Jumping to a swap component in Electrum's History tab
#
# ``history_list.py`` has no ``run_hook`` anywhere and no "select this entry"
# entry point, so we drive its model directly.  ``HistoryModel`` is a
# ``CustomModel`` tree (electrum/gui/qt/custom_model.py): one node per history
# row, with the members of a group as child nodes.  A transaction can therefore
# be either a top-level node or a child of the group it was folded into, and
# which of the two is not knowable in advance -- ``wallet.get_full_history``
# replaces a single-member group with the member itself.  So we look in both.
# ---------------------------------------------------------------------------

def _tab_index_for_widget(tabs: QTabWidget, widget: Optional[QWidget]) -> int:
    """Index of the tab that (eventually) contains ``widget``, or -1.

    ``ElectrumWindow.create_history_tab`` wraps the list in a container and does
    not keep a reference to it, so the tab has to be found from the list up.
    """
    while widget is not None:
        idx = tabs.indexOf(widget)
        if idx != -1:
            return idx
        widget = widget.parentWidget()
    return -1


def _history_item_matches(
        item: Any, *, txid: Optional[str], payment_hash: Optional[str]) -> bool:
    if not isinstance(item, dict):
        return False
    if txid and item.get('txid') == txid:
        return True
    if payment_hash and item.get('payment_hash') == payment_hash:
        return True
    return False


def find_history_index(
        history_model: Any,
        *,
        txid: Optional[str] = None,
        payment_hash: Optional[str] = None,
) -> Optional[QModelIndex]:
    """The source-model index of a history row, searching groups too."""
    root = getattr(history_model, '_root', None)
    if root is None:
        return None
    # ``CustomModel.index`` forwards its parent straight to ``rowCount``, which
    # calls ``.isValid()`` on it -- so the root has to be an invalid
    # QModelIndex, not the None its signature defaults to.
    top = QModelIndex()
    for row, node in enumerate(root._children):
        if _history_item_matches(node.get_data(), txid=txid, payment_hash=payment_hash):
            return history_model.index(row, 0, top)
        for child_row, child in enumerate(node._children):
            if _history_item_matches(child.get_data(), txid=txid, payment_hash=payment_hash):
                return history_model.index(child_row, 0,
                                           history_model.index(row, 0, top))
    return None


def show_in_history(
        window: 'ElectrumWindow',
        *,
        txid: Optional[str] = None,
        payment_hash: Optional[str] = None,
) -> bool:
    """Switch to the History tab and select a transaction there.

    Returns False when the row could not be shown -- it is not in the history,
    or the user has a date filter on that excludes it -- so the caller can say
    so instead of silently doing nothing.
    """
    history_list = getattr(window, 'history_list', None)
    history_model = getattr(window, 'history_model', None)
    if history_list is None or history_model is None:
        return False
    idx = _tab_index_for_widget(window.tabs, history_list)
    if idx != -1:
        window.tabs.setCurrentIndex(idx)
    # The model skips refreshes while its view is hidden (MyTreeView.
    # maybe_defer_update), and the tab we just selected is not painted yet, so a
    # swap that completed since the operator last looked at the History tab
    # would not be in the model.  Force the refresh the same way upstream's
    # showEvent does.
    try:
        history_list._forced_update = True
        history_list.update()
    except Exception:
        pass
    finally:
        try:
            history_list._forced_update = False
        except Exception:
            pass
    source_index = find_history_index(history_model, txid=txid, payment_hash=payment_hash)
    if source_index is None or not source_index.isValid():
        return False
    proxy = history_list.model()
    # Only a row inside a group needs its parent expanded.  ``.parent()`` cannot
    # answer that: ``CustomModel.parent`` returns ``createIndex(...)`` for the
    # root node itself rather than an invalid index, so a *top-level* row also
    # reports a valid parent.  Ask the node instead.
    node = source_index.internalPointer()
    parent_node = node.parent() if node is not None else None
    if parent_node is not None and parent_node is not getattr(history_model, '_root', None):
        history_list.expand(proxy.mapFromSource(source_index.parent()))
    index = proxy.mapFromSource(source_index)
    if not index.isValid():
        return False
    history_list.setCurrentIndex(index)
    history_list.selectionModel().select(
        index,
        QItemSelectionModel.SelectionFlag.ClearAndSelect
        | QItemSelectionModel.SelectionFlag.Rows)
    history_list.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
    history_list.setFocus()
    return True


class SwapComponentsDialog(QDialog):
    """What one served swap is actually made of, and where to find each part.

    A swap is never a single transaction: it is a lightning payment (plus a
    mining-fee prepayment, when we are the one funding on-chain), a funding
    transaction and a claim transaction, and only some of those belong to this
    wallet.  The Swap Server tab shows the swap as one row; this is where the
    row comes apart again.

    Deliberately *not* a ``WindowModalDialog``, which is what the rest of the
    Qt GUI uses: this window's whole purpose is to send the user to the History
    tab, and a window-modal dialog blocks the very window they are being sent
    to.  Non-modal also lets them walk through the components one by one.
    """

    #: Column indexes of the component tree.
    COL_COMPONENT, COL_AMOUNT, COL_LOCATION = range(3)

    def __init__(self, tab: Union['SwapServerTab', 'SwapHistoryTab'],
                 row: 'ServedSwapRow') -> None:
        QDialog.__init__(self, tab)
        self.setWindowTitle(_("Swap details"))
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.tab = tab
        self.row = row
        self.window = tab.window
        self.config = tab.config
        self.setMinimumSize(720, 380)

        outer = QVBoxLayout(self)
        outer.addWidget(self._build_summary())

        hint = QLabel(_("Double-click a component to show it in the History tab."))
        hint.setEnabled(False)  # renders dimmed, like secondary text
        outer.addWidget(hint)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([_("Component"), _("Amount"), _("Location")])
        self.tree.setRootIsDecorated(False)
        self.tree.itemDoubleClicked.connect(self.on_component_activated)
        outer.addWidget(self.tree, 1)
        self._fill_tree()

        self.status_label = WWLabel("")
        self.status_label.setVisible(False)
        outer.addWidget(self.status_label)
        outer.addLayout(Buttons(CloseButton(self)))

    # ------------------------------------------------------------------ build
    def _build_summary(self) -> QGroupBox:
        row = self.row
        box = QGroupBox(row.label)
        form = QFormLayout(box)
        kind = _("forward swap (the taker sent on-chain, we paid lightning)") \
            if row.taker_is_forward \
            else _("reverse swap (the taker paid lightning, we sent on-chain)")
        form.addRow(_("Type:"), self._value_label(kind))
        form.addRow(_("Date:"), self._value_label(row.date))
        # An incomplete row has no return to show: see format_incomplete_note.
        return_text = _("not known — incomplete") if row.return_sat is None \
            else _fmt_sat(self.config, row.return_sat)
        form.addRow(_("Net return:"), self._value_label(return_text))
        form.addRow(_("On-chain amount:"),
                    self._value_label(_fmt_sat(self.config, row.onchain_amount_sat)))
        form.addRow(_("Lightning amount:"),
                    self._value_label(_fmt_sat(self.config, row.lightning_amount_sat)))
        form.addRow(_("Payment hash:"), self._value_label(row.payment_hash))
        form.addRow(_("Lockup address:"), self._value_label(row.lockup_address))
        form.addRow(_("Refund locktime:"), self._value_label(
            _("block {}").format(row.locktime) if row.locktime else "—"))
        if row.missing_legs:
            form.addRow(WWLabel(format_incomplete_note(missing_legs=row.missing_legs)))
        if row.role is SwapRole.UNKNOWN:
            form.addRow(WWLabel(format_unattributed_note()))
        if row.batched_with:
            note = WWLabel(format_batched_note(batched_with=row.batched_with))
            form.addRow(note)
        return box

    @staticmethod
    def _value_label(text: str) -> QLabel:
        label = QLabel(text or "—")
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    def _fill_tree(self) -> None:
        self.tree.clear()
        for component in self.row.components:
            amount = "—" if component.value_sat is None \
                else _fmt_sat(self.config, component.value_sat)
            if component.in_wallet:
                location = _("in this wallet's history")
            elif component.is_missing:
                # Ours, and absent: this is the leg that makes the row
                # incomplete, so it has to read differently from the taker's.
                location = _("missing from this wallet")
            elif component.txid:
                location = _("made by the taker")
            else:
                location = _("not recorded")
            item = QTreeWidgetItem([component.title, amount, location])
            item.setData(self.COL_COMPONENT, Qt.ItemDataRole.UserRole, component)
            identifier = component.txid or component.payment_hash or ""
            tip = component.detail
            if identifier:
                tip += "\n\n" + identifier
            for col in range(3):
                item.setToolTip(col, tip)
            if not component.in_wallet:
                # Nothing to jump to; say so by dimming the row rather than by
                # letting a double-click silently do nothing.
                item.setForeground(self.COL_LOCATION, ColorScheme.GRAY.as_color())
            self.tree.addTopLevelItem(item)
        for col in range(3):
            self.tree.resizeColumnToContents(col)

    # ----------------------------------------------------------- interaction
    def on_component_activated(self, item: QTreeWidgetItem, column: int) -> None:
        component = item.data(self.COL_COMPONENT, Qt.ItemDataRole.UserRole)
        if component is None:
            return
        if component.is_missing:
            self._say(_("{name} is this wallet's own leg, but it is not in its "
                        "history, so there is nothing to jump to. This is why "
                        "the swap shows no return.").format(name=component.title))
            return
        if not component.in_wallet:
            self._say(_("{name} was made by the taker, so it is not in this "
                        "wallet's history.").format(name=component.title))
            return
        found = show_in_history(self.window, txid=component.txid,
                                payment_hash=component.payment_hash)
        if found:
            self._say("")
        else:
            self._say(_("Could not find {name} in the History tab. A date "
                        "filter may be hiding it.").format(name=component.title))

    def _say(self, message: str) -> None:
        self.status_label.setText(message)
        self.status_label.setVisible(bool(message))


class SwapServerTab(QWidget):
    """The 'Swap Server' tab: enable/disable, settings, and live output."""

    # The discoverability check completes on the asyncio thread; this hops the
    # report (or the exception) back onto the GUI thread before touching widgets.
    checkFinished = pyqtSignal(object)

    def __init__(self, plugin: 'Plugin', window: 'ElectrumWindow') -> None:
        QWidget.__init__(self)
        self.plugin = plugin
        self.window = window
        self.config = plugin.config
        self.wallet = window.wallet
        self._npub: Optional[str] = None
        self._pubkey_hex: Optional[str] = None
        self._check_running: bool = False
        self._alive: List[bool] = [True]  # cleared by clean_up(); see on_check_discoverability
        # Open (non-modal) swap-detail windows. Nothing else refers to them, so
        # without this they would be collected as soon as they were shown.
        self._detail_dialogs: List['SwapComponentsDialog'] = []
        self.checkFinished.connect(self.on_check_finished)

        root = QVBoxLayout(self)

        # ---- header: status + enable/disable toggle -----------------------
        header = QHBoxLayout()
        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.TextFormat.RichText)
        header.addWidget(self.status_label, 1)
        # Which build of the plugin this is. Stamped into the zip at build time
        # from the git tag, so it matches the GitHub release (see _version.py).
        self.version_label = QLabel(_version.format_version(_version.__version__))
        self.version_label.setToolTip(
            _("Installed version of the Swap Server (GUI) plugin."))
        self.version_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.version_label.setEnabled(False)  # renders dimmed, like secondary text
        header.addWidget(self.version_label)
        self.toggle_btn = QPushButton()
        self.toggle_btn.clicked.connect(self.on_toggle)
        header.addWidget(self.toggle_btn)
        root.addLayout(header)

        body = QHBoxLayout()
        root.addLayout(body, 1)
        body.addWidget(self._build_settings_group())
        body.addWidget(self._build_output_group(), 1)

        # ---- periodic refresh --------------------------------------------
        self._timer: Optional[QTimer] = QTimer(self)
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
        self.pow_spin.setToolTip(_(
            "Proof-of-work target (in bits) for the nostr announcement.\n"
            "Each extra bit doubles the expected computation time."))
        self.pow_est_label = QLabel()
        self.pow_spin.valueChanged.connect(self._update_pow_estimate)
        pow_row = QHBoxLayout()
        pow_row.addWidget(self.pow_spin)
        pow_row.addWidget(self.pow_est_label)
        pow_wrap = QWidget()
        pow_wrap.setLayout(pow_row)
        form.addRow(_("Nostr PoW target:"), pow_wrap)

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
        """The right-hand pane: a Status/Diagnostics tab pair.

        The settings box deliberately stays outside these tabs so it is
        reachable from both.
        """
        box = QGroupBox(_("Live output"))
        outer = QVBoxLayout(box)
        self.output_tabs = QTabWidget()
        self.output_tabs.addTab(self._build_status_tab(), _("Status"))
        self.output_tabs.addTab(self._build_diagnostics_tab(), _("Diagnostics"))
        outer.addWidget(self.output_tabs)
        return box

    def _build_status_tab(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)

        # ---- nostr identity ------------------------------------------------
        # This is what a taker pins as SWAPSERVER_NPUB, so it belongs with the
        # operator-facing status rather than with the diagnostics.
        #
        # BOTH encodings are shown on purpose. They are the same key, but they
        # look nothing alike, and the two places an operator sees it disagree:
        # Electrum's "Choose Swap Provider" dialog renders the *hex*
        # (electrum/gui/qt/swap_dialog.py: labels[Columns.PUBKEY] =
        # x.server_pubkey) and keeps the npub only as hidden item data, while
        # SWAPSERVER_NPUB -- what a taker actually pins -- is the *npub*.
        # Showing one alone reads as "the GUI is displaying the wrong key".
        ident_box = QGroupBox(_("Nostr identity"))
        ident_grid = QGridLayout(ident_box)
        ident_grid.setColumnStretch(2, 1)

        self.npub_icon = QLabel()
        self.npub_icon.setFixedWidth(18)
        self.npub_icon.setToolTip(
            _("Identicon takers see next to this server in their provider list.\n"
              "It is derived from the key, so it is the quickest way to confirm "
              "a provider entry is this server."))
        ident_grid.addWidget(self.npub_icon, 0, 0)

        self.npub_label = QLabel("—")
        self.npub_label.setWordWrap(True)
        self.npub_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.npub_label.setToolTip(
            _("The nostr public key this swap server announces under, in bech32 "
              "(NIP-19) form.\nThis is the value a taker stores as their chosen "
              "swap provider."))
        ident_grid.addWidget(QLabel(_("npub:")), 0, 1)
        ident_grid.addWidget(self.npub_label, 0, 2)
        self.npub_copy_btn = QPushButton(_("Copy"))
        self.npub_copy_btn.clicked.connect(self.on_copy_npub)
        ident_grid.addWidget(self.npub_copy_btn, 0, 3)

        self.pubkey_hex_label = QLabel("—")
        self.pubkey_hex_label.setWordWrap(True)
        self.pubkey_hex_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.pubkey_hex_label.setToolTip(
            _("The same key in hex. This is the form Electrum's 'Choose Swap "
              "Provider' dialog lists,\nso compare against this one when "
              "checking that a taker can see this server."))
        ident_grid.addWidget(QLabel(_("hex:")), 1, 1)
        ident_grid.addWidget(self.pubkey_hex_label, 1, 2)
        self.pubkey_hex_copy_btn = QPushButton(_("Copy"))
        self.pubkey_hex_copy_btn.clicked.connect(self.on_copy_pubkey_hex)
        ident_grid.addWidget(self.pubkey_hex_copy_btn, 1, 3)

        hint = QLabel(_("Same key, two encodings. Takers list the hex form."))
        hint.setEnabled(False)  # renders dimmed, like secondary text
        ident_grid.addWidget(hint, 2, 2, 1, 2)

        outer.addWidget(ident_box)

        grid = QGridLayout()
        self._out_labels: Dict[str, QLabel] = {}
        rows = [
            ("http", _("HTTP endpoint:")),
            ("nostr", _("Nostr announcement:")),
            ("pow", _("Proof of work:")),
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
        self.summary_label.setToolTip(_(
            "Confirmed swaps this server provided to other wallets.\n"
            "Swaps this wallet initiated itself as a customer are not counted."))
        outer.addWidget(self.summary_label)

        self.history_tree = QTreeWidget()
        self.history_tree.setHeaderLabels([_("Date"), _("Label"), _("Return (sat)")])
        self.history_tree.setRootIsDecorated(False)
        self.history_tree.setToolTip(_(
            "One row per swap served, even when several swaps were settled by "
            "the same transaction.\nDouble-click a row to see the components "
            "that swap is made of."))
        self.history_tree.itemDoubleClicked.connect(self.on_history_row_activated)
        outer.addWidget(self.history_tree, 1)

        return page

    def _build_diagnostics_tab(self) -> QWidget:
        """Everything needed to answer "why can nobody see my server?".

        Each rejection on the taker side is silent (see nostr_check.py), so this
        pane spells out the values that have to match and then goes and asks the
        relays directly.
        """
        page = QWidget()
        outer = QVBoxLayout(page)

        # ---- announcement state -------------------------------------------
        ann_box = QGroupBox(_("Announcement"))
        ann_layout = QVBoxLayout(ann_box)
        self.announce_label = QLabel("—")
        self.announce_label.setTextFormat(Qt.TextFormat.RichText)
        ann_layout.addWidget(self.announce_label)
        self.announce_reason = WWLabel("")
        ann_layout.addWidget(self.announce_reason)
        self.last_publish_label = QLabel("")
        self.last_publish_label.setEnabled(False)
        ann_layout.addWidget(self.last_publish_label)
        outer.addWidget(ann_box)

        # ---- the fields a taker's filter must agree with --------------------
        match_box = QGroupBox(_("Taker must match"))
        match_box.setToolTip(_(
            "A taker only considers an offer when all of these are identical on "
            "both sides. Mismatches are discarded silently by both wallets."))
        match_form = QFormLayout(match_box)
        self._match_labels: Dict[str, QLabel] = {}
        for key, text in (("net", _("Network:")),
                          ("version", _("Event version:")),
                          ("kind", _("Event kind:")),
                          ("pow", _("Proof of work:"))):
            val = QLabel("—")
            val.setTextFormat(Qt.TextFormat.RichText)
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self._match_labels[key] = val
            match_form.addRow(text, val)
        self.pow_warning = WWLabel("")
        self.pow_warning.setVisible(False)
        match_form.addRow(self.pow_warning)
        outer.addWidget(match_box)

        # ---- the self-check ------------------------------------------------
        check_box = QGroupBox(_("Discoverability check"))
        check_layout = QVBoxLayout(check_box)
        check_row = QHBoxLayout()
        self.check_btn = QPushButton(_("Check discoverability"))
        self.check_btn.setToolTip(_(
            "Query each configured relay the way another wallet would, and "
            "report whether this server's offer comes back."))
        self.check_btn.clicked.connect(self.on_check_discoverability)
        check_row.addWidget(self.check_btn)
        self.check_headline = QLabel("")
        check_row.addWidget(self.check_headline, 1)
        check_layout.addLayout(check_row)

        self.check_tree = QTreeWidget()
        self.check_tree.setHeaderLabels([_("Relay"), _("Result"), _("Detail")])
        self.check_tree.setRootIsDecorated(False)
        check_layout.addWidget(self.check_tree, 1)

        self.check_warnings = WWLabel("")
        self.check_warnings.setVisible(False)
        check_layout.addWidget(self.check_warnings)
        outer.addWidget(check_box, 1)

        return page

    # ------------------------------------------------------------- settings
    def load_settings_into_widgets(self) -> None:
        self.port_spin.setValue(int(self.config.SWAPSERVER_PORT or 0))
        self.fee_spin.setValue(int(self.config.SWAPSERVER_FEE_MILLIONTHS))
        self.pow_spin.setValue(int(self.config.SWAPSERVER_POW_TARGET))
        self.relays_edit.setPlainText((self.config.NOSTR_RELAYS or "").replace(",", ",\n"))
        self._update_fee_label()
        self._update_pow_estimate()

    def _update_fee_label(self) -> None:
        self.fee_pct_label.setText("= {:.4f} %".format(self.fee_spin.value() / 10000))

    def _update_pow_estimate(self) -> None:
        """Show what the selected PoW target actually costs on this machine."""
        bits = self.pow_spin.value()
        if bits <= 0:
            self.pow_est_label.setText(_("disabled"))
            return
        try:
            seconds = swap_pow.estimate_seconds(bits)
        except Exception:
            self.pow_est_label.setText("")
            return
        text = _("~{} to compute").format(swap_pow.format_duration(seconds))
        if bits > POW_TARGET_WARN_ABOVE:
            text = "<span style='color:#c00;'>" + text + "</span>"
        self.pow_est_label.setText(text)

    def _relays_from_widget(self) -> str:
        raw = self.relays_edit.toPlainText().replace("\n", ",")
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        return ",".join(parts)

    def on_save(self) -> None:
        # 0 == "disabled"; never None. Assigning None to SWAPSERVER_PORT crashes
        # Electrum -- see the docstring of swapserver_gui.save_settings.
        new_port = self.port_spin.value()
        new_relays = self._relays_from_widget()
        new_pow = self.pow_spin.value()
        # Normalise the old value too: configs written by earlier versions of
        # this plugin may hold None, which must compare equal to a disabled 0.
        old_port = int(self.config.SWAPSERVER_PORT or 0)
        old_relays = self.config.NOSTR_RELAYS or ""
        old_pow = int(self.config.SWAPSERVER_POW_TARGET or 0)

        # A higher target means the stored nonce no longer qualifies and the
        # server has to grind a new one, which can take a long time. Make that
        # explicit rather than silently burning CPU for hours.
        if new_pow > old_pow and new_pow > POW_TARGET_WARN_ABOVE:
            est = swap_pow.format_duration(swap_pow.estimate_seconds(new_pow))
            if not self.window.question(
                    _("A proof-of-work target of {} bits is expected to take about {} "
                      "to compute on this machine.").format(new_pow, est) + "\n\n"
                    + _("The swap server cannot announce over nostr until it finishes. "
                        "Continue?")):
                return

        # persist. A failure here must not escape into the Qt event loop: it
        # would surface as an unhandled-exception dialog (or just a traceback on
        # the console) with the user left unsure what, if anything, was saved.
        try:
            save_settings(
                self.config,
                port=new_port,
                fee_millionths=self.fee_spin.value(),
                pow_target=new_pow,
                relays=new_relays,
            )
        except Exception as e:
            self.plugin.logger.exception("failed to save swap server settings")
            self.window.show_error(
                _("Could not save the swap server settings: {}").format(e))
            # Put the widgets back in sync with what is actually stored, so the
            # form never claims a value that was not persisted.
            self.load_settings_into_widgets()
            return
        self.load_settings_into_widgets()
        # A port/relay change needs a server restart. So does *raising* the PoW
        # target, since the running server is announcing with a nonce that no
        # longer meets it. The fee is read live on each request, and lowering the
        # target keeps the existing nonce valid, so neither needs a bounce.
        transport_changed = (new_port != old_port) or (new_relays != old_relays) \
            or (new_pow > old_pow)
        if self.plugin.is_running() and transport_changed:
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

    def on_copy_npub(self) -> None:
        if not self._npub:
            return
        self.window.do_copy(self._npub, title=_("Nostr pubkey (npub)"))

    def on_copy_pubkey_hex(self) -> None:
        if not self._pubkey_hex:
            return
        self.window.do_copy(self._pubkey_hex, title=_("Nostr pubkey (hex)"))

    # ---------------------------------------------------------- diagnostics
    def on_check_discoverability(self) -> None:
        if self._check_running:
            return
        try:
            fut = self.plugin.check_discoverability()
        except SwapServerError as e:
            self.window.show_error(str(e))
            return
        except Exception as e:  # e.g. the asyncio loop is gone
            self.plugin.logger.exception("could not start the discoverability check")
            self.window.show_error(
                _("Could not run the discoverability check: {}").format(e))
            return
        self._check_running = True
        self.check_btn.setEnabled(False)
        self.check_btn.setText(_("Checking…"))
        self.check_headline.setText(_("Querying relays…"))
        self.check_tree.clear()
        self.check_warnings.setVisible(False)

        # A plain list, not a widget attribute: clean_up() flips it so a check
        # that lands *while* the tab is being torn down never emits into a
        # deleted QObject (the timer is stopped by then, but this future is not
        # driven by it).
        alive = self._alive

        def _done(f) -> None:
            # runs on the asyncio thread: emit only, never touch widgets here
            if f.cancelled() or not alive[0]:
                return
            try:
                result = f.result()
            except Exception as e:  # noqa: BLE001 - surfaced to the user below
                result = e
            try:
                self.checkFinished.emit(result)
            except RuntimeError:
                pass  # the tab was deleted between the guard and the emit
        fut.add_done_callback(_done)

    def on_check_finished(self, report: Any) -> None:
        self._check_running = False
        self.check_btn.setEnabled(True)
        self.check_btn.setText(_("Check discoverability"))
        if isinstance(report, BaseException):
            self.check_headline.setText(_("Check failed."))
            self.window.show_error(
                _("Could not run the discoverability check: {}").format(report))
            return
        self.check_headline.setText(report.headline())
        self.check_tree.clear()
        for result in report.results:
            item = QTreeWidgetItem([
                result.relay,
                _("discoverable") if result.is_ok else result.status.value.replace("_", " "),
                result.detail,
            ])
            colour = ColorScheme.GREEN if result.is_ok else ColorScheme.RED
            item.setForeground(1, colour.as_color())
            self.check_tree.addTopLevelItem(item)
        for i in range(3):
            self.check_tree.resizeColumnToContents(i)
        warnings = report.warnings()
        if warnings:
            self.check_warnings.setText("⚠ " + "\n\n⚠ ".join(warnings))
            self.check_warnings.setVisible(True)
        else:
            self.check_warnings.setVisible(False)

    # -------------------------------------------------------------- teardown
    def clean_up(self) -> None:
        """Stop the periodic refresh. Idempotent; safe to call during shutdown."""
        self._alive[0] = False
        self.plugin.cancel_discovery_check()
        self._check_running = False
        # Detail windows outlive the tab as top-level widgets, and they hold a
        # wallet the caller is in the middle of closing. Take them with us.
        for dialog in list(self._detail_dialogs):
            dialog.close()
        self._detail_dialogs.clear()
        if self._timer is not None:
            self._timer.stop()
            try:
                self._timer.timeout.disconnect(self.refresh)
            except TypeError:
                pass  # already disconnected
            self._timer = None

    # -------------------------------------------------------------- refresh
    def refresh(self) -> None:
        if self._timer is None:
            return  # torn down; the wallet/loop may already be gone
        # Backstop for an announce task that died outright; the loop recovers
        # from everything else on its own. See SwapServerGuiPlugin.supervise.
        self.plugin.supervise()
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

        # Never claim we are announcing when the announcement path is blocked:
        # a locked wallet, an unfinished proof of work and (most commonly) too
        # little liquidity all suppress publish_offer silently. See
        # swapserver_gui.announce_state.
        state = st["announce_state"]
        nostr_txt = {
            AnnounceState.DISABLED: _("disabled"),
            AnnounceState.STOPPED: _("{} relay(s) configured").format(st["nostr_relay_count"]),
            AnnounceState.TASK_DEAD: _("not announcing — task died, restarting"),
            AnnounceState.WAITING_UNLOCK: _("waiting for wallet unlock"),
            AnnounceState.WAITING_POW: _("waiting for proof of work"),
            AnnounceState.NO_RELAY_CONNECTED: _("not announcing — no relay reachable"),
            AnnounceState.NO_LIQUIDITY: _("not announcing — no liquidity"),
            AnnounceState.PUBLISH_FAILING: _("not announcing — relays reject the offer"),
            AnnounceState.ANNOUNCING: _("announcing to {} relay(s)").format(st["nostr_relay_count"]),
            # a state added later must degrade, not crash the 4s refresh
        }.get(state, state.value.replace("_", " "))
        self._out_labels["nostr"].setText(nostr_txt)

        target = st["pow_target"]
        if not st["nostr_enabled"] or not target:
            pow_txt = _("not required")
        elif st["pow_grinding"]:
            # There is no meaningful "percent done": each nonce is an independent
            # trial, so we report scanned hashes and the best result so far.
            pow_txt = _("computing… best {best}/{target} bits, {hashes} hashes scanned").format(
                best=st["pow_best_bits"], target=target,
                hashes=f"{st['pow_hashes_done']:,}")
        elif st["pow_ready"] and running:
            pow_txt = _("ready ({} bits)").format(target)
        else:
            pow_txt = _("target {} bits").format(target)
        self._out_labels["pow"].setText(pow_txt)

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

        self._refresh_identity(st)
        self._refresh_diagnostics(st)
        self._refresh_history()

    def _refresh_identity(self, st: Dict[str, Any]) -> None:
        npub = st["nostr_npub"]
        pubkey = st["nostr_pubkey"]
        self._npub = npub
        self._pubkey_hex = pubkey
        self.npub_copy_btn.setEnabled(bool(npub))
        self.pubkey_hex_copy_btn.setEnabled(bool(pubkey))
        if not npub or not pubkey:
            self.npub_label.setText(_("no nostr key (wallet has no lightning)"))
            self.pubkey_hex_label.setText("—")
            self.npub_icon.clear()
            return
        self.npub_label.setText(npub)
        self.pubkey_hex_label.setText(pubkey)
        # Both sides feed the *hex* into pubkey_to_q_icon, so this square is
        # identical to the one beside this server in a taker's provider list.
        self.npub_icon.setPixmap(pubkey_to_q_icon(pubkey).pixmap(16, 16))

    def _refresh_diagnostics(self, st: Dict[str, Any]) -> None:
        state = st["announce_state"]
        if state is AnnounceState.ANNOUNCING:
            self.announce_label.setText("<b>" + _("Announcing") + "</b>")
        else:
            self.announce_label.setText(
                "<b>" + _("Not announcing") + "</b> — " + state.value.replace("_", " "))
        self.announce_reason.setText(self.plugin.announcement_reason(st))

        # "Attempt" alone used to be the only thing recorded here, and it was
        # only ever written once per server start -- so a healthy server that had
        # been up for days reported its last announcement as days old. The
        # announce loop now owns the publish call, so both numbers are real, and
        # the one that matters is the *acknowledged* one.
        attempt = st["last_publish_attempt_at"]
        success = st["last_publish_success_at"]
        note = st["last_publish_note"]
        interval = st["republish_interval_sec"]
        now_ts = time.time()
        if attempt is None and success is None and not note:
            self.last_publish_label.setText(
                _("This plugin has not announced yet in this session."))
        else:
            parts = []
            if success is not None:
                parts.append(_("Last announcement accepted by a relay: {} ago").format(
                    nostr_check.format_age(int(now_ts - success))))
            if attempt is not None and (success is None or attempt > success + 1):
                parts.append(_("last attempt {} ago").format(
                    nostr_check.format_age(int(now_ts - attempt))))
            if note:
                parts.append(str(note))
            self.last_publish_label.setText(" · ".join(parts))
        # The offer carries a NIP-40 expiration of ~10 min, so a gap of more than
        # two re-announce intervals means takers have almost certainly lost us
        # even if every other indicator still looks healthy.
        stale = (st["announce_state"] is AnnounceState.ANNOUNCING
                 and success is not None and now_ts - success > 2 * interval)
        if stale:
            self.announce_reason.setText(
                self.announce_reason.text() + "\n\n" + _(
                    "⚠ The last acknowledged announcement was {age} ago, which is "
                    "longer than the {mins} minute re-announce interval. Takers "
                    "may no longer see this server; check the log and run the "
                    "discoverability check below.").format(
                        age=nostr_check.format_age(int(now_ts - success)),
                        mins=interval // 60))

        self._match_labels["net"].setText(st["r_tag"])
        self._match_labels["version"].setText(st["d_tag"])
        self._match_labels["kind"].setText(str(st["kind"]))

        achieved = st["pow_bits_achieved"]
        target = st["pow_target"]
        default_target = st["pow_default_taker_target"]
        if achieved is None:
            self._match_labels["pow"].setText("—")
        else:
            txt = _("{} bits (this wallet's target: {})").format(achieved, target)
            if achieved < default_target:
                txt = "<span style='color:#c00;'>" + txt + "</span>"
            self._match_labels["pow"].setText(txt)
        # The single most common silent failure: SWAPSERVER_POW_TARGET means
        # "bits to grind" here but "bits I demand" on the taker, so announcing
        # below Electrum's default makes us invisible to stock wallets.
        if achieved is not None and achieved < default_target:
            self.pow_warning.setText(_(
                "Takers using Electrum's default proof-of-work target of {default} "
                "will silently ignore this server, because the announcement only "
                "carries {achieved} bits. Raise 'Nostr PoW target' to {default} "
                "and restart the server.").format(default=default_target, achieved=achieved))
            self.pow_warning.setVisible(True)
        else:
            self.pow_warning.setVisible(False)

    def _refresh_history(self) -> None:
        try:
            rows = build_served_swap_rows(self.wallet)
        except Exception:
            self.plugin.logger.debug("failed to compute swap history", exc_info=True)
            return
        summary = get_swap_summary(rows)
        self.summary_label.setText(format_summary_line(
            num_swaps=summary["num_swaps"],
            net_return=_fmt_sat(self.config, summary["overall_return_sat"]),
            swaps_per_day=summary["swaps_per_day"],
            num_batched=summary["num_batched"],
            num_incomplete=summary["num_incomplete"],
            num_unattributed=summary["num_unattributed"],
        ))
        self.history_tree.clear()
        for row in reversed(rows):  # newest first
            # A row with a leg missing has no return to print. Showing the
            # partial sum instead would read as a real (and often negative)
            # result, which is exactly the reading this avoids.
            return_text = _("— incomplete") if row.return_sat is None \
                else str(row.return_sat)
            tree_item = QTreeWidgetItem([row.date, row.label, return_text])
            tree_item.setData(0, Qt.ItemDataRole.UserRole, row)
            tip = _("{n} component(s); double-click for details.").format(
                n=len(row.components))
            if row.missing_legs:
                tip += "\n\n" + format_incomplete_note(missing_legs=row.missing_legs)
            if row.role is SwapRole.UNKNOWN:
                tip += "\n\n" + format_unattributed_note()
            if row.batched_with:
                # Unlike before, the value *is* this swap's own -- the shared
                # transaction's fee is split across the swaps that caused it --
                # but the operator still needs to know why one wallet
                # transaction shows up behind several rows here.
                tip += "\n\n" + format_batched_note(batched_with=row.batched_with)
            for col in range(3):
                tree_item.setToolTip(col, tip)
            if not row.counts_towards_total:
                # Dimmed for the same reason the number is withheld: this row
                # is not part of the total printed above it.
                tree_item.setForeground(2, ColorScheme.GRAY.as_color())
            self.history_tree.addTopLevelItem(tree_item)

    def on_history_row_activated(self, item: QTreeWidgetItem, column: int) -> None:
        row = item.data(0, Qt.ItemDataRole.UserRole)
        if row is None:
            return
        dialog = SwapComponentsDialog(self, row)
        # Non-modal, so nothing else holds a reference and Python would collect
        # the window the moment this returns. Drop ours when it closes.
        self._detail_dialogs.append(dialog)
        dialog.finished.connect(
            lambda _result, d=dialog: self._detail_dialogs.remove(d)
            if d in self._detail_dialogs else None)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()


class SwapHistoryTab(QWidget):
    """The "History (Swaps)" tab: the wallet's history, reorganised per swap.

    Electrum's History tab is transaction-shaped, and a swap is not a
    transaction: it is a lightning payment, sometimes a mining-fee prepayment,
    a funding transaction and a claim or refund transaction, which the history
    scatters across a group -- or collapses into a single leg, or merges with
    another swap that shared a batch transaction.  This tab is the same history
    keyed on the swap instead: one top-level row per swap, its components
    underneath it, and a value that is that swap's own.

    It exists *instead of* rewriting rows in Electrum's History tab, which this
    plugin used to do by wrapping
    ``SwapManager.get_groups_for_onchain_history``.  That tab is now left
    exactly as upstream renders it; nothing here writes to the wallet.

    Unlike the Swap Server tab's table, this one lists the operator's own swaps
    as well as the served ones, and swaps that have not settled as well as
    those that have -- because a swap the operator can see in the History tab
    should never be missing from the swap-shaped view of that same history.
    Only served, settled, complete swaps contribute to the net return; see
    :attr:`ServedSwapRow.counts_towards_total`.
    """

    #: Column indexes of the swap tree.
    COL_DATE, COL_STATUS, COL_LABEL, COL_RETURN = range(4)

    #: How often the table is rebuilt, in ms.  Matches the Swap Server tab.
    REFRESH_MS = 4000

    def __init__(self, plugin: 'Plugin', window: 'ElectrumWindow') -> None:
        QWidget.__init__(self)
        self.plugin = plugin
        self.window = window
        self.config = plugin.config
        self.wallet = window.wallet
        self._detail_dialogs: List['SwapComponentsDialog'] = []

        outer = QVBoxLayout(self)

        self.summary_label = QLabel("—")
        self.summary_label.setToolTip(_(
            "Every swap this wallet has been part of, served or its own.\n"
            "The net return covers only swaps this server served, that have "
            "settled, and whose every leg is in this wallet."))
        outer.addWidget(self.summary_label)

        hint = QLabel(_(
            "One row per swap, with the components it is made of underneath. "
            "Double-click a component to show it in the History tab."))
        hint.setEnabled(False)  # renders dimmed, like secondary text
        outer.addWidget(hint)

        self.swap_tree = QTreeWidget()
        self.swap_tree.setHeaderLabels(
            [_("Date"), _("Status"), _("Swap"), _("Return")])
        self.swap_tree.setRootIsDecorated(True)
        self.swap_tree.itemDoubleClicked.connect(self.on_item_activated)
        outer.addWidget(self.swap_tree, 1)

        self._timer: Optional[QTimer] = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(self.REFRESH_MS)
        self.refresh()

    # ------------------------------------------------------------------ build
    def refresh(self) -> None:
        try:
            rows = build_swap_rows(self.wallet, include_own=True,
                                   include_pending=True)
        except Exception:
            self.plugin.logger.debug("failed to compute swap history", exc_info=True)
            return
        summary = get_swap_summary(rows)
        self.summary_label.setText(format_summary_line(
            num_swaps=summary["num_swaps"],
            net_return=_fmt_sat(self.config, summary["overall_return_sat"]),
            swaps_per_day=summary["swaps_per_day"],
            num_batched=summary["num_batched"],
            num_incomplete=summary["num_incomplete"],
            num_unattributed=summary["num_unattributed"],
            num_pending=summary["num_pending"],
            counts_own=True,
        ))
        # Remember which swaps were expanded, so a refresh does not collapse the
        # row the operator is reading. The timer fires every few seconds.
        expanded: Set[str] = {
            self.swap_tree.topLevelItem(i).data(
                self.COL_DATE, Qt.ItemDataRole.UserRole).payment_hash
            for i in range(self.swap_tree.topLevelItemCount())
            if self.swap_tree.topLevelItem(i).isExpanded()
        }
        self.swap_tree.clear()
        for row in reversed(rows):  # newest first
            item = self._build_swap_item(row)
            self.swap_tree.addTopLevelItem(item)
            # Only after the item is in the tree: setExpanded on a detached
            # QTreeWidgetItem has no model to record it against, and is lost.
            if row.payment_hash in expanded:
                item.setExpanded(True)
        for col in range(4):
            self.swap_tree.resizeColumnToContents(col)

    def _build_swap_item(self, row: 'ServedSwapRow') -> QTreeWidgetItem:
        item = QTreeWidgetItem([
            row.date or "—",
            format_swap_status(row.status),
            row.label,
            self._return_text(row),
        ])
        item.setData(self.COL_DATE, Qt.ItemDataRole.UserRole, row)
        for col in range(4):
            item.setToolTip(col, self._swap_tooltip(row))
        if not row.counts_towards_total:
            # Dimmed for the same reason the number is withheld: this row is not
            # part of the total printed above it.
            item.setForeground(self.COL_RETURN, ColorScheme.GRAY.as_color())
        if row.status is SwapStatus.LOCAL:
            # The one status that is a problem rather than a wait: a batch that
            # was never broadcast and that upstream will not retry.
            item.setForeground(self.COL_STATUS, ColorScheme.RED.as_color())
        elif row.status is not SwapStatus.FINAL:
            item.setForeground(self.COL_STATUS, ColorScheme.GRAY.as_color())
        for component in row.components:
            item.addChild(self._build_component_item(component))
        return item

    def _build_component_item(self, component: 'SwapComponent') -> QTreeWidgetItem:
        if component.in_wallet:
            location = _("in this wallet")
        elif component.is_missing:
            # Ours, and absent: this is the leg that makes the row incomplete,
            # so it has to read differently from the taker's own leg.
            location = _("missing")
        elif component.txid:
            location = _("the taker's")
        else:
            location = _("not recorded")
        amount = "—" if component.value_sat is None \
            else _fmt_sat(self.config, component.value_sat)
        item = QTreeWidgetItem(["", location, component.title, amount])
        item.setData(self.COL_DATE, Qt.ItemDataRole.UserRole, component)
        identifier = component.txid or component.payment_hash or ""
        tip = component.detail
        if identifier:
            tip += "\n\n" + identifier
        for col in range(4):
            item.setToolTip(col, tip)
        if not component.in_wallet:
            # Nothing to jump to; say so by dimming the row rather than by
            # letting a double-click silently do nothing.
            item.setForeground(self.COL_STATUS, ColorScheme.GRAY.as_color())
        return item

    def _return_text(self, row: 'ServedSwapRow') -> str:
        """What to print in the Return column.

        Four different silences, deliberately not collapsed into one: a swap
        that has not finished has no result *yet*, one of the operator's own
        swaps has a cost rather than a return, and an incomplete swap has a
        result nobody can compute.  Printing "—" for all three would hide which
        of them the operator is looking at.
        """
        if row.status is not SwapStatus.FINAL:
            return "—"
        if row.role is SwapRole.OWN:
            return _("— own swap")
        if row.return_sat is None:
            return _("— incomplete")
        return _fmt_sat(self.config, row.return_sat)

    def _swap_tooltip(self, row: 'ServedSwapRow') -> str:
        tip = _("{n} component(s); double-click to expand, or for details.").format(
            n=len(row.components))
        if row.status is SwapStatus.LOCAL:
            tip += "\n\n" + format_local_note()
        if row.missing_legs:
            tip += "\n\n" + format_incomplete_note(missing_legs=row.missing_legs)
        if row.role is SwapRole.UNKNOWN:
            tip += "\n\n" + format_unattributed_note()
        if row.batched_with:
            tip += "\n\n" + format_batched_note(batched_with=row.batched_with)
        return tip

    # ----------------------------------------------------------- interaction
    def on_item_activated(self, item: QTreeWidgetItem, column: int) -> None:
        """A swap row opens its detail window; a component jumps to History."""
        data = item.data(self.COL_DATE, Qt.ItemDataRole.UserRole)
        if isinstance(data, SwapComponent):
            if not data.in_wallet:
                return  # dimmed already; nothing to jump to
            show_in_history(self.window, txid=data.txid,
                            payment_hash=data.payment_hash)
            return
        if data is None:
            return
        dialog = SwapComponentsDialog(self, data)
        # Non-modal, so nothing else holds a reference and Python would collect
        # the window the moment this returns. Drop ours when it closes.
        self._detail_dialogs.append(dialog)
        dialog.finished.connect(
            lambda _result, d=dialog: self._detail_dialogs.remove(d)
            if d in self._detail_dialogs else None)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def clean_up(self) -> None:
        """Stop the refresh timer before the wallet goes away underneath it."""
        if self._timer is not None:
            self._timer.stop()
            self._timer.deleteLater()
            self._timer = None


class Plugin(SwapServerGuiPlugin):
    """Qt entry point. Adds the Swap Server tab and drives the server."""

    def __init__(self, *args: Any) -> None:
        SwapServerGuiPlugin.__init__(self, *args)
        self._tab: Optional[SwapServerTab] = None
        self._history_tab: Optional[SwapHistoryTab] = None
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
        # The swap-shaped view of the wallet's history.  A tab of its own rather
        # than a rewrite of Electrum's History tab, which is left untouched.
        self._history_tab = SwapHistoryTab(self, window)
        window.tabs.addTab(self._history_tab, read_QIcon("lightning.png"),
                           _("History (Swaps)"))

    def _remove_tab(self) -> None:
        if self._window is None:
            return
        # Stop the refresh timers first: they call back into the plugin (and
        # hence into get_asyncio_loop()) every 4s, and during shutdown the
        # wallet and the asyncio loop are torn down underneath them.
        for tab in (self._tab, self._history_tab):
            if tab is None:
                continue
            tab.clean_up()
            idx = self._window.tabs.indexOf(tab)
            if idx != -1:
                self._window.tabs.removeTab(idx)
            tab.deleteLater()
        self._tab = None
        self._history_tab = None
        self._window = None

    @hook
    def close_wallet(self, wallet: 'Abstract_Wallet') -> None:
        self.stop_server()
        self._remove_tab()
        self.unbind_wallet()

    @hook
    def on_close_window(self, window: 'ElectrumWindow') -> None:
        if window is self._window:
            self.stop_server()
            self._remove_tab()
            self.unbind_wallet()

    def close(self) -> None:
        # called when the plugin is disabled from the Plugins dialog
        self.stop_server()
        self._remove_tab()
        self.unbind_wallet()
        super().close()
