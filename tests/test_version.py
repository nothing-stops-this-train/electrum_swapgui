#!/usr/bin/env python3
"""Unit tests for the plugin version shown in the Swap Server tab header.

The version is stamped into ``_version.py`` at build time by
``contrib/make_zip.sh`` (from the git tag in CI), so these tests cover both the
runtime formatting and the contract the build script relies on: if the committed
module drifts from what the stamping regex expects, released zips would silently
ship the ``dev`` placeholder instead of the release version.

Run with:  python3 -m pytest tests/test_version.py
"""
import os
import re
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)                       # electrum_swapgui/ (our repo)
_PLUGINS_DIR = os.path.join(_REPO_ROOT, "plugins")
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)

from swapserver_gui import _version  # noqa: E402

_VERSION_PY = os.path.join(_PLUGINS_DIR, "swapserver_gui", "_version.py")

# Must stay in step with the substitution in contrib/make_zip.sh.
_STAMP_RE = re.compile(r'^__version__ = ".*"$', re.MULTILINE)


class VersionValueTests(unittest.TestCase):

    def test_version_is_a_non_empty_string(self) -> None:
        self.assertIsInstance(_version.__version__, str)
        self.assertTrue(_version.__version__.strip())

    def test_committed_value_is_the_placeholder(self) -> None:
        """The repo must not carry a hardcoded release number.

        The tag is the source of truth; committing e.g. "0.2.1" here would go
        stale the moment the next tag is pushed and would then misreport the
        version to users.
        """
        self.assertEqual(_version.__version__, "dev")


class FormatVersionTests(unittest.TestCase):

    def test_release_versions_get_a_v_prefix(self) -> None:
        # Matches the git tag / GitHub release name users compare against.
        self.assertEqual(_version.format_version("0.2.1"), "v0.2.1")
        self.assertEqual(_version.format_version("1.0.0"), "v1.0.0")
        self.assertEqual(_version.format_version("0.2.1rc1"), "v0.2.1rc1")

    def test_dev_stamps_are_shown_verbatim(self) -> None:
        # "vdev" would read as a release; these are not versions in that sense.
        self.assertEqual(_version.format_version("dev"), "dev")
        self.assertEqual(_version.format_version("dev-g1a2b3c"), "dev-g1a2b3c")

    def test_surrounding_whitespace_is_ignored(self) -> None:
        self.assertEqual(_version.format_version("  0.2.1  "), "v0.2.1")

    def test_missing_version_degrades_gracefully(self) -> None:
        # The header label must never render as an empty gap.
        for value in ("", "   ", None):
            with self.subTest(value=value):
                self.assertEqual(_version.format_version(value), "unknown")  # type: ignore[arg-type]


class StampingContractTests(unittest.TestCase):
    """_version.py must stay rewritable by contrib/make_zip.sh."""

    def setUp(self) -> None:
        with open(_VERSION_PY, encoding="utf-8") as f:
            self.source = f.read()

    def test_exactly_one_stampable_assignment(self) -> None:
        matches = _STAMP_RE.findall(self.source)
        self.assertEqual(
            len(matches), 1,
            "contrib/make_zip.sh rewrites the first (and only) __version__ "
            "assignment; found: " + repr(matches))

    def test_stamping_produces_valid_source(self) -> None:
        stamped, count = _STAMP_RE.subn('__version__ = "0.2.1"', self.source, count=1)
        self.assertEqual(count, 1)
        namespace: dict = {}
        exec(compile(stamped, _VERSION_PY, "exec"), namespace)
        self.assertEqual(namespace["__version__"], "0.2.1")
        self.assertEqual(namespace["format_version"]("0.2.1"), "v0.2.1")

    def test_module_has_no_imports(self) -> None:
        """Keeps the module trivially safe to rewrite and to exec in isolation."""
        self.assertNotIn("\nimport ", self.source)
        self.assertNotIn("\nfrom ", self.source)


if __name__ == "__main__":
    unittest.main()
