@echo off
REM Start EOU Bond Tracker in the background every time you log in to Windows.
REM (Adds a shortcut to your Startup folder - no admin rights needed. Undo with autostart-off.bat.)
cd /d "%~dp0"
powershell -NoProfile -Command ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup') + '\EOU Bond Tracker.lnk');" ^
  "$s.TargetPath = 'wscript.exe'; $s.Arguments = '\"' + (Resolve-Path 'start-background.vbs') + '\" /nobrowser';" ^
  "$s.WorkingDirectory = (Get-Location).Path; $s.Description = 'Start EOU Bond Tracker in the background'; $s.Save();" ^
  "Write-Host 'Done: EOU Bond Tracker will start in the background when you log in.'"
timeout /t 5 >nul
