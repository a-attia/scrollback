@echo off
REM Double-clickable launcher for Windows.
REM Starts the scrollback web app in a standalone browser window.

where scrollback >nul 2>nul
if %errorlevel%==0 (
  scrollback web --window
  goto :eof
)

python -c "import scrollback" >nul 2>nul
if %errorlevel%==0 (
  python -m scrollback.cli web --window
  goto :eof
)

echo scrollback is not installed.
echo Install it with:  pipx install scrollback
pause
