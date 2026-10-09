#!/usr/bin/env bash
# install_desk_icon.sh — desktop + menu icon for the Strategies Paper Desk (port 8504).
# Run once:   ./install_desk_icon.sh          Remove:  ./install_desk_icon.sh remove
# Needs paperdesk-strategies.png and desk_open.sh in this folder (the OC folder).
set -eu
cd "$(dirname "$0")"
OC="$(pwd)"
ICON=paperdesk-strategies
ID=paperdesk-strategies
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/512x512/apps"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"

if [ "${1:-}" = "remove" ]; then
    rm -f "$APPS/$ID.desktop" "$DESKTOP_DIR/$ID.desktop" "$ICONS/$ICON.png"
    update-desktop-database "$APPS" 2>/dev/null || true
    echo "removed"
    exit 0
fi

[ -f "$ICON.png" ] || { echo "✘ $ICON.png not found in $OC"; exit 1; }
[ -f desk_open.sh ] || { echo "✘ desk_open.sh not found in $OC"; exit 1; }
chmod +x desk_open.sh

mkdir -p "$APPS" "$ICONS"
cp "$ICON.png" "$ICONS/$ICON.png"

# The OC path contains spaces, so Exec= quotes it. Icon= uses the theme NAME, which has none.
cat > "$APPS/$ID.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=PAPER DESK STRATEGIES
GenericName=Paper trading desk
Comment=Strategies paper trading desk on port 8504 — starts it if it is not already running
Exec="$OC/desk_open.sh" strategies 8504
Icon=$ICON
Terminal=false
StartupNotify=true
Categories=Office;Finance;
Keywords=paper;trading;desk;strategies;nifty;
EOF
chmod +x "$APPS/$ID.desktop"

update-desktop-database "$APPS" 2>/dev/null || true
gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

if [ -d "$DESKTOP_DIR" ]; then
    cp "$APPS/$ID.desktop" "$DESKTOP_DIR/$ID.desktop"
    chmod +x "$DESKTOP_DIR/$ID.desktop"
    gio set "$DESKTOP_DIR/$ID.desktop" metadata::trusted true 2>/dev/null || true
fi

echo "✔ installed"
echo "  menu + desktop icon : PAPER DESK STRATEGIES"
echo "  launcher            : $OC/desk_open.sh strategies 8504"
echo "  if the desktop icon still shows a ? , log out and back in once"
