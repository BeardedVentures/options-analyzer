@echo off
REM ── VEGA local web app (with a fresh scan first) ─────────────────
cd /d "%~dp0"
echo.
echo === Step 1/2: scanning today's option chains (free, ~15-min delayed, 1-2 min) ===
python vega_candidates.py --no-open
echo.
echo === Step 2/2: starting the VEGA app ===
echo Your browser will open at http://127.0.0.1:8765
echo Leave this window open while you use it. Close it (or Ctrl+C) to stop.
echo.
python vega_app.py
pause
