@echo off
REM ---------------------------------------------------------------------------
REM  EOU Bond Tracker - double-click to start.
REM
REM  The program folder can live in OneDrive. The live database and the Python
REM  environment are kept OUTSIDE OneDrive, in
REM      %LOCALAPPDATA%\EOU Bond Tracker
REM  (syncing a database while it is written can corrupt it). Backups go to
REM  BACKUP_DIR (default: the "backups" folder here).
REM
REM  First run on a PC: creates the Python environment, installs packages,
REM  creates the database. Keep this window open while you use the app.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"
title EOU Bond Tracker
set "DATA=%LOCALAPPDATA%\EOU Bond Tracker"
set "PY=%DATA%\venv\Scripts\python.exe"

if not exist "%PY%" (
    echo Setting up for first use on this PC - this takes a few minutes...
    if not exist "%DATA%" mkdir "%DATA%"
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" -m venv "%DATA%\venv"
    ) else (
        py -3.12 -m venv "%DATA%\venv"
    )
    if not exist "%PY%" (
        echo.
        echo Python 3.12 and the Python launcher are needed. Install Python 3.12 from
        echo https://www.python.org/downloads/ and enable the Python launcher,
        echo then run start.bat again.
        pause
        exit /b 1
    )
    "%PY%" -m pip install --upgrade pip >nul
)

"%PY%" -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo Installing packages failed - check the internet connection and try again.
    pause
    exit /b 1
)

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo Created .env - open it in Notepad to change settings.
)

"%PY%" -m eoubond.cli upgrade
if errorlevel 1 (
    echo Database upgrade failed - see the message above.
    pause
    exit /b 1
)

start "" http://localhost:5000
"%PY%" -m eoubond.cli serve
pause
