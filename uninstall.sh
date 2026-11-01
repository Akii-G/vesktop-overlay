#!/usr/bin/env bash
# Removes Vesktop Overlay and gives Vesktop back its normal Vencord.
set -u
pkill -f vesktop-overlay.py 2>/dev/null
rm -rf "$HOME/.local/share/vesktop-overlay" "$HOME/.local/bin/vesktop-overlay" \
       "$HOME/.local/share/applications/vesktop-overlay.desktop" \
       "$HOME/.local/share/applications/vesktop-overlay-demo.desktop" \
       "$HOME/.config/autostart/vesktop-overlay.desktop" \
       "$HOME/.var/app/dev.vencord.Vesktop/config/vesktop/vencord-overlay"

for state in "$HOME/.config/vesktop/state.json" "$HOME/.var/app/dev.vencord.Vesktop/config/vesktop/state.json"; do
    [ -f "$state" ] || continue
    /usr/bin/python3 - "$state" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d.pop("vencordDir", None)
json.dump(d, open(p, "w"), indent=4)
PY
done
echo "Removed. Restart Vesktop to go back to the normal Vencord."
