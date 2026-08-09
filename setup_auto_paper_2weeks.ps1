param(
    # Name kept for continuity with the registered task. It is no longer a two-week task —
    # see the trigger comment below; the 14-day window was the bug, not the design.
    [string]$TaskName = "VEGA_AutoPaper_2Weeks",
    [int]$EveryHours = 2,
    [string]$StartTime = "09:35",     # ~5 min after the US equity open
    [int]$SessionHours = 7            # 09:35 -> ~16:35, covering the session
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$runner = Join-Path $projectRoot "run_auto_paper_cycle.ps1"

if (-not (Test-Path $runner)) {
    throw "Runner script not found: $runner"
}

$argument = "-NoProfile -ExecutionPolicy Bypass -File `"$runner`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argument

# A DAILY trigger that repeats through the session, NOT a one-off with a 14-day repetition.
#
# The original form (-Once with -RepetitionDuration 14 days) expires silently. The task stays
# "Ready", LastTaskResult stays 0, and NextRunTime simply goes blank — there is no error and
# nothing in any log, so the cycle just stops running and the board quietly goes stale. It was
# caught on 2026-08-09 with three days left in the window, which would have killed the cycle
# mid-week on Thursday the 13th.
#
# A daily trigger cannot expire. Repetition covers 09:35 to ~16:35 ET so the task only wakes
# during the session instead of every two hours around the clock; auto_paper_cycle's own
# is_market_open() guard remains the authority and makes an off-hours wake harmless.
$trigger = New-ScheduledTaskTrigger -Daily -At $StartTime
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At $StartTime `
    -RepetitionInterval (New-TimeSpan -Hours $EveryHours) `
    -RepetitionDuration (New-TimeSpan -Hours $SessionHours)).Repetition

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
