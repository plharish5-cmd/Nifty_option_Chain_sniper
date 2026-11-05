#!/usr/bin/env bash
# install_desktop_icons.sh — puts "Algos" launchers on the desktop and in the
# application menu. Generates the .desktop files with this repo's real absolute
# path, so it works regardless of where the repo lives (including paths with
# spaces, which this one has).
#
#   bash scripts/install_desktop_icons.sh
#
# Re-runnable: overwrites its own files, touches nothing else.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCH="${REPO_ROOT}/scripts/desk_launch.sh"
chmod +x "${LAUNCH}"

APPS_DIR="${HOME}/.local/share/applications"
mkdir -p "${APPS_DIR}"

# The desktop folder is localised on some systems; ask xdg, fall back to ~/Desktop.
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
[[ -d "${DESKTOP_DIR}" ]] || DESKTOP_DIR="${HOME}/Desktop"
mkdir -p "${DESKTOP_DIR}"

write_entry() {
  local file="$1" name="$2" comment="$3" port="$4" icon="$5"
  cat > "${file}" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=${name}
Comment=${comment}
Exec=bash -c '"${LAUNCH}" ${port}'
Icon=${icon}
Terminal=true
Categories=Office;Finance;
StartupNotify=true
EOF
  chmod +x "${file}"
}

write_entry "${APPS_DIR}/nifty-desk-8600.desktop" \
  "Algos — NIFTY Desk" "ICT/SMC Desk on port 8600" 8600 "utilities-system-monitor"
write_entry "${APPS_DIR}/nifty-algo-8510.desktop" \
  "Algos — Algo Terminal" "Algo execution terminal on port 8510" 8510 "utilities-terminal"

cp -f "${APPS_DIR}/nifty-desk-8600.desktop" "${DESKTOP_DIR}/"
cp -f "${APPS_DIR}/nifty-algo-8510.desktop" "${DESKTOP_DIR}/"

# GNOME 42+ requires desktop files to be explicitly trusted before they will run.
if command -v gio >/dev/null 2>&1; then
  gio set "${DESKTOP_DIR}/nifty-desk-8600.desktop" metadata::trusted true 2>/dev/null || true
  gio set "${DESKTOP_DIR}/nifty-algo-8510.desktop" metadata::trusted true 2>/dev/null || true
fi
command -v update-desktop-database >/dev/null 2>&1 && \
  update-desktop-database "${APPS_DIR}" 2>/dev/null || true

echo "Installed:"
echo "  ${DESKTOP_DIR}/nifty-desk-8600.desktop"
echo "  ${DESKTOP_DIR}/nifty-algo-8510.desktop"
echo "  ${APPS_DIR}/nifty-desk-8600.desktop"
echo "  ${APPS_DIR}/nifty-algo-8510.desktop"
echo
echo "Launcher script: ${LAUNCH}"
echo "Search 'Algos' in your application menu, or double-click the desktop icons."
echo "If a desktop icon shows as untrusted, right-click it and choose 'Allow Launching'."
