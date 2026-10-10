@echo off
REM ============================================================
REM Hermes BTC 1H Bot - restart script
REM Corresponds to Mac restart_hermes.sh (no /tmp symlink needed)
REM ============================================================

setlocal

set HERMES_HOME=D:\hermes-btc
set VENV_PY=D:\hermes-btc\.venv\Scripts\python.exe

echo ==========================================
echo Hermes BTC 1H Bot - restart
echo ==========================================
echo.

REM 1. Stop old bot (via Task Scheduler)
echo [1/3] Stopping Task Scheduler bot task ...
schtasks /End /TN "\HermesBTC1HBot" 2>nul
timeout /t 2 /nobreak >nul
echo [OK] stopped
echo.

REM 2. Clean leftover python.exe
echo [2/3] Cleaning leftover python processes ...
taskkill /F /FI "WINDOWTITLE eq HermesBTC1HBot*" 2>nul
taskkill /F /IM python.exe /FI "MEMUSAGE gt 50000" 2>nul
timeout /t 2 /nobreak >nul
echo [OK] cleaned
echo.

REM 3. Restart
echo [3/3] Restarting bot ...
call "%HERMES_HOME%\scripts\migration\bat\start_bot.bat"
if %errorlevel%==0 (
    echo.
    echo ==========================================
    echo [OK] bot restarted
    echo [LOG] %HERMES_HOME%\data\hermes_bot.log
    echo ==========================================
) else (
    echo.
    echo ==========================================
    echo [ERROR] bot restart failed, check log
    echo ==========================================
    exit /b 1
)

endlocal
