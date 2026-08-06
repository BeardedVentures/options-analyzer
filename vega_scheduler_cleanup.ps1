<#
  vega_scheduler_cleanup.ps1  —  removes ALL VEGA Windows scheduled tasks.

  As of 2026-07-23 the market-hours cadence lives INSIDE the cockpit (vega_app.py):
  a background scheduler refreshes the board every 15 min and runs the paper cycle
  hourly, but ONLY while the cockpit is running and US options are open. There are
  therefore no scheduled tasks to maintain — this script tears down the old ones
  (the around-the-clock 2-hourly task, the fixed-time daily/checkpoint tasks, etc.)
  and clears any stale automation lock.

  Idempotent — safe to re-run. Runs under your user (no admin needed in most setups;
  if it errors on permissions, run from an elevated PowerShell).

  Run:  right-click > Run with PowerShell,  or:
        powershell -ExecutionPolicy Bypass -File .\vega_scheduler_cleanup.ps1

  To re-refresh the board / trade on paper, just launch the cockpit:
        run_vega_app.bat        (or Launch_VEGA.bat)
#>
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "VEGA scheduled tasks before cleanup:" -ForegroundColor Cyan
$before = Get-ScheduledTask | Where-Object { $_.TaskName -like 'VEGA*' }
if ($before) { $before | ForEach-Object { Write-Host ("  {0}  [{1}]" -f $_.TaskName, $_.State) } }
else { Write-Host "  (none)" }

# 1) Remove every VEGA_* task — scheduling now lives in the cockpit, not Task Scheduler.
foreach ($t in $before) {
    try {
        Unregister-ScheduledTask -TaskName $t.TaskName -Confirm:$false -ErrorAction Stop
        Write-Host ("Removed task: {0}" -f $t.TaskName) -ForegroundColor Yellow
    } catch {
        Write-Host ("FAILED to remove {0}: {1}" -f $t.TaskName, $_.Exception.Message) -ForegroundColor Red
    }
}

# 2) Clear a stale automation lock so the cockpit's paper cycle can acquire it cleanly.
$lock = Join-Path $root "logs\auto_paper_cycle.lock"
if (Test-Path $lock) {
    Remove-Item $lock -Force
    Write-Host "Cleared automation lock (logs\auto_paper_cycle.lock)." -ForegroundColor Yellow
}

Write-Host "`nVEGA scheduled tasks after cleanup:" -ForegroundColor Cyan
$after = Get-ScheduledTask | Where-Object { $_.TaskName -like 'VEGA*' }
if ($after) { $after | ForEach-Object { Write-Host ("  {0}  [{1}]" -f $_.TaskName, $_.State) } }
else { Write-Host "  (none — all VEGA scheduled tasks removed)" -ForegroundColor Green }

Write-Host "`nDone. The board now refreshes from inside the cockpit during market hours." -ForegroundColor Cyan
Write-Host "Launch it with:  run_vega_app.bat   (keep the window open during the trading day)."
