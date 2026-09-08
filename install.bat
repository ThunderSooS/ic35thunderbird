@echo off
setlocal
cd /d "%~dp0"
py -3.12 -m venv .venv
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto fail
echo Installation abgeschlossen. Jetzt start.bat starten.
pause
exit /b 0
:fail
echo Installation fehlgeschlagen. Python 3.12 mit Tcl/Tk und Python Launcher erforderlich.
pause
exit /b 1
