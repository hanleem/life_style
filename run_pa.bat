@echo off
rem Family assistant server launcher. Restarts automatically if the server stops.
rem Started by the "family assistant" scheduled task (at logon + every 5 min watchdog; a running copy blocks duplicates).
title Family assistant server
cd /d "%~dp0"
set "PYTHONUTF8=1"
if not exist data mkdir data

:loop
echo [%date% %time%] starting server...
echo [%date% %time%] start>> data\pa_restarts.log
uv run python -m pa.main
echo [%date% %time%] server stopped. Restarting in 10 seconds (close this window to stop).
echo [%date% %time%] stopped (exit %errorlevel%)>> data\pa_restarts.log
timeout /t 10 /nobreak >nul
goto loop
