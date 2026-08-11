@echo off
:: Trace-E: register pre-logon speak_server startup (requires Admin / UAC Yes)
cd /d "%~dp0\.."
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0register_speak_server_startup.ps1"
echo.
pause
