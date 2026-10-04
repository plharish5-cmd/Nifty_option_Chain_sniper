#!/usr/bin/env bash
# install_launchers.sh — run ONCE:  bash install_launchers.sh
# Makes the start_*.sh files executable and puts three double-click icons on your Desktop (paths are filled in automatically).
OC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
chmod +x "$OC"/start_briefing.sh "$OC"/start_dashboard.sh "$OC"/start_all.sh
DESK="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
mkdir -p "$DESK"
mk() {  # file, name, comment, exec-script, icon
  local f="$DESK/$1.desktop"
  cat > "$f" <<D
[Desktop Entry]
Type=Application
Name=$2
Comment=$3
Exec=bash "$OC/$4"
Path=$OC
Terminal=$6
Icon=$5
Categories=Office;Finance;
D
  chmod +x "$f"
  command -v gio >/dev/null 2>&1 && gio set "$f" metadata::trusted true 2>/dev/null
  echo "  created $f"
}
# briefing and dashboard open their own terminal window (Terminal=true); "both" opens two windows itself (Terminal=false)
mk "NIFTY Briefing"   "NIFTY Briefing"   "Live briefing terminal (snap_watch) in OC with .venv" start_briefing.sh  utilities-terminal true
mk "NIFTY Dashboard"  "NIFTY Dashboard"  "Streamlit dashboard in OC with .venv"                 start_dashboard.sh applications-internet true
mk "NIFTY Start Both" "NIFTY Start Both" "Briefing terminal + dashboard, one click"             start_all.sh       view-fullscreen false
echo
echo "Done. If an icon shows a lock/warning: right-click it on the Desktop → 'Allow Launching'."
echo "Or run directly:  $OC/start_all.sh"
