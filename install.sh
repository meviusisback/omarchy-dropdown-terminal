#!/usr/bin/env bash
# install.sh — one-shot installer for meviusisback.dropdown-terminal.
# Installs the systemd foot server unit, the Hyprland rules + keybind, and
# enables everything. `install.sh uninstall` reverts every trace.
# argv-only exec; no eval.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
BACKEND="$SCRIPT_DIR/backend/dropdown_terminal.py"
UNIT="foot-server@dropdown-terminal.service"

msg() { printf '%s\n' "$*"; }
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

command -v foot >/dev/null 2>&1 || die "foot is required (pacman -S foot)"
command -v systemctl >/dev/null 2>&1 || die "systemctl not found"
command -v hyprctl >/dev/null 2>&1 || die "hyprctl not found (run inside a Hyprland session)"
python3 -c "import py_compile; py_compile.compile('$BACKEND', doraise=True)" || die "backend failed to compile"

if [[ "${1:-install}" == "uninstall" ]]; then
  python3 "$BACKEND" uninstall
  systemctl --user daemon-reload
  rm -f "$HOME/.local/bin/omarchy-dropdown-terminal"
  hyprctl reload 2>/dev/null || true
  msg "Uninstalled. All plugin traces removed."
  exit 0
fi

msg "Installing drop-down terminal..."
python3 "$BACKEND" install
systemctl --user daemon-reload
systemctl --user enable --now "$UNIT"
mkdir -p "$HOME/.local/bin"
ln -sfn "$SCRIPT_DIR/bin/omarchy-dropdown-terminal" "$HOME/.local/bin/omarchy-dropdown-terminal"
hyprctl reload 2>/dev/null || true

msg ""
msg "Done. Press SUPER + U to drop the terminal."
msg "Bar widget: meviusisback.dropdown-terminal (enable via omarchy plugin enable)."
msg "Uninstall any time with: install.sh uninstall"
