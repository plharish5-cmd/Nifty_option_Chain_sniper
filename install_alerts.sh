#!/usr/bin/env bash
# install_alerts.sh — run alerts_bot.py in the background on every boot (it only sends on trading days:
# 08:55 brief · 09:15–15:30 setups · 20:30 tomorrow's events).   ./install_alerts.sh remove   → undo
set -e
OC="$(cd "$(dirname "$0")" && pwd)"
ME="$(id -un)"
U=/etc/systemd/system/paperdesk-alerts.service
if [ "$1" = "remove" ]; then
  sudo systemctl disable --now paperdesk-alerts.service 2>/dev/null || true
  sudo rm -f "$U"; sudo systemctl daemon-reload; echo "✔ alerts removed"; exit 0
fi
grep -q "^TELEGRAM_BOT_TOKEN=" "$OC/.env" 2>/dev/null || { echo "✘ add TELEGRAM_BOT_TOKEN=... to .env first (see alerts_bot.py)"; exit 1; }
grep -q "^TELEGRAM_CHAT_ID=" "$OC/.env" 2>/dev/null || { echo "✘ run  .venv/bin/python alerts_bot.py --chatid  and add TELEGRAM_CHAT_ID=... to .env"; exit 1; }
mkdir -p "$OC/data/logs"
sudo tee "$U" >/dev/null <<UNIT
[Unit]
Description=NIFTY Telegram alerts
Wants=network-online.target
After=network-online.target
[Service]
Type=simple
User=$ME
Environment="OC=$OC"
WorkingDirectory=$OC
ExecStart=/bin/bash -c 'cd "\$\$OC" && exec .venv/bin/python -u alerts_bot.py >> data/logs/alerts.log 2>&1'
Restart=always
RestartSec=20
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now paperdesk-alerts.service
sleep 2
echo "alerts: $(systemctl is-active paperdesk-alerts)  · log: data/logs/alerts.log"
