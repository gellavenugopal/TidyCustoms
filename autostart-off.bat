@echo off
REM Stop starting EOU Bond Tracker automatically at login (removes the Startup shortcut).
powershell -NoProfile -Command ^
  "$l = [Environment]::GetFolderPath('Startup') + '\EOU Bond Tracker.lnk';" ^
  "if (Test-Path $l) { Remove-Item $l; Write-Host 'Done: it will no longer start at login.' } else { Write-Host 'It was not set to start at login.' }"
timeout /t 5 >nul
