@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Setting up virtual environment...
    py -3 -m venv .venv
)

echo Checking dependencies...
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt

echo Starting Music Hub...
".venv\Scripts\python.exe" -m app.main

endlocal 