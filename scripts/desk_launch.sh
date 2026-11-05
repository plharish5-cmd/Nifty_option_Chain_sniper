#!/usr/bin/env bash
# desk_launch.sh — start a desk service if it is not already up, wait for it to
# bind, then open it in the browser. Safe to run repeatedly: if the port is
# already listening it just opens the browser and exits.
#
#   ./scripts/desk_launch.sh 8600     ICT/SMC Desk
#   ./scripts/desk_launch.sh 8510     Algo Terminal
#
# Idempotent on purpose. Double-clicking the icon twice must not start a second
# uvicorn on the same port — that collision cost a trading morning on 2026-11-03.
set -uo pipefail

PORT="${1:-8600}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${REPO_ROOT}/data/logs"
LOG="${LOG_DIR}/desk_${PORT}.log"
mkdir -p "${LOG_DIR}"

say() { printf '%s\n' "$*"; }

is_up() { ss -ltn "sport = :${PORT}" 2>/dev/null | grep -q LISTEN; }

open_browser() {
  local url="http://127.0.0.1:${PORT}"
  say "Opening ${url}"
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "${url}" >/dev/null 2>&1 &
  elif command -v gio >/dev/null 2>&1; then
    gio open "${url}" >/dev/null 2>&1 &
  else
    say "No browser opener found. Open ${url} yourself."
  fi
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

if is_up; then
  say "Port ${PORT} is already serving — not starting a second instance."
  open_browser
  exit 0
fi

case "${PORT}" in
  8600) start_8600 ;;
  8510) start_8510 ;;
  *)    say "Unknown port ${PORT}. Use 8600 or 8510."; exit 2 ;;
esac

# The desk takes ~25s to bind (chain warm-up + Dhan backfill), so poll rather
# than guess at a sleep.
printf 'Waiting for port %s to bind' "${PORT}"
for _ in $(seq 1 60); do
  if is_up; then
    say ""
    say "Up after $(( SECONDS ))s."
    open_browser
    exit 0
  fi
  printf '.'
  sleep 1
done

say ""
say "Did not bind within 60s. Last 20 log lines:"
tail -20 "${LOG}" 2>/dev/null || say "(no log at ${LOG})"
say ""
say "Full log: ${LOG}"
read -r -p "Press Enter to close..." _ || true
exit 1
