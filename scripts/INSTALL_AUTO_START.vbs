' Elevate Trace-E auto-start registration (shows UAC on the interactive desktop)
Set sh = CreateObject("Shell.Application")
sh.ShellExecute "powershell.exe", "-NoProfile -ExecutionPolicy Bypass -File ""C:\Users\Bartl\Projects\Trace-E\scripts\register_speak_server_startup.ps1""", "", "runas", 1
