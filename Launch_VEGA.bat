@echo off
REM ============================================================
REM  VEGA — LIVE launcher (v3.1). Pulls live (~15-min delayed)
REM  Yahoo data and runs the full engine:
REM    * bull-put (proven)  + bear-call + iron-condor (live, verify-flagged)
REM    * lottery single-call scanner (live, speculative)
REM  Frees port 8765 first so no stale server shadows the new UI.
REM  For an OFFLINE demo review use Launch_VEGA_BETA.bat instead.
REM ============================================================
title VEGA (LIVE)
cd /d "%~dp0"
set "PY=c:\Users\Josh\AI_OS\AI_OS\architecture\Jarvis\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo === freeing port 8765 (closing any old VEGA) ===
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8765 ^| findstr LISTENING') do taskkill /PID %%a /F >nul 2>&1

echo === engine scan (LIVE Yahoo, all defined-risk strategies; ~1-3 min) ===
"%PY%" main.py

echo === lottery scan (LIVE single calls) ===
"%PY%" lottery_scanner.py

echo === integrity check ===
"%PY%" verify_numbers.py

echo === starting cockpit at http://127.0.0.1:8765 ===
echo Bear-call / iron-condor / lottery are LIVE but VERIFY-flagged: spot-check vs your broker first.
"%PY%" vega_app.py
pause
