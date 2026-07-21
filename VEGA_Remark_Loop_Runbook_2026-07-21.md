# VEGA re-mark loop — unstick runbook (2026-07-21)

**Symptom:** Track Record shows a red "Marks are 5 days stale" banner. Open positions were last
re-marked **2026-07-16**; CLV / unrealized P/L are frozen at that snapshot. The *scanner* cron is
running fine (morning scans fired 7/21), but `auto_paper_cycle.py` — the half that reprices open
positions and grades closes — has not successfully run since 7/16.

**Not the cause:** the `logs/auto_paper_cycle.lock` file. `_acquire_lock()` already auto-deletes any
lock older than 30 minutes, so a stale lock self-heals. Don't waste time on it (though step 1 clears it
anyway, harmlessly).

Run everything below from the repo root on the tower:
`C:\Users\Josh\AI_OS\AI_OS\projects\Stock Market Tools\options_intelligence`

---

## Step 1 — Refresh the marks manually right now (also the fastest diagnostic)

```powershell
cd "C:\Users\Josh\AI_OS\AI_OS\projects\Stock Market Tools\options_intelligence"
Remove-Item logs\auto_paper_cycle.lock -ErrorAction SilentlyContinue   # harmless if absent
python auto_paper_cycle.py --mark-only
```

Watch the output. Three outcomes:

- **It reprices and prints closes/marks** → the loop works; the problem was just that nothing was
  *scheduled* to call it. Go to Step 3.
- **It errors** (network / yfinance / no-quotes / traceback) → that error is the real blocker. Most
  likely a data-fetch failure pulling live option quotes. Copy the error; common fixes: confirm the
  tower has internet, that `yfinance` is installed in the same Python, and that quotes aren't rate-limited.
- **"Could not acquire lock"** → another copy is genuinely running; wait or reboot, then retry.

Then reload the cockpit's **Track Record** tab — the red stale banner should clear and `days_stale`
resets to 0.

## Step 2 — Confirm the marks actually updated

```powershell
python clv_tracker.py     # bottom line shows CLV over N open positions
```

Or just check the Track Record tab: the "Open predictions — CLV vs theta baseline" table should show
today's marks, and the banner should be gone.

## Step 3 — Make sure the mark/grade cycle is actually SCHEDULED (the real fix)

The scanner and the paper-cycle are separate jobs. The scanner is scheduled; the paper-cycle apparently
isn't (or was removed). Check what's registered:

```powershell
powershell -ExecutionPolicy Bypass -File .\vega_scheduler_status.ps1
```

- If there's **no auto-paper / mark task** (or it shows Last Run Result ≠ 0), re-register it:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_daily_paper.ps1
```

  (`setup_daily_paper.ps1` sets up the recurring paper cycle. `setup_auto_paper_2weeks.ps1` is the
  fixed 2-week validation variant — use whichever matches your intent; the daily one is the durable
  choice for the Gate-1 flywheel.)

- After registering, verify next-run time with `vega_scheduler_status.ps1` again.

## Step 4 — Schedule a dedicated end-of-day mark run (recommended)

CLV wants a fresh close-of-day mark on every open position. If the daily task only opens new trades,
add an EOD `--mark-only` pass (e.g. 3:45pm ET) so marks never drift more than a day. The task action is:

```
python "C:\Users\Josh\AI_OS\AI_OS\projects\Stock Market Tools\options_intelligence\auto_paper_cycle.py" --mark-only
```

## Step 5 — Check the log for silent failures

```powershell
Get-Content logs\run.log -Tail 40
```

Look for any `auto_paper_cycle` / mark / reprice lines and their errors. If the scanner logs appear but
the paper-cycle never does, that confirms it isn't being invoked (Step 3 is the fix). If it appears and
errors, fix that error (Step 1's manual run will have shown it).

---

### Why this matters
The whole "ever-learning" thesis — CLV, calibration, edge-retention, the Gate-1 flip from provisional to
validated — depends on open positions being re-marked and closes being graded. With the loop stalled,
predictions pile up (38 modeled) but almost nothing resolves (1 closed in the whole ledger). Getting the
mark/grade cycle running daily is the single highest-leverage fix for the learning system.
