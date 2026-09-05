@echo off
REM Opens the curbtool browser UI. Double-click this file.
cd /d "%~dp0"
echo Checking Python...
py -3 --version 2>nul
if errorlevel 1 (
  echo.
  echo Python 3.10+ was not found. Install it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during setup, then run this again.
  pause
  exit /b 1
)
echo Installing dependencies ^(first run only^)...
py -3 -m pip install --user --quiet -r requirements.txt
if errorlevel 1 (
  echo.
  echo Dependency install failed. See the message above.
  pause
  exit /b 1
)
echo.
echo Starting curbtool. Leave this window open; close it to stop the tool.
py -3 ingest.py web
pause
