@echo off
REM ============================================================
REM  VEGA — LIVE launcher (v3.1). Pulls live (~15-min delayed)
REM  Yahoo data and runs the full engine:
REM    * bull-put (proven)  + bear-call + iron-condor (live, verify-flagged)
REM    * lottery single-call scanner (live, speculative)
REM  Frees port 8765 first so no stale server shadows the new UI.
REM  For an OFFLINE demo review use Launch_VEGA_BETA.bat instead.
REM
REM  Every step's exit code is checked. A scan that fails must NEVER fall through
REM  to the cockpit: it would serve the previous run's artifact under a LIVE banner.
REM ============================================================
title VEGA (LIVE)
cd /d "%~dp0"
set "PY=c:\Users\Josh\AI_OS\AI_OS\architecture\Jarvis\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo === freeing port 8765 (closing any old VEGA) ===
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8765 ^| findstr LISTENING') do taskkill /PID %%a /F >nul 2>&1

echo === engine scan (LIVE Yahoo, all defined-risk strategies; ~1-3 min) ===
"%PY%" main.py
if errorlevel 1 (
    echo.
    echo *** ENGINE SCAN FAILED — the board would show the PREVIOUS scan's numbers. ***
    echo *** Fix the scan before trading off this board. Cockpit not started.       ***
    echo.
    pause
    exit /b 1
)

echo === lottery scan (LIVE single calls) ===
"%PY%" lottery_scanner.py
if errorlevel 1 echo *** WARNING: lottery scan failed — the Lottery tab may be stale. ***

echo === integrity check ===
"%PY%" verify_numbers.py
if errorlevel 1 (
    echo.
    echo *** INTEGRITY CHECK FAILED — rows did not reconcile, or the scan is STALE.  ***
    echo *** Do NOT trade off these numbers until this is understood.                ***
    echo.
    choice /c YN /m "Start the cockpit anyway (review-only)"
    if errorlevel 2 exit /b 1
)

echo === starting cockpit at http://127.0.0.1:8765 ===
echo Bear-call / iron-condor / lottery are LIVE but VERIFY-flagged: spot-check vs your broker first.
"%PY%" vega_app.py
pause
