#!/usr/bin/env bash
#
# Build the external-plugin zip for swapserver_gui.
#
# Electrum's external-plugin loader (electrum/plugin.py: read_manifest /
# find_zip_plugins) expects a zip that contains the plugin *package directory*
# with its manifest.json inside, e.g.:
#
#     swapserver_gui/manifest.json
#     swapserver_gui/__init__.py
#     swapserver_gui/swapserver_gui.py
#     swapserver_gui/pow.py
#     swapserver_gui/nostr_check.py
#     swapserver_gui/qt.py
#
# The zip is a plain archive: no signing key or secret is required to build it.
# The end user authorises it locally (with their own plugin password) the first
# time they install it via Electrum's Plugins dialog.
#
# Usage:  make_zip.sh [OUT_DIR] [VERSION]
#
# VERSION (or the PLUGIN_VERSION environment variable) is stamped into
# swapserver_gui/_version.py inside the archive, and is what the plugin shows in
# its tab header.  The stamping happens in a temporary staging copy, so building
# never modifies the working tree.  With no version given the committed
# placeholder ('dev') is kept.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"                 # electrum_swapgui/ (our repo)
PLUGIN_DIR="$ROOT/plugins/swapserver_gui"
OUT_DIR="${1:-$ROOT/dist}"
VERSION="${2:-${PLUGIN_VERSION:-}}"
OUT="$OUT_DIR/swapserver_gui.zip"

if [ ! -f "$PLUGIN_DIR/manifest.json" ]; then
    echo "error: $PLUGIN_DIR/manifest.json not found" >&2
    exit 1
fi
if [ ! -f "$PLUGIN_DIR/_version.py" ]; then
    echo "error: $PLUGIN_DIR/_version.py not found" >&2
    exit 1
fi

mkdir -p "$OUT_DIR"
# Resolve to an absolute path before we cd into the staging dir below.
OUT_DIR="$(cd "$OUT_DIR" && pwd)"
OUT="$OUT_DIR/swapserver_gui.zip"
rm -f "$OUT"

# Stage the package so the version can be stamped without touching the tree.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp -r "$PLUGIN_DIR" "$STAGE/swapserver_gui"
rm -rf "$STAGE/swapserver_gui/__pycache__"

if [ -n "$VERSION" ]; then
    # Reject anything that would need quoting inside the Python string literal,
    # so a bad tag fails the build instead of emitting a broken _version.py.
    case "$VERSION" in
        *[!A-Za-z0-9.+_-]*)
            echo "error: refusing to stamp unsafe version string: $VERSION" >&2
            exit 1 ;;
    esac
    python3 - "$STAGE/swapserver_gui/_version.py" "$VERSION" <<'PY'
import re, sys
path, version = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as f:
    src = f.read()
new, n = re.subn(r'^__version__ = ".*"$', f'__version__ = "{version}"',
                 src, count=1, flags=re.MULTILINE)
if n != 1:
    raise SystemExit(f"error: could not find __version__ assignment in {path}")
with open(path, "w", encoding="utf-8") as f:
    f.write(new)
PY
    echo "stamped version: $VERSION"
else
    echo "no version given; keeping the committed placeholder"
fi

# Zip from the staging dir so paths are prefixed with 'swapserver_gui/'.
# Exclude caches and any local pyc files.
( cd "$STAGE" && \
  zip -r -X "$OUT" swapserver_gui \
      -x '*/__pycache__/*' -x '*.pyc' >/dev/null )

echo "built $OUT"
unzip -l "$OUT"
