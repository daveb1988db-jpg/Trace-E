$ErrorActionPreference = "Continue"
$log = "C:\Users\Bartl\Projects\Trace-E\scripts\_register_startup_out.txt"
$script = "C:\Users\Bartl\Projects\Trace-E\scripts\register_speak_server_startup.ps1"
$sb = New-Object System.Text.StringBuilder
[void]$sb.AppendLine("Started: $(Get-Date -Format o)")
[void]$sb.AppendLine("IsAdmin: $((New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))")
try {
  $out = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script 2>&1
  $code = $LASTEXITCODE
  if ($null -eq $code) { $code = 0 }
  foreach ($line in $out) { [void]$sb.AppendLine([string]$line) }
  [void]$sb.AppendLine("ExitCode: $code")
} catch {
  [void]$sb.AppendLine("EXCEPTION: $($_.Exception.Message)")
  $code = 1
}
[System.IO.File]::WriteAllText($log, $sb.ToString())
exit $code
