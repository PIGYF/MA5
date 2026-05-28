@echo off
setlocal

set "APP_DIR=%~dp0"
set "PYTHON_EXE=C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

cd /d "%APP_DIR%"

if not exist "%PYTHON_EXE%" (
  echo Cannot find Python runtime:
  echo %PYTHON_EXE%
  pause
  exit /b 1
)

"%PYTHON_EXE%" -c "import yfinance" >nul 2>nul
if errorlevel 1 (
  echo Installing missing dependency: yfinance
  "%PYTHON_EXE%" -m pip install yfinance -U
  if errorlevel 1 (
    echo Failed to install yfinance. Check your network/VPN and try again.
    pause
    exit /b 1
  )
)

echo Stopping any old server on port 8765...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$pids = netstat -ano | Select-String ':8765' | ForEach-Object { ($_ -split '\s+')[-1] } | Sort-Object -Unique; foreach ($id in $pids) { if ($id -and $id -ne '0') { Stop-Process -Id ([int]$id) -Force -ErrorAction SilentlyContinue } }" >nul 2>nul

echo Starting local stock backtester...
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8765/scanner'"

"%PYTHON_EXE%" web_app.py
pause
