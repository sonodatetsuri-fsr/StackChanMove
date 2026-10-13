@echo off
rem Start VOICEVOX ENGINE, Ollama and the Hub (double-click). Runs scripts\start_windows.ps1.
setlocal
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_windows.ps1" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo start_windows exited with code %RC%. See logs\hub.log
  pause
)
exit /b %RC%
