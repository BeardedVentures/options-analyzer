param(
    [string]$PythonExe = "c:/Users/Josh/AI_OS/AI_OS/architecture/Jarvis/.venv/Scripts/python.exe"
)

$ErrorActionPreference = "Stop"

# Force UTF-8 for all child Python processes. Under Task Scheduler, stdout defaults to the
# cp1252 codepage, so the first non-ASCII print (e.g. the "Δ" delta symbol in vega_candidates.py)
# raised UnicodeEncodeError and aborted the whole cycle with exit 1 — the root cause of the
# stalled re-mark loop. These env vars propagate to every subprocess spawned by the cycle.
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

$logDir = Join-Path $projectRoot "output\paper_desk"
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}

$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$logFile = Join-Path $logDir "auto_paper_cycle.log"

"[$ts] Starting auto paper cycle" | Out-File -FilePath $logFile -Encoding utf8 -Append

# Windows PowerShell 5.1 wraps EVERY line a native exe writes to stderr in a NativeCommandError
# ErrorRecord. Under $ErrorActionPreference = "Stop" that is a TERMINATING error, so a single
# harmless stderr line from Python — a yfinance warning, a deprecation notice — aborted this
# script right here: the cycle died mid-run, "Finished" never logged, the lock was left behind
# and the task reported exit 1. That is what happened on 2026-07-25 and again on 2026-07-31
# 08:35, and it is why the exit-1 failures kept recurring after the PYTHONUTF8 fix (that fix
# removed one SOURCE of stderr, not the mechanism). Drop to Continue for the native call and
# gate on the real exit code instead.
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $PythonExe "auto_paper_cycle.py" 2>&1 | Out-File -FilePath $logFile -Encoding utf8 -Append
$cycleExit = $LASTEXITCODE
$ErrorActionPreference = $prevEAP

"[$(Get-Date -Format "yyyy-MM-dd HH:mm:ss")] Finished auto paper cycle (exit=$cycleExit)" |
    Out-File -FilePath $logFile -Encoding utf8 -Append

# Surface the cycle's real exit code to Task Scheduler so a genuine failure is visible in
# LastTaskResult instead of being masked by the wrapper always succeeding.
exit $cycleExit
