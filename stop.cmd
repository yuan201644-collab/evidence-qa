@echo off
REM ============================================================
REM  VeriMind - Stop services
REM  Stops frontend and backend. Database container is kept.
REM  Add --IncludeDatabase to stop the database too.
REM ============================================================
title VeriMind - Stopping

where pwsh >nul 2>nul
if %errorlevel%==0 (
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1" %*
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1" %*
)

echo.
echo Press any key to close this window...
pause >nul
