#!/usr/bin/env bash
# Vesktop Overlay installer: overlay app + Vencord build with the OverlayBridge plugin.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPDIR="$HOME/.local/share/vesktop-overlay"
BIN="$HOME/.local/bin/vesktop-overlay"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

if pgrep -x vesktop >/dev/null 2>&1 || pgrep -f "dev.vencord.Vesktop" >/dev/null 2>&1; then
    echo "Please close Vesktop completely first (right-click its tray icon -> Quit), then run this again."
    exit 1
fi

say "Installing what the overlay needs (asks for your password)"
sudo apt install -y python3-gi python3-gi-cairo gir1.2-gtk-3.0
# Optional: a real overlay layer on COSMIC. Missing on some systems, so never fail on it.
sudo apt install -y gir1.2-gtklayershell-0.1 >/dev/null 2>&1 || true

say "Installing the overlay app"
mkdir -p "$APPDIR" "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.config/autostart"
install -m 755 "$HERE/overlay/vesktop-overlay.py" "$APPDIR/vesktop-overlay.py"
cat > "$BIN" <<EOF
#!/bin/sh
exec /usr/bin/python3 "$APPDIR/vesktop-overlay.py" "\$@"
EOF
chmod +x "$BIN"

for target in "$HOME/.local/share/applications/vesktop-overlay.desktop" "$HOME/.config/autostart/vesktop-overlay.desktop"; do
cat > "$target" <<EOF
[Desktop Entry]
Type=Application
Name=Vesktop Overlay
Comment=Voice chat and webcam overlay for Vesktop
Exec=$BIN
Icon=audio-headset
Categories=Network;Chat;
X-GNOME-Autostart-enabled=true
EOF
done
cat > "$HOME/.local/share/applications/vesktop-overlay-demo.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Vesktop Overlay (preview)
Comment=Shows fake people and cams so you can check the look
Exec=$BIN --demo
Icon=audio-headset
Categories=Network;Chat;
EOF

say "Installing the Vencord build with the OverlayBridge plugin"
set_vencord_dir() {
    local state="$1" dir="$2"
    mkdir -p "$(dirname "$state")"
    /usr/bin/python3 - "$state" "$dir" <<'PY'
import json, os, sys
path, d = sys.argv[1], sys.argv[2]
data = {}
if os.path.exists(path):
    try:
        data = json.load(open(path))
    except ValueError:
        pass
data["vencordDir"] = d
json.dump(data, open(path, "w"), indent=4)
PY
    echo "   Vesktop will load Vencord from: $dir"
}

found=0
if command -v vesktop >/dev/null 2>&1 || [ -d "$HOME/.config/vesktop" ]; then
    rm -rf "$APPDIR/vencord" && cp -r "$HERE/vencord-dist" "$APPDIR/vencord"
    set_vencord_dir "$HOME/.config/vesktop/state.json" "$APPDIR/vencord"
    found=1
fi
if flatpak info dev.vencord.Vesktop >/dev/null 2>&1; then
    FPDIR="$HOME/.var/app/dev.vencord.Vesktop/config/vesktop"
    rm -rf "$FPDIR/vencord-overlay" && mkdir -p "$FPDIR" && cp -r "$HERE/vencord-dist" "$FPDIR/vencord-overlay"
    set_vencord_dir "$FPDIR/state.json" "$FPDIR/vencord-overlay"
    found=1
fi
[ "$found" = 1 ] || echo "   Vesktop not found. Install it, then run this script again."

say "Starting the overlay"
nohup "$BIN" >/dev/null 2>&1 &

cat <<'EOF'

Done! Last steps:
  1. Open Vesktop -> Settings -> Plugins, search "OverlayBridge" and turn it on
     (its gear icon has the position, size, colors and webcam options).
  2. Join a voice channel. The overlay appears on your screen.
  3. For webcams: keep the call view open somewhere in Vesktop (another workspace is fine).
  4. Optional show/hide shortcut: Settings -> Keyboard -> Custom shortcuts,
     command:  pkill -USR1 -f vesktop-overlay.py

Preview the look anytime: open "Vesktop Overlay (preview)" from your apps.
EOF
