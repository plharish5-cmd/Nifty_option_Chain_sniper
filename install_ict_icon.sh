#!/usr/bin/env bash
# install_ict_icon.sh — desktop + menu icon for the ICT / SMC desk (port 8600).
# Run once from the OC folder:   ./install_ict_icon.sh          Remove:  ./install_ict_icon.sh remove
# Needs ict-smc-desk.png and ict_open.sh in this folder.
set -eu
cd "$(dirname "$0")"
OC="$(pwd)"
ICON=ict-smc-desk
ID=ict-smc-desk
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
[ -f ict_open.sh ] || { echo "✘ ict_open.sh not found in $OC"; exit 1; }
[ -d ict_smc_app/backend ] || { echo "✘ ict_smc_app/backend not found — run this from the OC folder"; exit 1; }
chmod +x ict_open.sh

mkdir -p "$APPS" "$ICONS"
cp "$ICON.png" "$ICONS/$ICON.png"

# The OC path has spaces, so Exec= quotes it. Icon= uses the theme NAME, which has none.
cat > "$APPS/$ID.desktop" <<EOD
[Desktop Entry]
Version=1.0
Type=Application
Name=ICT SMC DESK
GenericName=ICT / SMC trading desk
Comment=ICT / SMC desk on port 8600 — starts it after hours if it is not already running
Exec="$OC/ict_open.sh"
Icon=$ICON
Terminal=false
StartupNotify=true
Categories=Office;Finance;
Keywords=ict;smc;crt;tbs;nifty;desk;
EOD
chmod +x "$APPS/$ID.desktop"
update-desktop-database "$APPS" 2>/dev/null || true
gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

if [ -d "$DESKTOP_DIR" ]; then
    cp "$APPS/$ID.desktop" "$DESKTOP_DIR/$ID.desktop"
    chmod +x "$DESKTOP_DIR/$ID.desktop"
    gio set "$DESKTOP_DIR/$ID.desktop" metadata::trusted true 2>/dev/null || true
fi

echo "✔ installed"
echo "  menu + desktop icon : ICT SMC DESK"
echo "  launcher            : $OC/ict_open.sh   (stop the after-hours copy: ./ict_open.sh stop)"
echo "  if the desktop icon still shows a ? , log out and back in once"
