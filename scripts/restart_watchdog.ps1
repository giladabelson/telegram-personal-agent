# Restart the Jarvis watchdog so it loads the new watchdog.ps1 code.
# Jarvis itself restarts once (about a minute of no replies).
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
    Where-Object { $_.CommandLine -like '*watchdog.ps1*' -and $_.ProcessId -ne $PID } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-ScheduledTask -TaskName 'Jarvis Watchdog'
Write-Host "Watchdog restarted."
