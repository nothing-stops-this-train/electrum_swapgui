#!/usr/bin/env python3
"""Regression tests for persisting the swap-server settings.

The bug these cover: clicking "Save settings" with the HTTP port set to 0
("disabled") crashed Electrum with

    File ".../swapserver_gui/qt.py", line 223, in on_save
      self.config.SWAPSERVER_PORT = new_port
    ...
    File ".../electrum/simple_config.py", line 316, in delete_key
      d.pop(key, None)
  AttributeError: 'NoneType' object has no attribute 'pop'

``qt.py`` turned a spinbox value of 0 into ``None``, and ``SWAPSERVER_PORT`` is
the *dotted* key ``plugins.swapserver.port``.  Assigning ``None`` routes into
``SimpleConfig``'s key-deletion branch, whose recursion dereferences the
intermediate ``plugins.swapserver`` dict without checking it exists.  This
plugin only ever writes ``plugins.swapserver_gui.*``, so that dict normally does
not exist and the delete always raises.  See ``save_settings``' docstring.

These tests drive a **real** ``SimpleConfig`` rather than the attribute-bag fake
the rest of the suite uses -- the fake is precisely why this was never caught.

Run with:  python3 -m pytest tests/test_settings_save.py
"""
import ast
import json
import os
import sys
import tempfile
import unittest
from typing import Any, Dict, List

