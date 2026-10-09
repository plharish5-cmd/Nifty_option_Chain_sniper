#!/usr/bin/env bash
# desk_open.sh — open a Paper Desk at ANY hour, from the desktop icon or the terminal.
#
#   ./desk_open.sh strategies 8504      # Strategies desk  (icon uses this)
#   ./desk_open.sh wife       8503      # wife's desk
#   ./desk_open.sh -          8502      # Harish's desk ( "-" = the default, no PAPER_PROFILE )
#   ./desk_open.sh stop                 # stop every after-hours copy this script started
#
# DURING MARKET HOURS (Mon–Fri 08:55–16:00) paperdesk-hours.service already runs the desks, so the port is
# live and this just opens the browser.
# AFTER HOURS the services are stopped on purpose, so this starts a temporary copy of the SAME app on the same
# port and records its pid in data/.manual_<port>.pid. desk_hours.sh stops those copies at 08:55 before it
# starts the real services, so the two never fight over a port.
#
# After hours there is no live Dhan feed: positions, P&L and history are all there, orders queue as AMO and
# fill at the first live price after 09:15. Nothing fills at a made-up price.
set -u
cd "$(dirname "$0")" || exit 1
mkdir -p data/logs

stop_manual() {
    local n=0 pid f i
    for f in data/.manual_*.pid; do
        [ -f "$f" ] || continue
        pid="$(cat "$f" 2>/dev/null || true)"
        rm -f "$f"
        [ -n "${pid:-}" ] || continue
        kill "$pid" 2>/dev/null || continue
        n=$((n + 1))
        for i in $(seq 1 20); do                  # wait for it to let go of the port, then insist
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.25
        done
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
    done
    echo "stopped $n after-hours desk(s)"
}

if [ "${1:-}" = "stop" ]; then
    stop_manual
    exit 0
fi

PROF="${1:-strategies}"
PORT="${2:-8504}"
URL="http://127.0.0.1:${PORT}"
PID_F="data/.manual_${PORT}.pid"

# real connect test — needs no ss/lsof, so it can never wrongly decide the port is free and start a duplicate
listening() { (exec 3<>"/dev/tcp/127.0.0.1/${PORT}") 2>/dev/null && exec 3<&- && return 0 || return 1; }

if ! listening; then
    [ "$PROF" = "-" ] && PROF=""
    PAPER_PROFILE="$PROF" nohup .venv/bin/streamlit run paper_desk_app.py \
        --server.port "$PORT" --server.headless true --browser.gatherUsageStats false \
        >> "data/logs/desk_manual_${PROF:-main}.log" 2>&1 &
    echo $! > "$PID_F"
    for _ in $(seq 1 40); do                      # Streamlit needs a couple of seconds to bind
        listening && break
        sleep 0.5
    done
    if ! listening; then
        command -v zenity >/dev/null && zenity --error --no-wrap \
            --text="Paper Desk did not start on port ${PORT}.\nSee data/logs/desk_manual_${PROF:-main}.log" 2>/dev/null
        echo "FAILED — see data/logs/desk_manual_${PROF:-main}.log" >&2
        exit 1
    fi
fi

# open in the default browser; xdg-open returns at once
if command -v xdg-open >/dev/null; then
    xdg-open "$URL" >/dev/null 2>&1 &
else
    echo "$URL"
fi
exit 0
