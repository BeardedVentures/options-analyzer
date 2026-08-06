param(
    [string]$TaskName  = "VEGA_DailyMorningScan",
    [string]$RunAt     = "08:35",          # local time (CST) — matches market open window
    [int]$MaxNewPerRun = 5,
    [int]$MaxOpenTotal = 15
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$runner      = Join-Path $projectRoot "run_auto_paper_cycle.ps1"

if (-not (Test-Path $runner)) {
    throw "Runner script not found: $runner"
}

# Defaults (5 trades/run, 15 max open) are baked into auto_paper_cycle.py.
$argument  = "-NoProfile -ExecutionPolicy Bypass -File `"$runner`""

$action   = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argument

# Weekdays only (Mon-Fri), daily, no end date
$trigger  = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At $RunAt

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

# Remove previous registration if it exists
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName   $TaskName `
    -Action     $action `
    -Trigger    $trigger `
    -Settings   $settings `
    -Description "VEGA daily morning scan — target $MaxNewPerRun paper trades per day, weekdays at $RunAt" | Out-Null

Write-Output "Task created : $TaskName"
Write-Output "Runs at      : $RunAt on Mon-Fri (no end date)"
Write-Output "Targets      : $MaxNewPerRun new trades/day, max $MaxOpenTotal open"

# Run immediately so today's remaining trades get picked up now
Start-ScheduledTask -TaskName $TaskName
Write-Output "Task started immediately for today."
