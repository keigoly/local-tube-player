@echo off
rem MyLocalTube (browser mode). Close this window to stop.
rem Keep this file ASCII-only: cmd.exe parses .cmd files as cp932.
title MyLocalTube - close this window to stop
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv not found. Run the setup in README.md first:
  echo   python -m venv .venv
  echo   .venv\Scripts\python -m pip install -r requirements.txt
  pause
  exit /b 1
)

rem Open the browser once the server responds
start "" powershell -NoProfile -WindowStyle Hidden -Command ^
  "for($i=0;$i -lt 60;$i++){try{Invoke-WebRequest 'http://127.0.0.1:5560' -UseBasicParsing -TimeoutSec 2|Out-Null;Start-Process 'http://127.0.0.1:5560';break}catch{Start-Sleep 1}}"

".venv\Scripts\python.exe" app.py

if errorlevel 1 (
  echo.
  echo [ERROR] exited with code %errorlevel%
  pause
)
