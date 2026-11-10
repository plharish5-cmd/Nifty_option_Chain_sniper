#!/usr/bin/env bash
# install_desktop_icons.sh — puts the 8510 Algo desk launcher on the desktop.
#
#   bash scripts/install_desktop_icons.sh           # 8510 only on desktop (default)
#   bash scripts/install_desktop_icons.sh --both    # also put 8600 ICT desk on desktop
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCH="${REPO_ROOT}/scripts/desk_launch.sh"
chmod +x "${LAUNCH}"

APPS_DIR="${HOME}/.local/share/applications"
mkdir -p "${APPS_DIR}"

DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
[[ -d "${DESKTOP_DIR}" ]] || DESKTOP_DIR="${HOME}/Desktop"
mkdir -p "${DESKTOP_DIR}"

INSTALL_8600=false
if [[ "${1:-}" == "--both" ]]; then
  INSTALL_8600=true
fi

write_entry() {
  local file="$1" name="$2" comment="$3" port="$4" icon="$5" terminal="$6"
  cat > "${file}" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=${name}
Comment=${comment}
Exec="${LAUNCH}" ${port}
Icon=${icon}
Terminal=${terminal}
Categories=Office;Finance;
StartupNotify=true
EOF
  chmod +x "${file}"
}

write_entry "${APPS_DIR}/nifty-algo-8510.desktop" \
  "Algos — 8510 Trading Desk" \
  "Kill any old 8510 engine, start fresh, and open in your browser" \
  8510 "utilities-terminal" "false"

write_entry "${APPS_DIR}/nifty-desk-8600.desktop" \
  "Algos — NIFTY Desk (8600)" \
  "ICT/SMC research desk on port 8600 — not the algo terminal" \
  8600 "utilities-system-monitor" "true"

rm -f "${DESKTOP_DIR}/nifty-desk-8600.desktop" \
      "${DESKTOP_DIR}/nifty-algo-8510.desktop" 2>/dev/null || true

cp -f "${APPS_DIR}/nifty-algo-8510.desktop" "${DESKTOP_DIR}/"

if "${INSTALL_8600}"; then
  cp -f "${APPS_DIR}/nifty-desk-8600.desktop" "${DESKTOP_DIR}/"
fi

if command -v gio >/dev/null 2>&1; then
  gio set "${DESKTOP_DIR}/nifty-algo-8510.desktop" metadata::trusted true 2>/dev/null || true
  if [[ -f "${DESKTOP_DIR}/nifty-desk-8600.desktop" ]]; then
    gio set "${DESKTOP_DIR}/nifty-desk-8600.desktop" metadata::trusted true 2>/dev/null || true
  fi
fi
command -v update-desktop-database >/dev/null 2>&1 && \
  update-desktop-database "${APPS_DIR}" 2>/dev/null || true

echo "Installed 8510 launcher on desktop:"
echo "  ${DESKTOP_DIR}/nifty-algo-8510.desktop"
echo "Menu entry:"
echo "  ${APPS_DIR}/nifty-algo-8510.desktop"
if "${INSTALL_8600}"; then
  echo "Also on desktop: ${DESKTOP_DIR}/nifty-desk-8600.desktop"
else
  echo "Removed legacy 8600 icon from desktop (8600 remains in app menu as ICT desk)."
  echo "To restore both icons: bash scripts/install_desktop_icons.sh --both"
fi
echo
echo "Launcher: ${LAUNCH}"
echo "Double-click 'Algos — 8510 Trading Desk' — stops old 8510, starts fresh, opens browser."
