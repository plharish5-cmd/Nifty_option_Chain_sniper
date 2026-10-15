#!/usr/bin/env bash
# night_sleep.sh — run every 10 min from 23:00 to 04:50 by wa-nightsleep.timer.
# If nobody has touched the keyboard/mouse for IDLE_MIN minutes, put the laptop to sleep.
# The 07:15 wa-morning timer wakes it again. Daytime is never touched (paper desks keep running).
IDLE_MIN=30
pid=$(pgrep -u harish -n gnome-shell) || exit 0
export $(tr '\0' '\n' < /proc/"$pid"/environ | grep -E '^(DBUS_SESSION_BUS_ADDRESS|XDG_RUNTIME_DIR)=' | xargs)
ms=$(sudo -u harish -E gdbus call --session --dest org.gnome.Mutter.IdleMonitor \
      --object-path /org/gnome/Mutter/IdleMonitor/Core \
      --method org.gnome.Mutter.IdleMonitor.GetIdletime 2>/dev/null | grep -oE ' [0-9]+' | tr -d ' ' | head -1)
[ -z "$ms" ] && exit 0
if [ "$ms" -ge $((IDLE_MIN * 60 * 1000)) ]; then
  echo "$(date '+%F %T') idle $((ms/60000)) min — suspending" >> "/home/harish/Documents/Swathi K N/1 Trading/option chain/OC/wa_daily/morning.log"
  systemctl suspend
fi
