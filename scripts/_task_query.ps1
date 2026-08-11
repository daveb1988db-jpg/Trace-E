$ErrorActionPreference = "Continue"
$out = @()
$out += "IsAdmin: $((New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))"
try {
  $t = Get-ScheduledTask -TaskName TraceE-SpeakServer -ErrorAction Stop
  $out += "TaskName=$($t.TaskName)"
  $out += "State=$($t.State)"
  $out += "UserId=$($t.Principal.UserId)"
  $out += "LogonType=$($t.Principal.LogonType)"
  $out += "RunLevel=$($t.Principal.RunLevel)"
  foreach ($tr in $t.Triggers) {
    $out += "Trigger=$($tr.CimClass.CimClassName); Enabled=$($tr.Enabled); Delay=$($tr.Delay)"
  }
  $out += (schtasks /Query /TN TraceE-SpeakServer /V /FO LIST | Out-String)
} catch {
  $out += "ERROR: $($_.Exception.Message)"
  $out += (schtasks /Query /TN TraceE-SpeakServer /V /FO LIST 2>&1 | Out-String)
}
[System.IO.File]::WriteAllLines("C:\Users\Bartl\Projects\Trace-E\scripts\_task_query_out.txt", $out)
