@echo off
rem Family assistant installer. Double-click this file. (Korean messages are shown by install.ps1)
chcp 65001 >nul
cd /d "%~dp0"
title Family assistant setup
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
echo.
pause
