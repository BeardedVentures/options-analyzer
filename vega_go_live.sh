#!/usr/bin/env bash
# VEGA Beta — Go-Live runbook (run in Git Bash on the JARVIS tower)
# Generated 2026-07-17. Safe to run step-by-step; each section is independent.
set -u
SCAN="/c/Users/Josh/AI_OS/AI_OS/projects/Stock Market Tools/options_intelligence"
JAR="/c/Users/Josh/AI_OS/AI_OS/architecture/Jarvis"

echo "==================================================================="
echo "STEP 1 — SCANNER: commit beta engine + push (REQUIRED: Actions runs origin/main)"
echo "==================================================================="
cd "$SCAN" || exit 1
rm -f .git/index.lock 2>/dev/null
git add main.py config.py analysis/edge_calculator.py data/fetcher.py lottery_scanner.py vega_app.py
git commit -m "feat(vega): beta build - dedup, IV-skew, book awareness, post-earnings, cockpit flags/footer + lottery" || echo "(nothing to commit or already committed - continuing)"
echo "--- pushing scanner (this also pushes earlier 5b937ec) ---"
git push origin main
echo ">>> Confirm on GitHub: Actions tab should show the beta commit on main."

echo
echo "==================================================================="
echo "STEP 2 — JARVIS: apply migration 029 to Supabase (LOCAL - no git needed)"
echo "==================================================================="
cd "$JAR" || exit 1
docker exec -i supabase-db psql -U postgres -d jarvis < db/029_vega_beta_performance_views.sql
echo "--- verify columns + views ---"
docker exec -i supabase-db psql -U postgres -d jarvis -c "\d vega_trade_outcomes" | grep -E "delta_at_entry|skew_at_entry"
docker exec -i supabase-db psql -U postgres -d jarvis -c "\dv vega_performance_*"

echo
echo "==================================================================="
echo "STEP 3 — JARVIS: restart FastAPI onto new router (hot-mounted ./app)"
echo "==================================================================="
docker compose restart fastapi
sleep 5
echo "--- smoke-test the 7 new endpoints ---"
curl -s http://localhost:8000/vega/performance/calibration ; echo
curl -s http://localhost:8000/vega/book/open-tickers ; echo
curl -s http://localhost:8000/vega/skew/SPY ; echo
echo ">>> calibration returning {\"status\":\"no_data\"} is CORRECT until >=30 trades close."

echo
echo "==================================================================="
echo "STEP 4 — SCANNER: run one beta scan + verify artifact"
echo "==================================================================="
cd "$SCAN" || exit 1
python main.py
echo "--- verify scan_latest.json has beta fields + no dup tickers ---"
python - <<'PY'
import json
d=json.load(open("logs/scan_latest.json"))
print("has book{}:", "book" in d)
tr=d.get("qualified_trades",[])
tk=[t.get("ticker") for t in tr]
print("tickers:",tk)
print("no dup tickers:", len(tk)==len(set(tk)))
print("first trade has skew_vol_pts:", "skew_vol_pts" in (tr[0] if tr else {}))
print("first trade has already_in_position:", "already_in_position" in (tr[0] if tr else {}))
PY
echo
echo "DONE. Open the cockpit (Launch_VEGA.bat) and eyeball Today (flags+footer) + Lottery."
