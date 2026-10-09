#!/usr/bin/env bash
# called every minute by paperdesk-hours.timer — the market-hours switch for the Paper Desks.
# At 08:55 it first stops any after-hours copy started by desk_open.sh (they hold the same ports), then starts
# the real services. Outside 08:55–16:00 Mon–Fri it stops the services; desk_open.sh may then run a copy.
cd "$(dirname "$0")" || exit 0
dow=$(date +%u); hm=$((10#$(date +%H%M)))
if [ "$dow" -le 5 ] && [ "$hm" -ge 855 ] && [ "$hm" -lt 1600 ]; then
  for f in data/.manual_*.pid; do                 # free the ports before the services claim them
    [ -f "$f" ] || continue
    pid="$(cat "$f" 2>/dev/null)"; rm -f "$f"
    [ -n "$pid" ] || continue
    kill "$pid" 2>/dev/null || continue
    for _ in 1 2 3 4 5 6 7 8; do                  # it must have released the port before systemctl start
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.25
    done
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
  done
  for s in paperdesk-awake paperdesk-main paperdesk-second paperdesk-strategies paperdesk-ict; do systemctl is-active --quiet $s || systemctl start $s; done
else
  for s in paperdesk-main paperdesk-second paperdesk-strategies paperdesk-ict paperdesk-awake; do systemctl is-active --quiet $s && systemctl stop $s; done
fi
exit 0
