@echo off
rem ============================================================
rem  Live Progress Panel - stop the panel service
rem  Usage:  stop_panel.bat [port]        (default port 8791)
rem ============================================================
setlocal
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8791"

set FOUND=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
    set FOUND=1
)
if "%FOUND%"=="1" (echo Panel stopped on port %PORT%.) else (echo Panel is not running on port %PORT%.)
endlocal
exit /b 0
