@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo Bitte zuerst install.bat ausfuehren.
    pause
    exit /b 1
)
.venv\Scripts\python.exe IC35_Thunderbird_Sync.py
if errorlevel 1 pause
