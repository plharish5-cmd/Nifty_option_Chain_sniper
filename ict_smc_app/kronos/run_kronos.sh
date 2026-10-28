#!/usr/bin/env bash
# run_kronos.sh — start the Kronos forecast sidecar on 127.0.0.1:8701 in its OWN venv.
#   ./run_kronos.sh            start (creates .venv and installs torch on first run)
#   ./run_kronos.sh stop       stop it
# The desk on 8600 never imports torch; it talks to this over localhost HTTP.
set -u
cd "$(dirname "$0")" || exit 1
HERE="$(pwd)"
PORT="${KRONOS_PORT:-8701}"
PID_F="$HERE/.kronos_${PORT}.pid"
LOG="$HERE/kronos_sidecar.log"

if [ "${1:-}" = "stop" ]; then
    if [ -f "$PID_F" ] && kill -0 "$(cat "$PID_F")" 2>/dev/null; then
        kill "$(cat "$PID_F")" && rm -f "$PID_F"
        echo "stopped"
    else
        echo "not running"
    fi
    exit 0
fi

if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    echo "already running on $PORT"
    exit 0
fi

if [ ! -x "$HERE/.venv/bin/python" ]; then
    echo "creating .venv (this installs torch — a few minutes, one time)"
    python3 -m venv "$HERE/.venv" || exit 1
    "$HERE/.venv/bin/pip" install -q --upgrade pip
    "$HERE/.venv/bin/pip" install -q -r "$HERE/requirements.txt" || exit 1
fi

KRONOS_PORT="$PORT" nohup "$HERE/.venv/bin/python" "$HERE/kronos_sidecar.py" >>"$LOG" 2>&1 &
echo $! > "$PID_F"
echo "Kronos sidecar starting on 127.0.0.1:$PORT (log: $LOG)"
