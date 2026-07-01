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
#     swapserver_gui/qt.py
#
# The zip is a plain archive: no signing key or secret is required to build it.
# The end user authorises it locally (with their own plugin password) the first
# time they install it via Electrum's Plugins dialog.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"                 # electrum_swapgui/ (our repo)
PLUGIN_DIR="$ROOT/plugins/swapserver_gui"
OUT_DIR="${1:-$ROOT/dist}"
OUT="$OUT_DIR/swapserver_gui.zip"

if [ ! -f "$PLUGIN_DIR/manifest.json" ]; then
    echo "error: $PLUGIN_DIR/manifest.json not found" >&2
    exit 1
fi

mkdir -p "$OUT_DIR"
rm -f "$OUT"

# Zip from plugins/ so paths are prefixed with 'swapserver_gui/'. Exclude
# caches and any local pyc files.
( cd "$ROOT/plugins" && \
  zip -r -X "$OUT" swapserver_gui \
      -x '*/__pycache__/*' -x '*.pyc' >/dev/null )

echo "built $OUT"
unzip -l "$OUT"
