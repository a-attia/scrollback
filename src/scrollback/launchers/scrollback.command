#!/bin/bash
# Double-clickable launcher for macOS (Finder: double-click this file).
# Starts the scrollback web app and opens it in your default browser.
#
# Install a copy you can double-click with:  scrollback install-launcher
# First-time setup: right-click -> Open (to bypass Gatekeeper once).
#
# If `scrollback` is not on PATH, this falls back to `python3 -m scrollback`.

if command -v scrollback >/dev/null 2>&1; then
  exec scrollback web
elif python3 -c "import scrollback" >/dev/null 2>&1; then
  exec python3 -m scrollback.cli web
else
  echo "scrollback is not installed."
  echo "Install it with:  pipx install scrollback"
  echo
  read -r -p "Press Return to close."
fi
