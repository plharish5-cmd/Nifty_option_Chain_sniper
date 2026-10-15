#!/usr/bin/env bash
# morning.sh — runs at login (autostart). On a weekday morning boot it:
#   1. starts the 8501 dashboard if it is not already running
#   2. waits a random 0–15 min (so the post lands between ~07:20 and 07:35)
#   3. sends the View Building image (wa_send.py)
#   4. if sleep_after is on, puts the laptop back to sleep (the timer wakes it again next weekday)
# Any other time (you logged in yourself, evening, weekend) it does nothing.
set -u
OC="$HOME/Documents/Swathi K N/1 Trading/option chain/OC"
WA="$OC/wa_daily"
LOG="$WA/morning.log"
cd "$WA" || exit 1
# when started by the systemd timer (after waking from sleep) borrow the desktop session's display
if [ -z "${DISPLAY:-}" ]; then
  pid=$(pgrep -u "$(id -u)" -n gnome-shell)
  if [ -n "$pid" ]; then
    while IFS= read -r -d '' kv; do
      case "$kv" in DISPLAY=*|XAUTHORITY=*|WAYLAND_DISPLAY=*|DBUS_SESSION_BUS_ADDRESS=*|XDG_RUNTIME_DIR=*) export "$kv";; esac
    done < /proc/"$pid"/environ
  fi
  export DISPLAY="${DISPLAY:-:0}"
fi
say() { echo "$(date '+%F %T') $*" >> "$LOG"; }

dow=$(date +%u); hm=$(date +%H%M)
FORCE="${FORCE:-0}"                                   # FORCE=1 ./morning.sh  → test now, any time
if [ "$FORCE" != "1" ] && { [ "$dow" -gt 5 ] || [ "$hm" -lt 0700 ] || [ "$hm" -gt 0800 ]; }; then
  exit 0                                            # not the morning automation window
fi
if [ "$FORCE" != "1" ] && [ -f "$WA/sent_$(date +%F)" ]; then exit 0; fi   # already sent today
say "morning run started"

# 1. 8501
if ! curl -s -o /dev/null http://localhost:8501; then
  say "starting 8501"
  ( cd "$OC" && nohup .venv/bin/streamlit run dashboard.py --server.headless true >> "$WA/streamlit.log" 2>&1 & )
  for i in $(seq 1 60); do curl -s -o /dev/null http://localhost:8501 && break; sleep 5; done
fi

# 2 + 3. random wait so the send lands 07:20–07:35, then send
now=$(date +%s); start=$(date -d "07:20" +%s); end=$(date -d "07:35" +%s)
[ "$now" -lt "$start" ] && lo=$((start - now)) || lo=0
hi=$((end - now - 120)); [ "$hi" -lt "$lo" ] && hi=$lo
if [ "$FORCE" = "1" ]; then lo=5; hi=20; fi
.venv/bin/python wa_send.py --jitter "$lo-$hi"
rc=$?
if grep -q "$(date +%F).*sent " wa_send.log; then
  touch "$WA/sent_$(date +%F)"; say "SENT OK"
else
  say "SEND FAILED (rc=$rc) — laptop left ON so you can see why"
  exit 1
fi

# 4. back to sleep (the systemd timer wakes it again next weekday at 07:15)
SLEEP=$(python3 -c "import json;print(json.load(open('wa_config.json')).get('sleep_after',False))")
if [ "$SLEEP" = "True" ] && [ "$FORCE" != "1" ]; then
  say "going back to sleep in 60 s"
  sleep 60
  systemctl suspend
fi
