#!/usr/bin/env bash
dow=$(date +%u); hm=$((10#$(date +%H%M)))
if [ "$dow" -le 5 ] && [ "$hm" -ge 855 ] && [ "$hm" -lt 1600 ]; then
  for s in paperdesk-awake paperdesk-main paperdesk-second paperdesk-strategies; do systemctl is-active --quiet $s || systemctl start $s; done
else
  for s in paperdesk-main paperdesk-second paperdesk-strategies paperdesk-awake; do systemctl is-active --quiet $s && systemctl stop $s; done
fi
exit 0
