#!/bin/bash
# Launcher for Linux / BSD. Make executable (chmod +x scrollback.sh) and run,
# or wire it into a .desktop entry (see scrollback.desktop).

if command -v scrollback >/dev/null 2>&1; then
  exec scrollback web --window
elif python3 -c "import scrollback" >/dev/null 2>&1; then
  exec python3 -m scrollback.cli web --window
else
  echo "scrollback is not installed. Install with: pipx install scrollback"
  exit 1
fi
