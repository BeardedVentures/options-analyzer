param(
    [string]$PythonExe = "c:/Users/Josh/AI_OS/AI_OS/architecture/Jarvis/.venv/Scripts/python.exe"
)

# End-of-day resolution run: reprice all open paper positions and auto-close by
# target/stop/DTE. Does NOT open new trades (that's --mark-only).

$ErrorActionPreference = "Stop"

# Force UTF-8 for the child Python process. Task Scheduler hands the script a cp1252 stdout, and
# the cycle prints characters cp1252 cannot encode — the delta in the ravens output, and the ±
# in the BTC forecast line ("flat band ±3.4%"). A UnicodeEncodeError there kills the run with
# exit 1 AFTER work has been done, so trades get opened and the log says the cycle failed.
# run_auto_paper_cycle.ps1 has had this since the 2026-07 fix; this script never got it.
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
# ...and make PowerShell DECODE the child's utf-8 stdout as utf-8 before it reaches the log.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

$logDir = Join-Path $projectRoot "output\paper_desk"
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}
$logFile = Join-Path $logDir "auto_paper_cycle.log"

"[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Starting auto paper cycle (mark-only)" | Out-File -FilePath $logFile -Encoding utf8 -Append
& $PythonExe "auto_paper_cycle.py" "--mark-only" 2>&1 | Out-File -FilePath $logFile -Encoding utf8 -Append
"[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Finished auto paper cycle (mark-only)" | Out-File -FilePath $logFile -Encoding utf8 -Append
