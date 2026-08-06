param(
    [string]$PythonExe = "c:/Users/Josh/AI_OS/AI_OS/architecture/Jarvis/.venv/Scripts/python.exe"
)

# End-of-day resolution run: reprice all open paper positions and auto-close by
# target/stop/DTE. Does NOT open new trades (that's --mark-only).

$ErrorActionPreference = "Stop"

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
