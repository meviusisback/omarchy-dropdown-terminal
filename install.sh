#!/usr/bin/bash
# install.sh — one-shot installer for meviusisback.dropdown-terminal.
#
# Deliberately thin: the trusted-tool resolution and the actual install /
# uninstall work live in bin/omarchy-dropdown-terminal, so the plugin has
# exactly ONE PATH-free lookup implementation (see that file's header for the
# trust rules). `./install.sh` is `omarchy-dropdown-terminal install`, and
# `./install.sh uninstall` is `omarchy-dropdown-terminal uninstall`.
set -euo pipefail

SCRIPT_DIR="$(cd -- "${BASH_SOURCE[0]%/*}" && pwd -P)"
exec "$SCRIPT_DIR/bin/omarchy-dropdown-terminal" "${1:-install}"
