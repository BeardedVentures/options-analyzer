<#
  install_desktop_shortcut.ps1
  Creates a "VEGA Paper Desk" shortcut on your Desktop that launches Launch_VEGA.bat
  (fresh ~15-min-delayed scan, then opens the cockpit in your browser).
  Run once:  right-click this file > Run with PowerShell.  Re-running just refreshes it.
#>
$ErrorActionPreference = "Stop"
$root   = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $root "Launch_VEGA.bat"
if (-not (Test-Path $target)) { throw "Launcher not found next to this script: $target" }

$desktop = [Environment]::GetFolderPath("Desktop")
$lnkPath = Join-Path $desktop "VEGA Paper Desk.lnk"

$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnkPath)
$sc.TargetPath       = $target
$sc.WorkingDirectory = $root
$sc.WindowStyle      = 1
$sc.Description       = "Launch VEGA Paper Desk - fresh ~15-min delayed scan, then the cockpit in your browser"
# Cosmetic icon (a chart-style glyph from shell32). Change or remove if you prefer.
$sc.IconLocation     = "$env:SystemRoot\System32\shell32.dll,13"
$sc.Save()

Write-Host ("Created desktop shortcut: {0}" -f $lnkPath) -ForegroundColor Green
Write-Host  "Double-click 'VEGA Paper Desk' on your desktop to launch." -ForegroundColor Cyan
