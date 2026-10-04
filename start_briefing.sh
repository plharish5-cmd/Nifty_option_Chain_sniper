#!/usr/bin/env bash
# start_briefing.sh — live briefing terminal: refreshes the NIFTY briefing (snap_watch.py) every N seconds.
# Runs from the OC folder with the .venv environment. Usage: start_briefing.sh [seconds]   (default 60). Ctrl+C to stop.
OC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EVERY="${1:-60}"
cd "$OC" || { echo "Cannot open $OC"; read -r -p "Press Enter to close"; exit 1; }
if [ ! -x ".venv/bin/python" ]; then
  echo "No .venv found in $OC  (expected .venv/bin/python)."; read -r -p "Press Enter to close"; exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate
export PYTHONUNBUFFERED=1
trap 'echo; echo "Briefing stopped."; exit 0' INT TERM
while true; do
  clear
  echo "══ NIFTY BRIEFING · $(date '+%a %d %b %Y %H:%M:%S') · refresh every ${EVERY}s · Ctrl+C to stop ══"
  echo
  python snap_watch.py 2>&1
  echo
  echo "── next refresh in ${EVERY}s ──"
  sleep "$EVERY"
done
