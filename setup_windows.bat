@echo off
rem Windows setup entry point (double-click). Runs scripts\setup_windows.ps1.
setlocal
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_windows.ps1" %*
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%
