@echo off
setlocal

set "APP_DIR=%~dp0"
set "PYTHON_EXE=C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe"

cd /d "%APP_DIR%"

if not exist "%PYTHON_EXE%" (
  echo Cannot find Python runtime:
  echo %PYTHON_EXE%
  pause
  exit /b 1
)

echo Using Python runtime:
echo %PYTHON_EXE%

echo Checking required Python packages...
"%PYTHON_EXE%" -c "import yfinance, efinance, pytdx" >nul 2>nul
if errorlevel 1 (
  echo Missing dependency detected. Run:
  echo %PYTHON_EXE% -m pip install yfinance efinance pytdx -U
  pause
  exit /b 1
)

echo Stopping any old server on port 8765...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$pids = netstat -ano | Select-String ':8765' | ForEach-Object { ($_ -split '\s+')[-1] } | Sort-Object -Unique; foreach ($id in $pids) { if ($id -and $id -ne '0') { Stop-Process -Id ([int]$id) -Force -ErrorAction SilentlyContinue } }" >nul 2>nul

echo Starting local stock backtester...
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8765/scanner'"

"%PYTHON_EXE%" web_app.py
pause
