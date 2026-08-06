param(
    [string]$TaskName = "VEGA_AutoPaper_2Weeks",
    [int]$EveryHours = 2
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$runner = Join-Path $projectRoot "run_auto_paper_cycle.ps1"

if (-not (Test-Path $runner)) {
    throw "Runner script not found: $runner"
}

$now = Get-Date
$start = $now.AddMinutes(2)
$end = $start.AddDays(14)

$argument = "-NoProfile -ExecutionPolicy Bypass -File `"$runner`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argument

$trigger = New-ScheduledTaskTrigger -Once -At $start `
    -RepetitionInterval (New-TimeSpan -Hours $EveryHours) `
    -RepetitionDuration (New-TimeSpan -Days 14)

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

# Remove any existing task with same name to ensure deterministic settings.
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "VEGA auto paper cycle: analyze, select, grade for 14 days" | Out-Null

Write-Output "Scheduled task created: $TaskName"
Write-Output "Start: $($start.ToString('yyyy-MM-dd HH:mm:ss'))"
Write-Output "End:   $($end.ToString('yyyy-MM-dd HH:mm:ss'))"
Write-Output "Every: $EveryHours hour(s)"

Start-ScheduledTask -TaskName $TaskName
Write-Output "Task started immediately."
