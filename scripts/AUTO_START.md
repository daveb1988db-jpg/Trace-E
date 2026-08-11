# Trace-E auto-start (before Windows login)

## What runs automatically

Press the PC power button → Windows boots → scheduled task **TraceE-SpeakServer** starts
**speak_server** (port **8787**) as **SYSTEM**, even if nobody logs in.

That brings up the Trace-E brain (HTTP API, cam hub embedded, ESP proxy). **Person follow / motors do not start on boot** — that needs a parent (phone UI or API) so the robot cannot lunge unsupervised.

Day-to-day stack: `speak_server` only (embeds cam + follow modules). No separate ROS required.

## One-time setup (Administrator PowerShell)

```powershell
cd C:\Users\Bartl\Projects\Trace-E
powershell -ExecutionPolicy Bypass -File .\scripts\register_speak_server_startup.ps1
```

## Parent checklist

| Automatic after power press | Still needs a parent |
| --- | --- |
| PC boots, speak_server listening on :8787 | Log in only if you need the desktop |
| Cam hub available via speak_server | Start **follow** from phone UI when someone is supervising |
| Health at `http://<pc-ip>:8787/api/health` | Stop follow / motors if needed |

## Verify

After reboot (login optional):

```powershell
Invoke-WebRequest http://127.0.0.1:8787/api/health -UseBasicParsing
Get-Content C:\Users\Bartl\Projects\Trace-E\logs\speak_server.log -Tail 40
Get-ScheduledTask -TaskName TraceE-SpeakServer | Format-List TaskName,State
```

From a phone on the same Wi‑Fi, open `http://<pc-ip>:8787/api/health`.

## Disable / remove

```powershell
# Stop auto-start but leave the task definition:
Disable-ScheduledTask -TaskName TraceE-SpeakServer

# Remove task entirely (elevated):
powershell -ExecutionPolicy Bypass -File C:\Users\Bartl\Projects\Trace-E\scripts\unregister_speak_server_startup.ps1

# Also stop a running server:
powershell -ExecutionPolicy Bypass -File C:\Users\Bartl\Projects\Trace-E\scripts\unregister_speak_server_startup.ps1 -StopRunning
```

## Manual start (without waiting for reboot)

```powershell
powershell -ExecutionPolicy Bypass -File C:\Users\Bartl\Projects\Trace-E\scripts\start_speak_server.ps1
```

Env defaults: `TRACE_E_ESP_BASE=http://192.168.1.104`, `TRACE_E_CHIRPS=off`. Project `.env` is still loaded by speak_server if present.
