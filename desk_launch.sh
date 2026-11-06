#!/usr/bin/env bash
# desk_launch.sh <harish|wife> — what the desktop icons run. Any time of day, no terminal needed:
#   market hours → opens the automatic desk (8502 / 8503; waits up to 60 s if it is just starting)
#   after hours  → starts a temporary desk on 8520 / 8511 (same account) and opens it; it stops itself after 4 h
#                  or at 08:50, so it never runs next to the morning desk
OC="$(cd "$(dirname "$0")" && pwd)"
cd "$OC" || exit 1
WHO="${1:-harish}"
if [ "$WHO" = "wife" ]; then MAIN=8503; ALT=8511; PROFILE=wife; NAME="Paper Desk · Wife"
else MAIN=8502; ALT=8520; PROFILE=""; NAME="Paper Desk · Harish"; fi
PY="$OC/.venv/bin/python"
note() { command -v notify-send >/dev/null && notify-send -i "$OC/data/icons/paperdesk_${WHO}.svg" "$NAME" "$1"; echo "$1"; }
up() { "$PY" - "$1" <<'PYEOF' 2>/dev/null
import sys, urllib.request
p = sys.argv[1]
for path in ("/_stcore/health", "/healthz", "/"):
    try:
        if urllib.request.urlopen(f"http://127.0.0.1:{p}{path}", timeout=3).status == 200:
            sys.exit(0)
    except Exception:
        pass
sys.exit(1)
PYEOF
}
open_url() { xdg-open "http://localhost:$1" >/dev/null 2>&1 & }

# 1) the automatic market-hours desk
if up $MAIN; then open_url $MAIN; exit 0; fi
dow=$(date +%u); hm=$((10#$(date +%H%M)))
if [ "$dow" -le 5 ] && [ "$hm" -ge 850 ] && [ "$hm" -lt 1600 ]; then
  note "Market hours — waiting for the desk to start…"
  for i in $(seq 1 30); do sleep 2; if up $MAIN; then open_url $MAIN; exit 0; fi; done
  note "The automatic desk did not answer — opening a temporary one instead."
fi
# 2) a temporary desk already running?
if up $ALT; then open_url $ALT; exit 0; fi
# 3) start a temporary desk (same account), stop it after 4 h or at 08:50, whichever comes first
mkdir -p data/logs
now=$(date +%s); morning=$(date -d "tomorrow 08:50" +%s); [ "$hm" -lt 850 ] && morning=$(date -d "today 08:50" +%s)
life=$(( morning - now )); [ "$life" -gt 14400 ] && life=14400; [ "$life" -lt 60 ] && life=60
note "Starting your desk (after hours)…"
PAPER_PROFILE="$PROFILE" nohup timeout "$life" "$OC/.venv/bin/streamlit" run paper_desk_app.py --server.port $ALT \
  --server.headless true --browser.gatherUsageStats false > "data/logs/desk_after_${WHO}.log" 2>&1 &
for i in $(seq 1 30); do sleep 1; if up $ALT; then open_url $ALT; exit 0; fi; done
note "Could not start the desk — see data/logs/desk_after_${WHO}.log"
exit 1
