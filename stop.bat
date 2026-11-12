@echo off
REM Stops EOU Bond Tracker when it runs in the background (start-background.vbs or start.bat).
REM Safe to use: every save is a single transaction, so stopping never leaves half-saved data.
REM Only Python processes running "eoubond.cli serve" are stopped (never this script itself).
title Stop EOU Bond Tracker
powershell -NoProfile -Command "$p = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.ProcessId -ne $PID -and $_.CommandLine -like '*eoubond.cli serve*' }); $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; @('EOU Bond Tracker was not running.', 'EOU Bond Tracker has been stopped.')[[int]($p.Count -gt 0)]"
timeout /t 4 >nul
