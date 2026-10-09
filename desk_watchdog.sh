#!/usr/bin/env bash
# called every minute by paperdesk-watchdog.timer
cd "$(dirname "$0")" || exit 0
LOG=data/logs/watchdog.log; mkdir -p data/logs
ok() { .venv/bin/python - "$1" <<'PY' 2>/dev/null
import sys, urllib.request
port = sys.argv[1]
for path in ("/_stcore/health", "/healthz", "/"):     # newer and older Streamlit, then the page itself
    try:
        r = urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=15)
        if r.status == 200:
            sys.exit(0)
    except Exception:
        pass
sys.exit(1)
PY
}
for pair in "paperdesk-main:8502" "paperdesk-second:8503" "paperdesk-strategies:8504"; do
  svc=${pair%%:*}; port=${pair##*:}
  systemctl is-active --quiet "$svc" || continue          # stopped on purpose / still starting → leave it
  up=$(( $(date +%s) - $(date -d "$(systemctl show -p ActiveEnterTimestamp --value "$svc")" +%s 2>/dev/null || echo 0) ))
  [ "$up" -lt 90 ] && continue                             # give a fresh start 90 s to come up
  if ! ok "$port"; then
    sleep 10
    if ! ok "$port"; then
      echo "$(date '+%F %T') $svc (port $port) not answering → restarted" >> "$LOG"
      systemctl restart "$svc"
    fi
  fi
done
# keep the log small
[ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 500 ] && tail -n 200 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
exit 0
