#!/usr/bin/env bash
# install_market_hours.sh — run the Paper Desks and keep the laptop awake ONLY Mon–Fri 08:55–16:00.
#   • 08:55  desks start + laptop is held awake (lid close / idle will NOT sleep it)
#   • 16:00  desks stop + normal Ubuntu power settings come back (lid close sleeps, idle suspend works)
#   • laptop asleep overnight? it sets a wake-up alarm for 08:54 on the next weekday (if your laptop supports it)
#   • booted / opened in the middle of the day? the desks come up within a minute
#   ./install_market_hours.sh          → install
#   ./install_market_hours.sh remove   → back to "always on" (desks start on every boot)
set -e
OC="$(cd "$(dirname "$0")" && pwd)"
U=/etc/systemd/system
HOOK=/usr/lib/systemd/system-sleep/paperdesk-wake
[ -f $U/paperdesk-main.service ] || { echo "✘ run ./install_autostart.sh first"; exit 1; }

if [ "$1" = "remove" ]; then
  sudo systemctl disable --now paperdesk-hours.timer paperdesk-awake.service 2>/dev/null || true
  sudo rm -f $U/paperdesk-hours.service $U/paperdesk-hours.timer $U/paperdesk-awake.service "$HOOK" "$OC/desk_hours.sh"
  sudo systemctl daemon-reload
  sudo systemctl enable --now paperdesk-main paperdesk-second
  echo "✔ market-hours mode removed — desks run all the time again"; exit 0
fi

# 1) lid/idle: no more permanent "ignore lid" — awake is held only during market hours
sudo rm -f /etc/systemd/logind.conf.d/no-lid-suspend.conf

# 2) desks no longer start at boot by themselves; the hours switch decides
sudo systemctl disable paperdesk-main paperdesk-second >/dev/null 2>&1 || true

# 3) "keep awake" lock — while this runs, the laptop cannot sleep (lid, idle or suspend button)
sudo tee $U/paperdesk-awake.service >/dev/null <<'UNIT'
[Unit]
Description=Keep laptop awake for Paper Desk (market hours)
[Service]
ExecStart=/usr/bin/systemd-inhibit --what=sleep:idle:handle-lid-switch:handle-suspend-key --who="Paper Desk" --why="Market hours 08:55-16:00" --mode=block /bin/sleep infinity
UNIT

# 4) the switch: decides from the clock, so it is always right even after a reboot or wake-up
cat > "$OC/desk_hours.sh" <<'SW'
#!/usr/bin/env bash
dow=$(date +%u); hm=$((10#$(date +%H%M)))
if [ "$dow" -le 5 ] && [ "$hm" -ge 855 ] && [ "$hm" -lt 1600 ]; then
  for s in paperdesk-awake paperdesk-main paperdesk-second; do systemctl is-active --quiet $s || systemctl start $s; done
else
  for s in paperdesk-main paperdesk-second paperdesk-awake; do systemctl is-active --quiet $s && systemctl stop $s; done
fi
exit 0
SW
chmod +x "$OC/desk_hours.sh"
sudo tee $U/paperdesk-hours.service >/dev/null <<UNIT
[Unit]
Description=Paper Desk market-hours switch
After=network-online.target
[Service]
Type=oneshot
ExecStart=/bin/bash "$OC/desk_hours.sh"
UNIT
sudo tee $U/paperdesk-hours.timer >/dev/null <<'UNIT'
[Unit]
Description=Paper Desk market-hours switch (08:55 on, 16:00 off, re-checked every minute)
[Timer]
OnBootSec=20s
OnCalendar=*-*-* *:*:00
AccuracySec=5s
[Install]
WantedBy=timers.target
UNIT

# 5) wake-up alarm: when the laptop goes to sleep, ask it to wake at 08:54 on the next weekday
sudo tee "$HOOK" >/dev/null <<'HK'
#!/bin/sh
[ "$1" = "pre" ] || exit 0
[ -w /sys/class/rtc/rtc0/wakealarm ] || exit 0
now=$(date +%s)
for d in 0 1 2 3 4 5 6 7; do
  t=$(date -d "today +$d day 08:54" +%s)
  [ "$t" -gt "$now" ] && [ "$(date -d @"$t" +%u)" -le 5 ] && break
done
echo 0 > /sys/class/rtc/rtc0/wakealarm
echo "$t" > /sys/class/rtc/rtc0/wakealarm
exit 0
HK
sudo chmod +x "$HOOK"

sudo systemctl daemon-reload
sudo systemctl enable --now paperdesk-hours.timer
sudo systemctl start paperdesk-hours.service
sleep 3
echo "now: $(date '+%a %H:%M')"
for s in paperdesk-awake paperdesk-main paperdesk-second; do printf "  %-18s %s\n" $s "$(systemctl is-active $s)"; done
echo "✔ Market-hours mode on: Mon–Fri 08:55–16:00 desks run + laptop stays awake; rest of the time normal sleep."
