#!/usr/bin/env bash
# install_watchdog.sh — every minute, check that both Paper Desks actually answer.
# A desk that has crashed is already restarted by its service; this also catches one that is
# running but stuck (not answering), restarts it, and writes what happened to data/logs/watchdog.log.
#   ./install_watchdog.sh          → install
#   ./install_watchdog.sh remove   → undo
set -e
OC="$(cd "$(dirname "$0")" && pwd)"
U=/etc/systemd/system
if [ "$1" = "remove" ]; then
  sudo systemctl disable --now paperdesk-watchdog.timer 2>/dev/null || true
  sudo rm -f $U/paperdesk-watchdog.service $U/paperdesk-watchdog.timer "$OC/desk_watchdog.sh"
  sudo systemctl daemon-reload; echo "✔ watchdog removed"; exit 0
fi
[ -f $U/paperdesk-main.service ] || { echo "✘ run ./install_autostart.sh first"; exit 1; }

cat > "$OC/desk_watchdog.sh" <<'WD'
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
for pair in "paperdesk-main:8502" "paperdesk-second:8503"; do
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
WD
chmod +x "$OC/desk_watchdog.sh"

sudo tee $U/paperdesk-watchdog.service >/dev/null <<UNIT
[Unit]
Description=Paper Desk watchdog (restarts a desk that stops answering)
[Service]
Type=oneshot
ExecStart=/bin/bash "$OC/desk_watchdog.sh"
UNIT
sudo tee $U/paperdesk-watchdog.timer >/dev/null <<UNIT
[Unit]
Description=Run Paper Desk watchdog every minute
[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=10s
[Install]
WantedBy=timers.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now paperdesk-watchdog.timer
sudo systemctl start paperdesk-watchdog.service
echo "✔ watchdog on: checks both desks every minute · history in data/logs/watchdog.log (empty = nothing went wrong)"
