@echo off
REM Double-click to write a full backup into BACKUP_DIR right now.
cd /d "%~dp0"
"%LOCALAPPDATA%\EOU Bond Tracker\venv\Scripts\python.exe" -m eoubond.cli backup
pause
