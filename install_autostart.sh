#!/usr/bin/env bash
# install_autostart.sh — make both Paper Desks start automatically when the laptop boots
# (no login needed), and restart by themselves if they ever crash.
#   ./install_autostart.sh            → Harish on :8502, profile "wife" on :8503
#   ./install_autostart.sh priya      → second desk uses profile "priya"
#   ./install_autostart.sh remove     → undo (back to manual ./start_desks.sh)
set -e
OC="$(cd "$(dirname "$0")" && pwd)"
ME="$(id -un)"
P2="${1:-wife}"
U=/etc/systemd/system

if [ "$P2" = "remove" ]; then
  sudo systemctl disable --now paperdesk-main.service paperdesk-second.service 2>/dev/null || true
  sudo rm -f $U/paperdesk-main.service $U/paperdesk-second.service
  sudo systemctl daemon-reload
  echo "✔ autostart removed — use ./start_desks.sh to start manually"; exit 0
fi

[ -x "$OC/.venv/bin/streamlit" ] || { echo "✘ $OC/.venv/bin/streamlit not found — run this from the OC folder"; exit 1; }
[ -f "$OC/paper_desk_app.py" ]   || { echo "✘ paper_desk_app.py not found in $OC"; exit 1; }

# stop any desks started by hand so the ports are free
pkill -f "streamlit run paper_desk_app.py" 2>/dev/null || true
sleep 1
mkdir -p "$OC/data/logs"

unit() {   # $1 = unit name, $2 = port, $3 = profile ("" = Harish)
sudo tee "$U/$1.service" >/dev/null <<UNIT
[Unit]
Description=Paper Desk ${3:-harish} (port $2)
Wants=network-online.target
After=network-online.target tailscaled.service

[Service]
Type=simple
User=$ME
Environment="OC=$OC"
Environment="PAPER_PROFILE=$3"
WorkingDirectory=$OC
ExecStart=/bin/bash -c 'cd "\$\$OC" && exec .venv/bin/streamlit run paper_desk_app.py --server.port $2 --server.headless true --browser.gatherUsageStats false >> "data/logs/desk_${3:-main}.log" 2>&1'
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
}
unit paperdesk-main   8502 ""
unit paperdesk-second 8503 "$P2"

sudo systemctl daemon-reload
sudo systemctl enable --now paperdesk-main.service paperdesk-second.service
sleep 6
TS=$(tailscale ip -4 2>/dev/null | head -1)
for s in paperdesk-main paperdesk-second; do
  printf "%-18s %s\n" "$s" "$(systemctl is-active $s)"
done
echo "Harish → http://${TS:-<tailscale-ip>}:8502"
echo "${P2^}  → http://${TS:-<tailscale-ip>}:8503"
echo "✔ Both desks now start on every boot. Logs: data/logs/ · restart after code updates: ./start_desks.sh"
