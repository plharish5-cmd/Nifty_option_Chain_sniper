#!/usr/bin/env bash
# start_all.sh — one click: opens the briefing terminal AND the Streamlit dashboard, each in its own terminal window.
OC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
open_term() {   # title, script
  local title="$1" script="$2"
  if   command -v gnome-terminal    >/dev/null 2>&1; then gnome-terminal --title="$title" -- bash "$script" &
  elif command -v x-terminal-emulator >/dev/null 2>&1; then x-terminal-emulator -T "$title" -e bash "$script" &
  elif command -v konsole           >/dev/null 2>&1; then konsole --title "$title" -e bash "$script" &
  elif command -v xfce4-terminal    >/dev/null 2>&1; then xfce4-terminal --title="$title" -x bash "$script" &
  elif command -v xterm             >/dev/null 2>&1; then xterm -T "$title" -e bash "$script" &
  else echo "No terminal program found (gnome-terminal / konsole / xfce4-terminal / xterm)."; return 1
  fi
}
open_term "NIFTY Briefing" "$OC/start_briefing.sh"
sleep 1
open_term "NIFTY Dashboard" "$OC/start_dashboard.sh"
