#!/usr/bin/env bash
# start_dashboard.sh — starts the Streamlit dashboard from the OC folder with the .venv environment and opens the browser.
OC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${1:-8501}"
cd "$OC" || { echo "Cannot open $OC"; read -r -p "Press Enter to close"; exit 1; }
if [ ! -x ".venv/bin/python" ]; then
  echo "No .venv found in $OC  (expected .venv/bin/python)."; read -r -p "Press Enter to close"; exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate
if (command -v ss >/dev/null 2>&1) && ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
  echo "Dashboard is already running on port ${PORT} — opening it."
  (xdg-open "http://localhost:${PORT}" >/dev/null 2>&1 &)
  sleep 2; exit 0
fi
echo "Starting dashboard from $OC  →  http://localhost:${PORT}   (Ctrl+C to stop)"
python -m streamlit run dashboard.py --server.port "${PORT}"
echo; echo "Dashboard stopped."; read -r -p "Press Enter to close"
