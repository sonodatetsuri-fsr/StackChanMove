@echo off
rem Flash firmware (core = CoreS3, remote = M5StickC Plus). Runs scripts\flash_firmware_windows.ps1.
setlocal
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\flash_firmware_windows.ps1" %*
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%
