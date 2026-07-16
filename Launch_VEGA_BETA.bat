@echo off
REM ============================================================
REM  VEGA BETA REVIEW launcher — populates every screener with
REM  CRITERIA-COMPLIANT demo data (produced by strategies.py) so
REM  you can review the full system offline: all menus, all
REM  strategy screeners, criteria + news validation, lottery.
REM  For LIVE data use Launch_VEGA.bat instead.
REM ============================================================
title VEGA Beta Review
cd /d "%~dp0"
set "PY=c:\Users\Josh\AI_OS\AI_OS\architecture\Jarvis\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo === freeing port 8765 (closing any old VEGA) ===
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8765 ^| findstr LISTENING') do taskkill /PID %%a /F >nul 2>&1

echo === seeding criteria-compliant demo data (bull put / bear call / iron condor / lottery) ===
"%PY%" seed_demo.py

echo === integrity check ===
"%PY%" verify_numbers.py

echo === starting the cockpit at http://127.0.0.1:8765 ===
echo Review: Today (3 strategies, click rows), Lottery tab, Open, History.
"%PY%" vega_app.py
pause
