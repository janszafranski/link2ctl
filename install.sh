#!/usr/bin/env bash
# Install link2ctl into ~/.local. No root, no system files touched.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/link2ctl"
BIN="$HOME/.local/bin"
DESKTOP="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

mkdir -p "$DATA" "$BIN" "$DESKTOP"
install -m644 "$SRC/link2ctl.py" "$SRC/link2ctl_gui.py" "$DATA/"
install -m644 "$SRC/README.md" "$SRC/PROTOCOL.md" "$DATA/"

cat > "$BIN/link2ctl" <<EOF
#!/usr/bin/env bash
# link2ctl — Insta360 Link 2 controller. Payload lives in $DATA.
set -euo pipefail

# Launched from a .desktop entry, XDG_RUNTIME_DIR is sometimes unset; GTK and
# the Wayland socket both need it.
if [ -z "\${XDG_RUNTIME_DIR:-}" ] && [ -d "/run/user/\$(id -u)" ]; then
    export XDG_RUNTIME_DIR="/run/user/\$(id -u)"
fi

exec python3 "\${LINK2CTL_APP:-$DATA/link2ctl.py}" "\$@"
EOF
chmod 755 "$BIN/link2ctl"

cat > "$DESKTOP/link2ctl.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Insta360 Link 2
GenericName=Webcam Control
Comment=Pan, tilt, zoom, image settings and presets for the Insta360 Link 2
Exec=$BIN/link2ctl
Icon=camera-web
Terminal=false
Categories=AudioVideo;Video;Settings;HardwareSettings;
Keywords=webcam;camera;insta360;link;ptz;
StartupNotify=true
EOF

update-desktop-database "$DESKTOP" 2>/dev/null || true

echo "installed:"
echo "  $BIN/link2ctl"
echo "  $DESKTOP/link2ctl.desktop"
case ":$PATH:" in
    *":$BIN:"*) ;;
    *) echo; echo "note: $BIN is not on your PATH" ;;
esac
