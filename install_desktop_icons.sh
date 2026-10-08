#!/usr/bin/env bash
# install_desktop_icons.sh — puts 2 icons on your desktop (and in the app menu):
#   📝 Paper Desk · Harish     📝 Paper Desk · Wife
# Each works at ANY time: market hours → the automatic desk; after hours → starts one for you.
#   ./install_desktop_icons.sh          ./install_desktop_icons.sh remove
set -e
OC="$(cd "$(dirname "$0")" && pwd)"
APPS="$HOME/.local/share/applications"
DESK="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [ "$1" = "remove" ]; then
  rm -f "$APPS"/paperdesk-harish.desktop "$APPS"/paperdesk-wife.desktop "$DESK"/paperdesk-harish.desktop "$DESK"/paperdesk-wife.desktop
  echo "✔ icons removed"; exit 0
fi
[ -x "$OC/.venv/bin/streamlit" ] || { echo "✘ run this from the OC folder (.venv not found)"; exit 1; }
[ -f "$OC/desk_launch.sh" ] || { echo "✘ desk_launch.sh not found in $OC"; exit 1; }
chmod +x "$OC/desk_launch.sh"
mkdir -p "$OC/data/icons" "$APPS" "$DESK"
svg() {  # $1 file, $2 accent colour, $3 label
cat > "$1" <<SVG
<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">
 <rect x="8" y="8" width="240" height="240" rx="52" fill="#0b1a33" stroke="$2" stroke-width="10"/>
 <g stroke-width="5">
  <line x1="70" y1="70" x2="70" y2="150" stroke="#ef4444"/><rect x="58" y="88" width="24" height="44" fill="#ef4444"/>
  <line x1="110" y1="60" x2="110" y2="140" stroke="#22c55e"/><rect x="98" y="76" width="24" height="46" fill="#22c55e"/>
  <line x1="150" y1="44" x2="150" y2="120" stroke="#22c55e"/><rect x="138" y="58" width="24" height="44" fill="#22c55e"/>
  <line x1="190" y1="30" x2="190" y2="104" stroke="#22c55e"/><rect x="178" y="42" width="24" height="44" fill="#22c55e"/>
 </g>
 <text x="128" y="206" font-family="DejaVu Sans, sans-serif" font-size="40" font-weight="700" fill="$2" text-anchor="middle">$3</text>
</svg>
SVG
}
svg "$OC/data/icons/paperdesk_harish.svg" "#d4af37" "HARISH"
svg "$OC/data/icons/paperdesk_wife.svg" "#e879a4" "WIFE"
mk() {  # $1 id, $2 name, $3 who
f="$APPS/$1.desktop"
cat > "$f" <<DESKTOP
[Desktop Entry]
Type=Application
Version=1.0
Name=$2
Comment=Paper trading desk — market hours or after hours, opens in your browser
Exec="$OC/desk_launch.sh" $3
Path=$OC
Icon=$OC/data/icons/paperdesk_$3.svg
Terminal=false
Categories=Finance;Office;
StartupNotify=false
DESKTOP
chmod +x "$f"
cp "$f" "$DESK/$1.desktop"; chmod +x "$DESK/$1.desktop"
gio set "$DESK/$1.desktop" metadata::trusted true 2>/dev/null || true
}
mk paperdesk-harish "Paper Desk · Harish" harish
mk paperdesk-wife "Paper Desk · Wife" wife
update-desktop-database "$APPS" 2>/dev/null || true
echo "✔ 2 icons on your desktop + in the app menu (search 'Paper Desk')."
echo "  If the desktop icon shows a ✕ or asks: right-click it → 'Allow Launching' (once)."
