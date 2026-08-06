@echo off
REM ── VEGA Paper Desk one-click runner ─────────────────────────────
REM Runs the real candidate scan on today's free (~15-min delayed) data,
REM renders the paper-desk cockpit, and opens both in your browser.
cd /d "%~dp0"
echo.
echo === VEGA: scanning live option chains (25-45 DTE)... this can take 1-2 min ===
python vega_candidates.py
echo.
echo === VEGA: rendering paper-desk dashboard ===
python paper_desk.py dashboard
echo.
echo Done. Two tabs should have opened in your browser:
echo   1) Real spread candidates (today)
echo   2) Paper Desk cockpit
echo.
pause
