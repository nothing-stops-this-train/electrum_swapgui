#!/usr/bin/env python3
"""Regression tests for loading the plugin the way Electrum loads a zip plugin.

The bug these cover: installing the plugin zip crashed with

    ModuleNotFoundError: No module named 'swapserver_gui'

``electrum/plugin.py: maybe_load_plugin_init_method`` registers an external zip
plugin in ``sys.modules`` under ``electrum_external_plugins.<name>``, but builds
its spec with ``zipimport.zipimporter(zip).find_spec('<name>')`` -- so the
package object's ``__name__`` stays the *bare* ``swapserver_gui``.  CPython's
``importlib._bootstrap._handle_fromlist`` resolves a submodule fromlist against
``__name__`` rather than the ``sys.modules`` key, so ``from . import pow``
resolved to ``swapserver_gui.pow``, whose parent is not importable.

The rest of the test suite imports the plugin as a top-level ``swapserver_gui``
package (plugins/ on sys.path), where ``__name__`` *does* match the sys.modules
key -- which is precisely why it could not catch this.  So the e2e test here
runs in a subprocess with plugins/ deliberately kept OFF sys.path.

Run with:  python3 -m pytest tests/test_zip_plugin_load.py
"""
import ast
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from typing import List

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)                       # electrum_swapgui/ (our repo)
_PROJECT_ROOT = os.path.dirname(_REPO_ROOT)
_ELECTRUM_SRC = os.environ.get("ELECTRUM_SRC", os.path.join(_PROJECT_ROOT, "electrum"))
_PLUGINS_DIR = os.path.join(_REPO_ROOT, "plugins")
_PLUGIN_DIR = os.path.join(_PLUGINS_DIR, "swapserver_gui")
_MAKE_ZIP = os.path.join(_REPO_ROOT, "contrib", "make_zip.sh")

# The name Electrum registers external plugins under (electrum/plugin.py).
_BASE_NAME = "electrum_external_plugins.swapserver_gui"

_SUBPROCESS_TIMEOUT = 120


def _plugin_sources() -> List[str]:
    return sorted(
        os.path.join(_PLUGIN_DIR, fn)
        for fn in os.listdir(_PLUGIN_DIR)
        if fn.endswith(".py")
    )


class RelativeImportFormTests(unittest.TestCase):
    """Static guard: the ``from . import <submodule>`` form must not come back.

    It is the only import form affected -- ``from .pow import x`` and
    ``from .swapserver_gui import X`` resolve through ``__package__`` (taken from
    the submodule's correctly-dotted spec) and are fine.  This check needs no
    PyQt6, so it covers qt.py too.
    """

    def test_no_bare_relative_package_imports(self) -> None:
        offenders = []
        for path in _plugin_sources():
            with open(path, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=path)
            for node in ast.walk(tree):
                # `from . import x`  ->  level >= 1 and no module name
                if isinstance(node, ast.ImportFrom) and node.level >= 1 and not node.module:
                    names = ", ".join(a.name for a in node.names)
                    offenders.append(f"{os.path.basename(path)}:{node.lineno}: from . import {names}")
        self.assertEqual(
            offenders, [],
            "`from . import <submodule>` breaks when Electrum loads the plugin from a zip "
            "(see this module's docstring). Use importlib.import_module('.<sub>', __package__) "
            "or `from .<sub> import <names>` instead. Offenders:\n  " + "\n  ".join(offenders),
        )

    def test_ast_guard_would_catch_the_regression(self) -> None:
        """The guard above is only worth having if it actually fires."""
        tree = ast.parse("from . import pow as swap_pow\n")
        hits = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.level >= 1 and not n.module
        ]
        self.assertEqual(len(hits), 1)


class _ZipBuildMixin:

    @classmethod
    def build_zip(cls) -> str:
        cls._tmpdir = tempfile.TemporaryDirectory()  # noqa: SIM115  (closed in tearDownClass)
        subprocess.run(
            ["bash", _MAKE_ZIP, cls._tmpdir.name],
            check=True, capture_output=True, timeout=_SUBPROCESS_TIMEOUT,
        )
        path = os.path.join(cls._tmpdir.name, "swapserver_gui.zip")
        assert os.path.exists(path), path
        return path

    def run_child(self, code: str, *, extra_path: List[str] = ()) -> subprocess.CompletedProcess:
        """Run `code` in a fresh interpreter.

        A fresh interpreter matters twice over: the plugin's ``__init__``
        registers ConfigVars, and ``ConfigVar`` asserts each key is registered
        exactly once -- so it cannot be executed twice in one process.
        """
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([_ELECTRUM_SRC, *extra_path])
        return subprocess.run(
            [sys.executable, "-c", textwrap.dedent(code)],
            capture_output=True, text=True, env=env, timeout=_SUBPROCESS_TIMEOUT,
        )


