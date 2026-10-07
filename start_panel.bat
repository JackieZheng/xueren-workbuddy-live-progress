@echo off
rem ============================================================
rem  Live Progress Panel - one click start
rem  Usage:  start_panel.bat [port]        (default port 8791)
rem  Env:    NO_BROWSER=1  -> do not open the browser
rem          IDLE_EXIT=N   -> auto quit after N idle seconds (default: never)
rem  Safe to double click. Idempotent: if the panel is already
rem  running on that port, this instance just exits.
rem  NOTE: this instance is launched by explorer.exe, i.e. OUTSIDE
rem  the agent session - it will never keep a session unfinished.
rem ============================================================
setlocal
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8791"

set "PYW=%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
if not exist "%PYW%" set "PYW=%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PYW%" set "PYW=pythonw.exe"

set "EXTRA="
if not "%IDLE_EXIT%"=="" set "EXTRA=--idle-exit %IDLE_EXIT%"

start "" "%PYW%" "%~dp0scripts\live_panel.py" --port %PORT% %EXTRA%
if "%NO_BROWSER%"=="1" goto :eof
timeout /t 1 /nobreak >nul
start "" "http://127.0.0.1:%PORT%/"
endlocal
exit /b 0
