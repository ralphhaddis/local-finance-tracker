@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_tracker.ps1"
exit /b %ERRORLEVEL%
