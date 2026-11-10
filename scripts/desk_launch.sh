#!/usr/bin/env bash
# desk_launch.sh — start a desk service, wait for bind, open browser.
#
#   ./scripts/desk_launch.sh 8600     ICT/SMC Desk (idempotent: opens if already up)
#   ./scripts/desk_launch.sh 8510     Algo Terminal — **always kills & restarts** 8510
#
# 8510 default: stop any engine_8510_live.py and free port 8510, then fresh start.
# Set DESK_LAUNCH_NO_KILL=1 to only open browser when health is OK (old behaviour).
set -uo pipefail

PORT="${1:-8600}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${REPO_ROOT}/data/logs"
LOG="${LOG_DIR}/desk_${PORT}.log"
mkdir -p "${LOG_DIR}"

say() { printf '%s\n' "$*"; }

is_up() { ss -ltn "sport = :${PORT}" 2>/dev/null | grep -q LISTEN; }

# A listening port may be the old after-hours Streamlit desk, not the algo daemon.
# Verify identity without killing anything or launching a duplicate scanner.
expected_app() {
  [[ "${PORT}" == 8510 ]] || return 0
  local py="${REPO_ROOT}/.venv/bin/python"
  [[ -x "${py}" ]] || py="$(command -v python3)"
  "${py}" - "${PORT}" <<'PYIDENTITY'
import json,sys,urllib.request,urllib.error
port=int(sys.argv[1])
try:
    raw=urllib.request.urlopen(
        'http://127.0.0.1:'+sys.argv[1]+'/api/health',timeout=8
    ).read()
except (urllib.error.URLError,TimeoutError,OSError):
    print('Port %s is bound; waiting for health.' % port,file=sys.stderr)
    sys.exit(2)
try:
    data=json.loads(raw.decode('utf-8','replace'))
except json.JSONDecodeError:
    print('Port %s returned non-JSON (wrong app).' % port,file=sys.stderr)
    sys.exit(1)
valid=(
    data.get('port')==8510
    and data.get('status')=='ONLINE'
    and 'algo_only_execution' in data
    and 'execution_mode' in data
)
if valid:
    sys.exit(0)
if data.get('port')==8510:
    print('Port 8510 warming up…',file=sys.stderr)
    sys.exit(2)
sys.exit(1)
PYIDENTITY
}
port_conflict() {
  say "Port ${PORT} belongs to another app. No process was stopped and no duplicate was started."
  say "Identify it with: sudo ss -ltnp 'sport = :${PORT}'"
}

open_browser() {
  local url="http://127.0.0.1:${PORT}/"
  say "Opening ${url}"
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "${url}" >/dev/null 2>&1 &
  elif command -v gio >/dev/null 2>&1; then
    gio open "${url}" >/dev/null 2>&1 &
  else
    say "No browser opener found. Open ${url} yourself."
  fi
}

notify_user() {
  local title="$1" body="$2"
  if command -v notify-send >/dev/null 2>&1; then
    notify-send -i utilities-terminal "${title}" "${body}" 2>/dev/null || true
  fi
  say "${title}: ${body}"
}

start_8600() {
  local backend="${REPO_ROOT}/ict_smc_app/backend"
  local py="${backend}/.venv/bin/python"
  [[ -x "${py}" ]] || py="${REPO_ROOT}/.venv/bin/python"
  [[ -x "${py}" ]] || py="$(command -v python3)"
  say "Starting ICT/SMC Desk on 127.0.0.1:${PORT} ..."
  ( cd "${backend}" && nohup "${py}" -m uvicorn app.api.main:app \
      --host 127.0.0.1 --port "${PORT}" >>"${LOG}" 2>&1 & )
}

start_8510() {
  local py="${REPO_ROOT}/.venv/bin/python"
  [[ -x "${py}" ]] || py="$(command -v python3)"
  say "Starting Algo Terminal on 127.0.0.1:${PORT} ..."
  ( cd "${REPO_ROOT}" && nohup "${py}" engine_8510_live.py >>"${LOG}" 2>&1 & )
}

stop_8510() {
  say "Stopping any existing Algo Terminal (port ${PORT}) for a clean restart..."
  pkill -TERM -f "[e]ngine_8510_live\\.py" 2>/dev/null || true
  sleep 2
  pkill -KILL -f "[e]ngine_8510_live\\.py" 2>/dev/null || true
  local waited=0
  while is_up && [[ "${waited}" -lt 25 ]]; do
    sleep 1
    waited=$((waited + 1))
  done
  if is_up; then
    if command -v fuser >/dev/null 2>&1; then
      fuser -k "${PORT}/tcp" 2>/dev/null || true
      sleep 2
    fi
    local pid
    pid="$(ss -ltnp "sport = :${PORT}" 2>/dev/null | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | head -1)"
    if [[ -n "${pid}" ]]; then
      kill -TERM "${pid}" 2>/dev/null || true
      sleep 1
      kill -KILL "${pid}" 2>/dev/null || true
      sleep 1
    fi
  fi
  if is_up; then
    say "Warning: port ${PORT} still in use after stop — start may fail."
  else
    say "Port ${PORT} is free."
  fi
}

wait_for_desk() {
  printf 'Waiting for port %s to bind' "${PORT}"
  for _ in $(seq 1 90); do
    if is_up; then
      expected_app; identity=$?
      if [[ "$identity" == 1 ]]; then port_conflict; return 1; fi
      if [[ "$identity" == 0 ]]; then
        say ""
        say "Up after ${SECONDS}s."
        open_browser
        return 0
      fi
    fi
    printf '.'
    sleep 1
  done
  say ""
  say "Did not bind within 60s. Last 20 log lines:"
  tail -20 "${LOG}" 2>/dev/null || say "(no log at ${LOG})"
  say ""
  say "Full log: ${LOG}"
  notify_user "Algos 8510" "Desk did not start in 60s. See ${LOG}"
  if [[ -t 0 ]]; then
    read -r -p "Press Enter to close..." _ || true
  fi
  return 1
}

if [[ "${PORT}" == "8510" ]] && [[ "${DESK_LAUNCH_NO_KILL:-}" != "1" ]]; then
  stop_8510
  start_8510
  wait_for_desk || exit 1
  exit 0
fi

if is_up; then
  expected_app; identity=$?
  if [[ "$identity" == 0 ]]; then
    say "Port ${PORT} is already serving the expected desk — not starting a second instance."
    open_browser
    exit 0
  elif [[ "$identity" == 1 ]]; then
    port_conflict; exit 1
  fi
  say "A process is already bound; waiting for startup without launching a duplicate."
else
  case "${PORT}" in
    8600) start_8600 ;;
    8510) start_8510 ;;
    *) say "Unknown port ${PORT}. Use 8600 or 8510."; exit 2 ;;
  esac
fi

wait_for_desk || exit 1
