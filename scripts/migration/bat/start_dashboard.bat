@echo off
REM ============================================================
REM Hermes BTC 1H Dashboard launcher
REM Corresponds to dashboard.py (launched by launchd on Mac)
REM ============================================================

setlocal

set HERMES_HOME=D:\hermes-btc
set VIRTUAL_ENV=D:\hermes-btc\.venv
set PATH=D:\hermes-btc\.venv\Scripts;%PATH%
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set HERMES_DIR=D:\hermes-btc
set HERMES_DASHBOARD_HOST=127.0.0.1
set HERMES_DASHBOARD_PORT=5050

cd /d "%HERMES_HOME%"

REM Start dashboard in background
echo [%date% %time%] Starting Hermes BTC Dashboard :%HERMES_DASHBOARD_PORT% ...
start "HermesBTC1HDashboard" /B "%VIRTUAL_ENV%\Scripts\pythonw.exe" dashboard.py --host %HERMES_DASHBOARD_HOST% --port %HERMES_DASHBOARD_PORT% 1>"%HERMES_HOME%\data\hermes_dashboard.log" 2>&1

REM 9/3: venv shim spawns child python + Flask imports take 5-6s; fixed 5s wait
REM raced the bind and false-reported "did not start". Retry up to ~30s instead.
REM (ping sleep: `timeout /t` errors out instantly when stdin is redirected)
set /a TRIES=0
:waitloop
ping -n 3 127.0.0.1 >nul
netstat -an | findstr ":%HERMES_DASHBOARD_PORT%" | findstr "LISTENING" >nul
if %errorlevel%==0 goto up
set /a TRIES+=1
if %TRIES% lss 15 goto waitloop
echo [ERROR] dashboard did not start, check log: %HERMES_HOME%\data\hermes_dashboard.log
exit /b 1
:up
echo [OK] dashboard up: http://localhost:%HERMES_DASHBOARD_PORT%

endlocal
