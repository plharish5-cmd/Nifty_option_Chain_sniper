#!/usr/bin/env bash
# ict_open.sh — open the ICT / SMC desk (port 8600) at ANY hour, from the desktop icon or the terminal.
#   ./ict_open.sh          open (start an after-hours copy if needed)
#   ./ict_open.sh stop     stop the after-hours copy this script started
#
# MARKET HOURS (Mon–Fri 08:55–16:00): paperdesk-ict.service runs the desk, so this just opens the browser
# (if the port is still coming up it waits for it — it never starts a second copy, desk_hours.sh would kill it).
# AFTER HOURS: starts a temporary copy of the same desk on 8600 and records its pid in data/.manual_8600.pid;
# desk_hours.sh stops it at 08:55 before starting the service, so the two never fight over the port.
# The first start of the day can take up to a minute (it downloads candles and resolves the futures contracts).
set -u
cd "$(dirname "$0")" || exit 1
OC="$(pwd)"
BACK="$OC/ict_smc_app/backend"
PORT=8600
URL="http://127.0.0.1:${PORT}"
PID_F="$OC/data/.manual_${PORT}.pid"
LOG="$BACK/ict_desk_manual.log"
mkdir -p "$OC/data"

msg() { command -v notify-send >/dev/null && notify-send "ICT / SMC desk" "$1" 2>/dev/null; echo "$1"; }
err() { command -v zenity >/dev/null && zenity --error --no-wrap --text="$1" 2>/dev/null; echo "✘ $1" >&2; }
listening() { (exec 3<>"/dev/tcp/127.0.0.1/${PORT}") 2>/dev/null && exec 3<&- && return 0 || return 1; }

if [ "${1:-}" = "stop" ]; then
    if [ -f "$PID_F" ]; then
        pid="$(cat "$PID_F")"; rm -f "$PID_F"
        if kill "$pid" 2>/dev/null; then
            for _ in $(seq 1 40); do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done   # graceful, then insist
            kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
            echo "stopped after-hours ICT desk (pid $pid)"
        else
            echo "it was not running"
        fi
    else
        echo "no after-hours ICT desk to stop (the market-hours service is managed by desk_hours.sh)"
    fi
    exit 0
fi

dow=$(date +%u); hm=$((10#$(date +%H%M)))
in_hours=0; [ "$dow" -le 5 ] && [ "$hm" -ge 855 ] && [ "$hm" -lt 1600 ] && in_hours=1

if ! listening; then
    if [ "$in_hours" = 1 ]; then
        msg "Market hours — waiting for the paperdesk-ict service on port ${PORT}…"
        for _ in $(seq 1 90); do listening && break; sleep 1; done
        if ! listening; then
            err "The ICT desk service is not answering on port ${PORT}.\nCheck:  systemctl status paperdesk-ict\nLog:  ict_smc_app/backend/ict_desk.log"
            exit 1
        fi
    else
        [ -x "$BACK/.venv/bin/python" ] || { err "ict_smc_app/backend/.venv not found"; exit 1; }
        ENVV=""
        [ ! -f "$BACK/.env" ] && [ -f "$OC/.env" ] && ENVV="$OC/.env"
        msg "Starting the ICT / SMC desk (after hours) — up to a minute on the first start…"
        cd "$BACK" || exit 1
        ENV_FILE="${ENVV}" nohup .venv/bin/python -m uvicorn app.api.main:app \
            --host 0.0.0.0 --port "$PORT" >> "$LOG" 2>&1 &
        echo $! > "$PID_F"                        # uvicorn's own pid — desk_hours.sh stops it at 08:55
        cd "$OC" || exit 1
        for _ in $(seq 1 120); do listening && break; sleep 1; done
        if ! listening; then
            err "The ICT desk did not start on port ${PORT}.\nSee ict_smc_app/backend/ict_desk_manual.log"
            exit 1
        fi
    fi
fi

if command -v xdg-open >/dev/null; then xdg-open "$URL" >/dev/null 2>&1 & else echo "$URL"; fi
exit 0
