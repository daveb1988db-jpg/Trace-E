<#
.SYNOPSIS
  Disable and remove the Trace-E speak_server startup scheduled task.
#>
[CmdletBinding()]
param(
  [string]$TaskName = "TraceE-SpeakServer",
  [switch]$StopRunning
)

$ErrorActionPreference = "Continue"

function Test-IsAdmin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  $p = New-Object Security.Principal.WindowsPrincipal($id)
  return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdmin)) {
  Write-Host "ERROR: Run elevated (Administrator) to remove the SYSTEM startup task." -ForegroundColor Red
  exit 1
}

if ($StopRunning) {
  try {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  } catch { }
  try {
    $owners = Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue |
      Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($p in $owners) {
      Write-Host "Stopping PID $p on :8787"
      Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
    }
  } catch { }
}

$t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $t) {
  Write-Host "Task '$TaskName' not found (already removed)."
  exit 0
}

Disable-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Removed scheduled task: $TaskName" -ForegroundColor Green
Write-Host "speak_server will no longer start at boot."
