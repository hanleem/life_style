@echo off
rem Family assistant doctor. Double-click this file. (Korean messages are shown by tools/doctor.py)
chcp 65001 >nul
cd /d "%~dp0"
title Family assistant doctor
set "PYTHONUTF8=1"
set "PATH=%PATH%;%ProgramFiles%\Tailscale;%APPDATA%\npm;%USERPROFILE%\.local\bin"
where uv >nul 2>nul
if errorlevel 1 (
  echo [!] uv was not found. Please run the setup file again.
  echo.
  pause
  exit /b 1
)
uv run python -m tools.doctor --interactive
echo.
pause
