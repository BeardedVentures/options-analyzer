<#
  vega_scheduler_status.ps1  —  READ ONLY. Changes nothing.
  Shows the live state of VEGA's automated paper-trading routine:
    1) which VEGA scheduled tasks exist, their state, last/next run, last result
    2) whether the automation lock is held (and whether it's stale/blocking)
    3) how the outcomes ledger is growing (open vs closed = resolved predictions)

  Run:  right-click > Run with PowerShell,  or  in a terminal:
        powershell -ExecutionPolicy Bypass -File .\vega_scheduler_status.ps1
#>
$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "================ VEGA AUTOMATION STATUS ================" -ForegroundColor Cyan
Write-Host ("Checked: {0}" -f (Get-Date))

# ---- 1. Scheduled tasks -------------------------------------------------
Write-Host "`n-- Scheduled tasks (VEGA*) --" -ForegroundColor Cyan
$tasks = Get-ScheduledTask | Where-Object { $_.TaskName -like 'VEGA*' }
if (-not $tasks) {
    Write-Host "  NONE registered named VEGA*  ->  the routine is NOT scheduled." -ForegroundColor Yellow
} else {
    foreach ($t in $tasks) {
        $info = $t | Get-ScheduledTaskInfo
        Write-Host ("`n  {0}    [state: {1}]" -f $t.TaskName, $t.State) -ForegroundColor White
        Write-Host ("    Last run : {0}   (result 0x{1:X8})" -f $info.LastRunTime, $info.LastTaskResult)
        Write-Host ("    Next run : {0}" -f $info.NextRunTime)
        $i = 0
        foreach ($tr in $t.Triggers) { $i++; Write-Host ("    Trigger {0}: {1}" -f $i, $tr) }
    }
    # After cleanup the intended set is 2: VEGA_DailyMorningScan (open+manage) and
    # VEGA_EOD_Mark (end-of-day resolve). More than that means leftover/overlapping tasks.
    if ($tasks.Count -gt 2) {
        Write-Host ("`n  ! {0} VEGA tasks registered — expected 2 (DailyMorningScan + EOD_Mark). Extra tasks may be colliding; run vega_scheduler_cleanup.ps1." -f $tasks.Count) -ForegroundColor Yellow
    }
}

# ---- 2. Automation lock -------------------------------------------------
Write-Host "`n-- Automation lock --" -ForegroundColor Cyan
$lock = Join-Path $root 'logs\auto_paper_cycle.lock'
if (Test-Path $lock) {
    $age = [int](New-TimeSpan -Start (Get-Item $lock).LastWriteTime -End (Get-Date)).TotalMinutes
    Write-Host ("  PRESENT  pid={0}  age={1} min  (auto-treated as stale after 180 min)" -f (Get-Content $lock), $age)
    if ($age -gt 180) { Write-Host "  -> stale; the next run will clear it and proceed." -ForegroundColor Yellow }
    else { Write-Host ("  -> runs will SKIP for the next {0} min until this clears." -f (180 - $age)) -ForegroundColor Yellow }
} else {
    Write-Host "  none held (clear)." -ForegroundColor Green
}

# ---- 3. Outcomes ledger -------------------------------------------------
Write-Host "`n-- Outcomes ledger (tracked predictions) --" -ForegroundColor Cyan
$ledger = Join-Path $root 'logs\vega_outcomes.jsonl'
if (Test-Path $ledger) {
    $lines = Get-Content $ledger | Where-Object { $_.Trim() }
    $open = 0; $closed = 0; $auto = 0
    $dates = @{}
    foreach ($l in $lines) {
        if ($l -match '"status":\s*"open"')       { $open++ }
        if ($l -match '"status":\s*"closed"')      { $closed++ }
        if ($l -match '"source":\s*"auto-paper"')  { $auto++ }
        if ($l -match '"opened_at":\s*"([0-9]{4}-[0-9]{2}-[0-9]{2})') { $dates[$Matches[1]] = $true }
    }
    Write-Host ("  {0} records: {1} open, {2} closed   (goal: 30 closed before trusting the edge)" -f $lines.Count, $open, $closed)
    Write-Host ("  auto-opened by the routine: {0}   |   distinct trading days with entries: {1}" -f $auto, $dates.Keys.Count)
    if ($closed -lt 5) { Write-Host "  -> resolved predictions are still very low; auto-close needs live marks from market-hours runs." -ForegroundColor Yellow }
} else {
    Write-Host "  no ledger file yet (nothing logged)." -ForegroundColor Yellow
}
Write-Host "`n=======================================================" -ForegroundColor Cyan
