@echo off
REM ============================================================
REM  VeriMind - One-click launcher
REM  Double-click this file. It will:
REM    1. check the PostgreSQL container
REM    2. start the backend  (127.0.0.1:8000)
REM    3. start the frontend (127.0.0.1:5184, verified source)
REM  First run loads AI models (~6.5 GB), takes 30-60 seconds.
REM ============================================================
title VeriMind - Starting

where pwsh >nul 2>nul
if %errorlevel%==0 (
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
)

echo.
echo Launcher exited. Press any key to close this window...
pause >nul
