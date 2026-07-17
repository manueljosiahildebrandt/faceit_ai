@echo off
REM Double-click in Explorer to start Faceit AI (Windows).
REM Keep this window open while the app is running; close it to stop.

setlocal
cd /d "%~dp0\.."
echo Faceit AI — starting from: %CD%

where python >nul 2>&1
if errorlevel 1 (
  echo ERROR: Python is not installed or not on PATH.
  echo Install Python 3.11-3.13 from https://www.python.org/downloads/
  echo During setup, tick "Add python.exe to PATH".
  pause
  exit /b 1
)

if not exist ".venv\" (
  echo Creating virtual environment (.venv^)...
  python -m venv .venv
)

call .venv\Scripts\activate.bat

python -c "import faceit_ai" >nul 2>&1
if errorlevel 1 (
  echo Installing Faceit AI (first run can take several minutes^)...
  python -m pip install --upgrade pip
  python -m pip install -e .
)

if not exist "config\default.yaml" (
  if exist "config\default.example.yaml" (
    copy /Y "config\default.example.yaml" "config\default.yaml" >nul
    echo Created config\default.yaml from example (edit via Settings in the browser^).
  )
)

echo Starting web UI...
faceit_ai_web
if errorlevel 1 pause
endlocal
