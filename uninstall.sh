#!/usr/bin/env bash
# Remove what install.sh put in ~/.local. Presets are left alone.
set -euo pipefail

DATA="${XDG_DATA_HOME:-$HOME/.local/share}/link2ctl"
BIN="$HOME/.local/bin/link2ctl"
DESKTOP="${XDG_DATA_HOME:-$HOME/.local/share}/applications/link2ctl.desktop"

rm -rf "$DATA"
rm -f "$BIN" "$DESKTOP"

update-desktop-database "$(dirname "$DESKTOP")" 2>/dev/null || true

echo "removed:"
echo "  $BIN"
echo "  $DATA"
echo "  $DESKTOP"
echo
echo "presets kept at ${XDG_CONFIG_HOME:-$HOME/.config}/link2ctl/presets.json"
