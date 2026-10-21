@echo off
REM ============================================================
REM  VeriMind - Government-procurement mode launcher
REM  Same as start.cmd, but injects the askdata_gov config:
REM    DATABASE_URL / PG_DSN / RAG_DSN -> askdata_gov
REM    SCHEMA_META_PATH -> infra/schema_meta_gov.json
REM    APP_TIME_ANCHOR  -> 2026-10-17
REM  .env stays untouched (Chinook defaults), so pytest stays green.
REM ============================================================
title VeriMind - Government mode

where pwsh >nul 2>nul
if %errorlevel%==0 (
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-gov.ps1" %*
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-gov.ps1" %*
)

echo.
echo Launcher exited. Press any key to close this window...
pause >nul
