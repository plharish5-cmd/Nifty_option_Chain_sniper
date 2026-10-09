#!/usr/bin/env bash
# install_ict_service.sh — run the ICT / SMC desk (port 8600) as a service, like the paper desks.
#   ./install_ict_service.sh          install (or re-install) and start it if the market window is open
#   ./install_ict_service.sh remove   stop and remove the service, take it out of the hours switch + watchdog
# What it does:
#   * /etc/systemd/system/paperdesk-ict.service  — uvicorn on 0.0.0.0:8600, Restart=always
#   * desk_hours.sh    starts it 08:55 and stops it 16:00 (Mon–Fri) together with the paper desks
#   * desk_watchdog.sh restarts it if http://127.0.0.1:8600/ stops answering
# Run it from the OC folder. Uses sudo for the systemd part only.
set -eu
cd "$(dirname "$0")"
OC="$(pwd)"
BACK="$OC/ict_smc_app/backend"
UNIT=/etc/systemd/system/paperdesk-ict.service
SVC=paperdesk-ict

if [ "${1:-}" = "remove" ]; then
    sudo systemctl stop "$SVC" 2>/dev/null || true
    sudo rm -f "$UNIT"; sudo systemctl daemon-reload
    sed -i "s/ $SVC//g" desk_hours.sh
    sed -i "s/ \"$SVC:8600\"//" desk_watchdog.sh
    echo "removed — start the desk by hand again: cd ict_smc_app/backend && .venv/bin/python -m uvicorn app.api.main:app --host 0.0.0.0 --port 8600"
    exit 0
fi

[ -x "$BACK/.venv/bin/python" ] || { echo "✘ $BACK/.venv not found"; exit 1; }
[ -f desk_hours.sh ] && [ -f desk_watchdog.sh ] || { echo "✘ run this from the OC folder (desk_hours.sh / desk_watchdog.sh)"; exit 1; }

# same Dhan token as when you start it by hand: backend/.env wins, else the OC folder's .env
ENVLINE=""
if [ ! -f "$BACK/.env" ] && [ -f "$OC/.env" ]; then
    ENVLINE="Environment=\"ENV_FILE=$OC/.env\""
fi

sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=ICT / SMC desk (port 8600)
Wants=network-online.target
After=network-online.target tailscaled.service

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=$BACK
$ENVLINE
ExecStart=/bin/bash -c 'cd "$BACK" && exec .venv/bin/python -m uvicorn app.api.main:app --host 0.0.0.0 --port 8600 >> "$BACK/ict_desk.log" 2>&1'
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload

cp desk_hours.sh "desk_hours.sh.bak_ict_$(date +%H%M)"
cp desk_watchdog.sh "desk_watchdog.sh.bak_ict_$(date +%H%M)"
grep -q "$SVC" desk_hours.sh || sed -i \
    -e "s/paperdesk-strategies; do systemctl is-active --quiet \$s || systemctl start/paperdesk-strategies $SVC; do systemctl is-active --quiet \$s || systemctl start/" \
    -e "s/paperdesk-strategies paperdesk-awake; do/paperdesk-strategies $SVC paperdesk-awake; do/" desk_hours.sh
grep -q "$SVC:8600" desk_watchdog.sh || sed -i "s|\"paperdesk-strategies:8504\"|\"paperdesk-strategies:8504\" \"$SVC:8600\"|" desk_watchdog.sh
chmod +x desk_hours.sh desk_watchdog.sh
bash -n desk_hours.sh && bash -n desk_watchdog.sh
grep -c "$SVC" desk_hours.sh | grep -q 2 || { echo "✘ desk_hours.sh was not patched (unexpected layout) — send it to me"; exit 1; }
grep -q "$SVC:8600" desk_watchdog.sh || { echo "✘ desk_watchdog.sh was not patched — send it to me"; exit 1; }

# hand over from a desk you started by hand (it holds port 8600)
pkill -f "uvicorn app.api.main:app" 2>/dev/null && sleep 2 || true
dow=$(date +%u); hm=$((10#$(date +%H%M)))
if [ "$dow" -le 5 ] && [ "$hm" -ge 855 ] && [ "$hm" -lt 1600 ]; then
    sudo systemctl start "$SVC"; sleep 6
    echo "✔ installed and started: $(systemctl is-active $SVC)"
else
    echo "✔ installed. Outside 08:55–16:00 Mon–Fri, so it is not running now — the hours switch starts it next session."
    echo "  To use it tonight anyway:  sudo systemctl start $SVC   (it will be stopped again on the next minute tick"
    echo "  outside the window — for a long after-hours session run uvicorn by hand instead)"
fi
echo "  log: $BACK/ict_desk.log     status: systemctl status $SVC"
