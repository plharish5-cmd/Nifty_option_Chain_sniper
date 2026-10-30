#!/usr/bin/env bash
# run_kronos.sh — start the Kronos forecast sidecar on 127.0.0.1:8701 in its OWN venv.
#   ./run_kronos.sh            start (creates .venv and installs torch on first run)
#   ./run_kronos.sh stop       stop it
#   ./run_kronos.sh --force    start anyway, past the memory and market-hours guards below
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

# ── refuse to start when starting would hurt ──────────────────────────────────────────────────────
# On 30 Oct 2026 this sidecar was started at 07:55 on a 5.6 GiB machine already 3.3 GiB into swap.
# At 07:59 systemd-oomd reported user-slice memory pressure at 76% and killed 53 processes in the
# terminal scope, the sidecar among them. Kronos holds a 409 MB model plus torch worker threads, and
# it is a POSITIONAL read over ~4 sessions with no measured directional edge (walk-forward: direction
# 47.5% vs 49.7% for random paths). There is nothing it can tell you during a session that is worth
# competing with the desk for memory. So it declines to start in the two cases that caused harm.
MIN_FREE_MB="${KRONOS_MIN_FREE_MB:-1500}"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

if [ "$FORCE" -eq 0 ]; then
    avail_mb=$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 999999)
    if [ "$avail_mb" -lt "$MIN_FREE_MB" ]; then
        echo "REFUSING TO START: only ${avail_mb} MB available, need ${MIN_FREE_MB} MB." >&2
        echo "Loading the model here risks systemd-oomd reaping your terminal scope, as it did on" >&2
        echo "30 Oct at 07:59 (53 processes killed). Close a browser and retry, or --force." >&2
        exit 3
    fi
    dow=$(date +%u); hhmm=$(date +%H%M)
    if [ "$dow" -le 5 ] && [ "$hhmm" -ge 0900 ] && [ "$hhmm" -le 1535 ]; then
        echo "REFUSING TO START: market hours ($(date +%H:%M))." >&2
        echo "Kronos is a ~4-session positional read with no directional edge; it has nothing to add" >&2
        echo "before 15:30, and it competes with the desk on 8600 for memory. Run it after the close," >&2
        echo "or --force if you really mean it." >&2
        exit 3
    fi
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