# --- make electrum + the plugin importable ---------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)                       # electrum_swapgui/ (our repo)
_PROJECT_ROOT = os.path.dirname(_REPO_ROOT)
_ELECTRUM_SRC = os.environ.get("ELECTRUM_SRC", os.path.join(_PROJECT_ROOT, "electrum"))
_PLUGINS_DIR = os.path.join(_REPO_ROOT, "plugins")
for p in (_ELECTRUM_SRC, _PLUGINS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from electrum.simple_config import SimpleConfig  # noqa: E402
# Registers the shared ConfigVars (plugins.swapserver.port, .fee_millionths).
import electrum.plugins.swapserver  # noqa: E402,F401

from swapserver_gui.swapserver_gui import save_settings  # noqa: E402

_QT_PY = os.path.join(_PLUGINS_DIR, "swapserver_gui", "qt.py")


def _make_config(seed: Dict[str, Any]) -> SimpleConfig:
    """A real SimpleConfig on a throwaway datadir, pre-seeded with `seed`."""
    tmpdir = tempfile.mkdtemp()
    config = SimpleConfig({"electrum_path": tmpdir})
    config.user_config.update(seed)
    return config


class SaveSettingsTests(unittest.TestCase):
    """save_settings must survive every shape of user_config we can be handed."""

    # The four states, from the reproduction. 'plugins present without
    # swapserver' is the one users actually hit: enabling *this* plugin writes
    # plugins.swapserver_gui.enabled, which creates 'plugins' but not
    # 'plugins.swapserver'.
    SEEDS = {
        "fresh config": {},
        "plugins present, no swapserver": {
            "plugins": {"swapserver_gui": {"enabled": True}}},
        "full path already present": {
            "plugins": {"swapserver": {"port": 5455}}},
        "swapserver present but empty": {
            "plugins": {"swapserver": {}}},
    }

    def test_disabling_port_does_not_crash(self) -> None:
        """The regression: port 0 must save cleanly from any starting state."""
        for label, seed in self.SEEDS.items():
            with self.subTest(seed=label):
                config = _make_config(seed)
                save_settings(config, port=0, fee_millionths=5000,
                              pow_target=30, relays="wss://relay.example.com")
                self.assertFalse(config.SWAPSERVER_PORT,
                                 "a disabled port must read back as falsy")

    def test_enabling_port_does_not_crash(self) -> None:
        for label, seed in self.SEEDS.items():
            with self.subTest(seed=label):
                config = _make_config(seed)
                save_settings(config, port=5455, fee_millionths=5000,
                              pow_target=30, relays="wss://relay.example.com")
                self.assertEqual(config.SWAPSERVER_PORT, 5455)

    def test_all_values_round_trip(self) -> None:
        config = _make_config({})
        save_settings(config, port=5455, fee_millionths=1234, pow_target=22,
                      relays="wss://a.example.com,wss://b.example.com")
        self.assertEqual(config.SWAPSERVER_PORT, 5455)
        self.assertEqual(config.SWAPSERVER_FEE_MILLIONTHS, 1234)
        self.assertEqual(config.SWAPSERVER_POW_TARGET, 22)
        self.assertEqual(config.NOSTR_RELAYS,
                         "wss://a.example.com,wss://b.example.com")

    def test_later_writes_are_not_lost_when_port_is_disabled(self) -> None:
        """The port write comes first; before the fix its crash discarded the rest."""
        config = _make_config({"plugins": {"swapserver_gui": {"enabled": True}}})
        save_settings(config, port=0, fee_millionths=7777, pow_target=18,
                      relays="wss://c.example.com")
        self.assertEqual(config.SWAPSERVER_FEE_MILLIONTHS, 7777)
        self.assertEqual(config.SWAPSERVER_POW_TARGET, 18)
        self.assertEqual(config.NOSTR_RELAYS, "wss://c.example.com")

    def test_settings_persist_to_disk(self) -> None:
        """Values must survive a restart, not just live in memory."""
        tmpdir = tempfile.mkdtemp()
        config = SimpleConfig({"electrum_path": tmpdir})
        save_settings(config, port=0, fee_millionths=4321, pow_target=12,
                      relays="wss://d.example.com")
        with open(os.path.join(tmpdir, "config"), encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["plugins"]["swapserver"]["fee_millionths"], 4321)

        reloaded = SimpleConfig({"electrum_path": tmpdir})
        self.assertFalse(reloaded.SWAPSERVER_PORT)
        self.assertEqual(reloaded.SWAPSERVER_FEE_MILLIONTHS, 4321)
        self.assertEqual(reloaded.SWAPSERVER_POW_TARGET, 12)
        self.assertEqual(reloaded.NOSTR_RELAYS, "wss://d.example.com")

    def test_does_not_disable_this_plugin(self) -> None:
        """Upstream's delete path prunes empty parent dicts; ours must not run at all.

        ``plugins.swapserver_gui.enabled`` is what keeps this plugin installed --
        collateral damage there would uninstall the plugin on save.
        """
        config = _make_config({
            "plugins": {"swapserver_gui": {"enabled": True}, "swapserver": {"port": 5455}},
        })
        save_settings(config, port=0, fee_millionths=5000, pow_target=30, relays="")
        self.assertIs(config.user_config["plugins"]["swapserver_gui"]["enabled"], True)

    def test_none_port_is_rejected_loudly(self) -> None:
        """None is the trap. Fail fast in our own code rather than deep in Electrum."""
        config = _make_config({})
        with self.assertRaises(AssertionError):
            save_settings(config, port=None, fee_millionths=5000,  # type: ignore[arg-type]
                          pow_target=30, relays="")


class UpstreamBugTests(unittest.TestCase):
    """Pins down the upstream defect the workaround exists for."""

    def test_assigning_none_still_crashes_upstream(self) -> None:
        config = _make_config({"plugins": {"swapserver_gui": {"enabled": True}}})
        try:
            config.SWAPSERVER_PORT = None
        except AttributeError:
            pass  # expected: this is the crash users reported
        else:
            # Not a failure: upstream fixed delete_key. Storing 0 stays correct
            # and behaviourally identical, so the workaround can remain.
            self.skipTest("upstream Electrum no longer crashes on dotted-key deletion")


class QtWriteGuardTests(unittest.TestCase):
    """Static guard: qt.py must not write SWAPSERVER_PORT behind save_settings' back.

    qt.py cannot be imported here (PyQt6 is absent locally and in CI), so this
    inspects the source instead -- which is also what keeps the guard cheap.
    """

    @staticmethod
    def _port_assignment_lines(source: str, filename: str) -> List[int]:
        tree = ast.parse(source, filename=filename)
        hits = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "SWAPSERVER_PORT":
                    hits.append(node.lineno)
        return hits

    def test_qt_does_not_assign_swapserver_port_directly(self) -> None:
        with open(_QT_PY, encoding="utf-8") as f:
            source = f.read()
        hits = self._port_assignment_lines(source, _QT_PY)
        self.assertEqual(
            hits, [],
            "qt.py must persist the port via swapserver_gui.save_settings, which "
            "enforces the int/0 contract. Direct assignment risks reintroducing "
            "the None crash. Offending lines: " + repr(hits))

    def test_guard_would_catch_the_regression(self) -> None:
        """The guard is only worth having if it actually fires."""
        hits = self._port_assignment_lines(
            "self.config.SWAPSERVER_PORT = new_port\n", "<test>")
        self.assertEqual(len(hits), 1)


if __name__ == "__main__":
    unittest.main()
