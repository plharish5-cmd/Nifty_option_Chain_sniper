#!/usr/bin/env bash
# start_desks.sh — (re)start both Paper Desks.
#   ./start_desks.sh            → restart both (Harish :8502, profile "wife" :8503)
#   ./start_desks.sh priya      → manual mode: second desk uses profile "priya"
#   ./start_desks.sh stop       → stop both
#   ./start_desks.sh status     → are they running?
# If autostart is installed (install_autostart.sh) this just restarts the system services,
# so you never end up with two copies fighting for the same port.
cd "$(dirname "$0")" || exit 1
ARG="${1:-wife}"
TS=$(tailscale ip -4 2>/dev/null | head -1)

if [ -f /etc/systemd/system/paperdesk-main.service ]; then
  case "$ARG" in
    stop)   sudo systemctl stop paperdesk-main paperdesk-second; echo "stopped (they will start again on next boot)";;
    status) systemctl --no-pager --lines=0 status paperdesk-main paperdesk-second | grep -E "●|Active";;
    *)      sudo systemctl restart paperdesk-main paperdesk-second; sleep 5
            systemctl is-active paperdesk-main paperdesk-second | paste -sd' ' | sed 's/^/services: /'
            echo "Harish → http://${TS:-<tailscale-ip>}:8502   second desk → http://${TS:-<tailscale-ip>}:8503";;
  esac
  exit 0
fi

# ── manual mode (autostart not installed) ──
if [ "$ARG" = "stop" ]; then
  pkill -f "streamlit run paper_desk_app.py" && echo "stopped" || echo "nothing running"; exit 0
fi
if [ "$ARG" = "status" ]; then pgrep -af "streamlit run paper_desk_app.py" || echo "not running"; exit 0; fi
mkdir -p data/logs
pkill -f "streamlit run paper_desk_app.py" 2>/dev/null; sleep 1
nohup .venv/bin/streamlit run paper_desk_app.py --server.port 8502 --server.headless true > data/logs/desk_main.log 2>&1 &
PAPER_PROFILE="$ARG" nohup .venv/bin/streamlit run paper_desk_app.py --server.port 8503 --server.headless true > "data/logs/desk_${ARG}.log" 2>&1 &
sleep 4
echo "Harish  → http://${TS:-<tailscale-ip>}:8502"
echo "${ARG^} → http://${TS:-<tailscale-ip>}:8503"
echo "logs in data/logs/ · stop with: ./start_desks.sh stop"
