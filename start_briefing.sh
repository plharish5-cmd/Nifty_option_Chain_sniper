#!/usr/bin/env bash
# start_briefing.sh — ONE briefing terminal, drawn as separate boxed tables:
#   · a full dashboard every 5 minutes (first one immediately):
#       at-a-glance · alerts · OPTION CHAIN · levels ladder · INSTITUTIONAL · metrics · trade check ·
#       SMC · 5-RULE · CANDLES · ORDER FLOW · SCANNER · GARCH · EVIDENCE BOARD · data health
#   · between dashboards, live alert blocks from the scheduler (MENTOR / price-action / option-chain alerts) as they happen
# The long raw 5-MIN and LIVE BRIEFING text blocks are hidden here because the tables replace them.
#   SHOW_RAW=1 ./start_briefing.sh        # also show the raw scheduler text (old look)
#   BRIEF_LOG=/path/to/other.log ./start_briefing.sh
# Runs from the OC folder with the .venv environment. Ctrl+C stops it.
OC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$OC" || { echo "Cannot open $OC"; read -r -p "Press Enter to close"; exit 1; }
if [ ! -x ".venv/bin/python" ]; then
  echo "No .venv found in $OC  (expected .venv/bin/python)."; read -r -p "Press Enter to close"; exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate
export PYTHONUNBUFFERED=1
LOG="${BRIEF_LOG:-$OC/data/agt_briefing_watch.log}"
mkdir -p "$(dirname "$LOG")"; touch "$LOG"
clear
echo "══ NIFTY BRIEFING TERMINAL · $(date '+%a %d %b %Y %H:%M') · Ctrl+C to stop ══"
echo "   dashboard tables every 5 min (first one now) · live alerts in between · stream file: $LOG"
echo
trap 'trap - INT TERM; echo; echo "Briefing stopped."; kill 0' INT TERM
if [ "${SHOW_RAW:-0}" = "1" ]; then
  tail -n 40 -F "$LOG" 2>/dev/null &
else
  # new alerts as small boxed tables (the raw 5-MIN / LIVE BRIEFING text is hidden — the dashboard tables replace it)
  python terminal_extras.py --alerts &
fi
python terminal_extras.py --loop --no-write --force &
wait
