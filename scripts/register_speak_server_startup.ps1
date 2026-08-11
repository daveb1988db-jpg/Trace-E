<#
.SYNOPSIS
  Register Trace-E speak_server to start at Windows boot (BEFORE login) as SYSTEM.

.DESCRIPTION
  Creates scheduled task "TraceE-SpeakServer" — At startup, highest privileges,
  run whether user is logged on or not (SYSTEM), restart on failure.
  Grants SYSTEM read/execute on the repo so Python under the user profile can run.
  Run this script once from an elevated PowerShell.
#>
[CmdletBinding()]
param(
  [string]$RepoRoot = "C:\Users\Bartl\Projects\Trace-E",
  [string]$TaskName = "TraceE-SpeakServer",
  [string]$EspBase = "http://192.168.1.104",
  [string]$Chirps = "off"
)

$ErrorActionPreference = "Stop"

function Test-IsAdmin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  $p = New-Object Security.Principal.WindowsPrincipal($id)
  return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdmin)) {
  Write-Host "ERROR: Run elevated (Administrator). Right-click PowerShell → Run as administrator," -ForegroundColor Red
  Write-Host "  then:  powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`""
  exit 1
}

$StartScript = Join-Path $RepoRoot "scripts\start_speak_server.ps1"
if (-not (Test-Path $StartScript)) { throw "Missing $StartScript" }
if (-not (Test-Path (Join-Path $RepoRoot ".venv-nav\Scripts\python.exe"))) {
  throw "Missing .venv-nav Python at $RepoRoot\.venv-nav\Scripts\python.exe"
}

$LogDir = Join-Path $RepoRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

Write-Host "Granting SYSTEM read/execute on repo (needed for pre-logon)..."
# SYSTEM often cannot read C:\Users\<user>\... without an explicit grant
& icacls $RepoRoot /grant "NT AUTHORITY\SYSTEM:(OI)(CI)RX" /T /C /Q 2>&1 | Select-Object -Last 5

$arg = "-NoProfile -ExecutionPolicy Bypass -File `"$StartScript`" -RepoRoot `"$RepoRoot`" -EspBase `"$EspBase`" -Chirps `"$Chirps`" -ServiceMode"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arg -WorkingDirectory $RepoRoot

$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = "PT45S"  # brief delay so NICs/services settle

$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable `
  -RestartCount 10 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit (New-TimeSpan -Days 0) `
  -MultipleInstances IgnoreNew

# ExecutionTimeLimit 0 = no limit (infinite) on modern PS; if ignored, also clear via schtasks
$principal = New-ScheduledTaskPrincipal -UserId "NT AUTHORITY\SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

# Ensure no time limit (some hosts still apply a default)
schtasks /Change /TN $TaskName /RL HIGHEST /RU "SYSTEM" /ENABLE | Out-Null

Write-Host ""
Write-Host "Registered scheduled task: $TaskName" -ForegroundColor Green
Write-Host "  Trigger : At startup (delay 45s), runs as SYSTEM before login"
Write-Host "  Action  : $StartScript -ServiceMode"
Write-Host "  Restart : up to 10 times, every 1 minute on failure"
Write-Host "  Log     : $LogDir\speak_server.log"
Write-Host ""
Write-Host "Person follow is NOT started on boot (safe). Use phone UI or POST /api/follow/start when supervised."
Write-Host ""
Write-Host "Verify task:"
Write-Host "  Get-ScheduledTask -TaskName $TaskName | Format-List TaskName,State"
Write-Host "  schtasks /Query /TN $TaskName /V /FO LIST"
Write-Host ""
Write-Host "After reboot (no login needed), from another device or after login:"
Write-Host "  Invoke-WebRequest http://127.0.0.1:8787/api/health -UseBasicParsing"
Write-Host ""
Write-Host "Disable / remove:"
Write-Host "  Disable:  Disable-ScheduledTask -TaskName $TaskName"
Write-Host "  Remove:   powershell -ExecutionPolicy Bypass -File `"$(Join-Path $RepoRoot 'scripts\unregister_speak_server_startup.ps1')`""
