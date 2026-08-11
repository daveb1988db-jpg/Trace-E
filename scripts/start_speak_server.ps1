<#
.SYNOPSIS
  Start Trace-E speak_server (brain) for pre-logon / always-on use.

.DESCRIPTION
  Idempotent: if http://127.0.0.1:8787/api/health is OK, exits 0 without starting.
  If port 8787 is occupied but unhealthy, stops the owning process, then starts fresh.
  Waits briefly for network before launch. Does NOT auto-start person follow (motors).

  Scheduled-task mode uses -ServiceMode (foreground) so Task Scheduler can restart on crash.
#>
[CmdletBinding()]
param(
  [string]$RepoRoot = "C:\Users\Bartl\Projects\Trace-E",
  [string]$EspBase = "http://192.168.1.104",
  [string]$Chirps = "off",
  [int]$Port = 8787,
  [int]$NetworkWaitSeconds = 120,
  [switch]$ServiceMode,
  [switch]$Foreground
)

$ErrorActionPreference = "Continue"
$Python = Join-Path $RepoRoot ".venv-nav\Scripts\python.exe"
$Server = Join-Path $RepoRoot "desktop\speak_server.py"
$LogDir = Join-Path $RepoRoot "logs"
$LogFile = Join-Path $LogDir "speak_server.log"
$OutLog = Join-Path $LogDir "speak_server.out.log"
$ErrLog = Join-Path $LogDir "speak_server.err.log"
$PidFile = Join-Path $LogDir "speak_server.pid"
$HealthUrl = "http://127.0.0.1:$Port/api/health"
$RunForeground = $ServiceMode -or $Foreground

function Write-Log([string]$Message) {
  $line = "{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $Message
  try {
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
  } catch { }
  Write-Host $line
}

function Test-SpeakHealthy {
  try {
    $resp = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 3
    return ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 300)
  } catch {
    return $false
  }
}

function Get-PortOwnerPids([int]$ListenPort) {
  $pids = @()
  try {
    $conns = Get-NetTCPConnection -LocalPort $ListenPort -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conns) {
      if ($c.OwningProcess -and ($pids -notcontains $c.OwningProcess)) {
        $pids += [int]$c.OwningProcess
      }
    }
  } catch { }
  if ($pids.Count -eq 0) {
    try {
      $lines = netstat -ano | Select-String ":$ListenPort\s+.*LISTENING"
      foreach ($ln in $lines) {
        $parts = ($ln.ToString() -split '\s+') | Where-Object { $_ -ne '' }
        $p = [int]$parts[-1]
        if ($p -gt 0 -and ($pids -notcontains $p)) { $pids += $p }
      }
    } catch { }
  }
  return $pids
}

function Stop-StaleSpeak {
  $pids = Get-PortOwnerPids -ListenPort $Port
  foreach ($p in $pids) {
    Write-Log "Stopping stale process on :$Port (PID $p)"
    try { Stop-Process -Id $p -Force -ErrorAction Stop } catch {
      Write-Log "WARN: could not stop PID $p : $($_.Exception.Message)"
    }
  }
  if (Test-Path $PidFile) {
    try {
      $old = [int](Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
      if ($old -gt 0) {
        $proc = Get-Process -Id $old -ErrorAction SilentlyContinue
        if ($proc) {
          Write-Log "Stopping PID from pidfile ($old)"
          Stop-Process -Id $old -Force -ErrorAction SilentlyContinue
        }
      }
    } catch { }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
  }
  Start-Sleep -Seconds 2
}

function Wait-ForNetwork {
  $deadline = (Get-Date).AddSeconds($NetworkWaitSeconds)
  Write-Log "Waiting up to ${NetworkWaitSeconds}s for network..."
  while ((Get-Date) -lt $deadline) {
    try {
      $up = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object {
          $_.IPAddress -notlike '127.*' -and
          $_.AddressState -eq 'Preferred'
        }
      if ($up) {
        Write-Log ("Network ready: " + (($up | Select-Object -ExpandProperty IPAddress | Select-Object -First 5) -join ', '))
        return $true
      }
    } catch { }
    try {
      $ip = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike '127.*' } |
        Select-Object -First 1).IPAddress
      if ($ip) {
        Write-Log "Network address present: $ip"
        return $true
      }
    } catch { }
    Start-Sleep -Seconds 2
  }
  Write-Log "WARN: network wait timed out; starting speak_server anyway"
  return $false
}

if (-not (Test-Path $Python)) {
  Write-Log "ERROR: Python not found: $Python"
  exit 1
}
if (-not (Test-Path $Server)) {
  Write-Log "ERROR: speak_server not found: $Server"
  exit 1
}

if (Test-SpeakHealthy) {
  Write-Log "speak_server already healthy on :$Port — nothing to do"
  exit 0
}

$owners = Get-PortOwnerPids -ListenPort $Port
if ($owners.Count -gt 0) {
  Write-Log "Port $Port in use but health check failed; replacing stale listener(s)"
  Stop-StaleSpeak
}

Wait-ForNetwork | Out-Null

$env:TRACE_E_ESP_BASE = $EspBase
$env:TRACE_E_CHIRPS = $Chirps
$env:TRACE_E_SPEAK_HOST = "0.0.0.0"
$env:TRACE_E_SPEAK_PORT = "$Port"
if (-not $env:TRACE_E_ALLOW_LAPTOP) { $env:TRACE_E_ALLOW_LAPTOP = "0" }

Write-Log "Starting speak_server (ESP=$EspBase chirps=$Chirps port=$Port mode=$(if ($RunForeground) { 'service/foreground' } else { 'background' }))"
Set-Location $RepoRoot

if ($RunForeground) {
  Write-Log "speak_server service launching (task restarts if process exits)"
  $p = Start-Process -FilePath $Python -ArgumentList "`"$Server`"" `
    -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog
  Set-Content -Path $PidFile -Value $p.Id -Encoding ASCII
  Write-Log "speak_server service PID=$($p.Id)"
  Wait-Process -Id $p.Id
  $code = $p.ExitCode
  Write-Log "speak_server exited code=$code"
  if ($null -eq $code) { exit 1 }
  exit ([int]$code)
}

$p = Start-Process -FilePath $Python -ArgumentList "`"$Server`"" `
  -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog
Set-Content -Path $PidFile -Value $p.Id -Encoding ASCII
Write-Log "speak_server launched PID=$($p.Id)"

$ok = $false
for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep -Seconds 1
  if (Test-SpeakHealthy) { $ok = $true; break }
  if ($p.HasExited) {
    Write-Log "ERROR: speak_server exited early code=$($p.ExitCode)"
    exit 1
  }
}
if ($ok) {
  Write-Log "Health OK: $HealthUrl"
  exit 0
}
Write-Log "WARN: started but health not ready yet within 30s (check $LogFile / $OutLog / $ErrLog)"
exit 0
