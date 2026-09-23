@echo off
rem Rebuild MyLocalTube.exe (launcher) and static\app.ico.
rem Only needed when MyLocalTube.cs or make_icon.py changes.
rem Keep this file ASCII-only: cmd.exe parses .cmd files as cp932 and
rem UTF-8 Japanese text can swallow line breaks.
setlocal
cd /d "%~dp0"
set "CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not exist "%CSC%" (
  echo [ERROR] C# compiler not found: %CSC%
  goto :fail
)

"..\.venv\Scripts\python.exe" make_icon.py || goto :fail

"%CSC%" /nologo /codepage:65001 /target:winexe /optimize+ ^
  /win32icon:..\static\app.ico /r:System.Windows.Forms.dll ^
  /out:..\MyLocalTube.exe MyLocalTube.cs || goto :fail

echo OK: %~dp0..\MyLocalTube.exe
exit /b 0

:fail
echo [ERROR] build failed
exit /b 1