class ExternalZipLoadTests(_ZipBuildMixin, unittest.TestCase):
    """End-to-end: replicate electrum/plugin.py's zip loader against a real zip."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.zip_path = cls.build_zip()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmpdir.cleanup()

    # The loader, transcribed from electrum/plugin.py (read_manifest +
    # maybe_load_plugin_init_method + load_plugin_by_name).
    _LOADER = """
        import importlib.util, os, sys, zipfile, zipimport
        ZIP = {zip_path!r}
        BASE = {base!r}

        with zipfile.ZipFile(ZIP) as zf:
            manifest_name = next(n for n in zf.namelist() if n.endswith('manifest.json'))
        dirname = os.path.dirname(manifest_name)

        def exec_module_from_spec(spec, path):
            module = importlib.util.module_from_spec(spec)
            sys.modules[path] = module
            spec.loader.exec_module(module)
            return module

        # This is what makes the bug reachable: the spec is named 'swapserver_gui'
        # but the module is registered as 'electrum_external_plugins.swapserver_gui'.
        init_spec = zipimport.zipimporter(ZIP).find_spec(dirname)
        pkg = exec_module_from_spec(init_spec, BASE)
    """

    def _loader(self, tail: str) -> str:
        head = textwrap.dedent(self._LOADER).format(zip_path=self.zip_path, base=_BASE_NAME)
        return head + textwrap.dedent(tail)

    def test_bare_package_name_is_not_importable(self) -> None:
        """Guards the premise: if 'swapserver_gui' were importable, this all passes vacuously."""
        r = self.run_child("""
            import importlib.util, sys
            assert 'swapserver_gui' not in sys.modules
            assert importlib.util.find_spec('swapserver_gui') is None, 'plugins/ leaked onto sys.path'
            print('OK')
        """)
        self.assertIn("OK", r.stdout, r.stderr)

    def test_package_name_differs_from_sys_modules_key(self) -> None:
        """Documents the exact asymmetry the bug rests on."""
        r = self.run_child(self._loader("""
            assert sys.modules[BASE] is pkg
            assert pkg.__name__ == 'swapserver_gui', pkg.__name__
            assert pkg.__package__ == 'swapserver_gui', pkg.__package__
            assert 'swapserver_gui' not in sys.modules
            print('OK')
        """))
        self.assertIn("OK", r.stdout, r.stderr)

    def test_non_gui_submodules_import_from_zip(self) -> None:
        """The regression test: this is the import that crashed the install."""
        r = self.run_child(self._loader("""
            for sub in ('pow', 'swapserver_gui'):
                full = BASE + '.' + sub
                spec = importlib.util.find_spec(full)
                assert spec is not None, full
                exec_module_from_spec(spec, full)
            core = sys.modules[BASE + '.swapserver_gui']
            # the module handle must be the *same* module object, not a re-import
            assert core.swap_pow is sys.modules[BASE + '.pow'], core.swap_pow
            assert core.swap_pow.pow_bits(bytes(32), 1) >= 0
            print('OK')
        """))
        self.assertIn("OK", r.stdout, r.stderr)
        self.assertNotIn("No module named 'swapserver_gui'", r.stderr)

    def test_qt_module_is_resolvable(self) -> None:
        """qt.py needs PyQt6, so exec it only if available; always check it resolves."""
        r = self.run_child(self._loader("""
            full = BASE + '.qt'
            spec = importlib.util.find_spec(full)
            assert spec is not None, 'qt implementation not found in zip'
            try:
                import PyQt6  # noqa: F401
            except ImportError:
                print('OK (spec only; PyQt6 unavailable)')
            else:
                mod = exec_module_from_spec(spec, full)
                assert mod.swap_pow is sys.modules[BASE + '.pow']
                assert issubclass(mod.Plugin, sys.modules[BASE + '.swapserver_gui'].SwapServerGuiPlugin)
                print('OK (executed)')
        """))
        self.assertIn("OK", r.stdout, r.stderr)


class DirectoryLoadTests(_ZipBuildMixin, unittest.TestCase):
    """The zip fix must not break the directory layout the rest of the suite uses."""

    def test_imports_as_top_level_package(self) -> None:
        r = self.run_child("""
            import sys
            from swapserver_gui import swapserver_gui as core, pow as swap_pow
            assert core.swap_pow is swap_pow, core.swap_pow
            print('OK')
        """, extra_path=[_PLUGINS_DIR])
        self.assertIn("OK", r.stdout, r.stderr)


if __name__ == "__main__":
    unittest.main()
