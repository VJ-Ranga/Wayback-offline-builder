@echo off
setlocal

if not exist ".venv\Scripts\python.exe" (
  echo Virtual environment not found. Run install.ps1 first.
  exit /b 1
)

".venv\Scripts\python.exe" run_and_healthcheck.py --open-browser
if %errorlevel%==0 (
  echo.
  echo App is running in background.
  echo Stop anytime with: .\stop.ps1
)
