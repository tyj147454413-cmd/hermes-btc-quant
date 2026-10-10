@echo off
REM ============================================================
REM Hermes BTC 1H Bot - launcher
REM Mirrors Hermes Agent .cmd template (HERMES_HOME / VIRTUAL_ENV / PATH)
REM Deploy: D:\hermes-btc\
REM ============================================================

setlocal

set HERMES_HOME=D:\hermes-btc
set VIRTUAL_ENV=D:\hermes-btc\.venv
set PATH=D:\hermes-btc\.venv\Scripts;%PATH%
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set HERMES_DIR=D:\hermes-btc
set PROXY=

REM Direct connection OK, no proxy needed
set HTTP_PROXY=
set HTTPS_PROXY=
set ALL_PROXY=
set NO_PROXY=

cd /d "%HERMES_HOME%"

REM Check .venv health
if not exist "%VIRTUAL_ENV%\Scripts\python.exe" (
    echo [ERROR] .venv missing or broken
    exit /b 1
)

REM Simple .env existence check
if not exist "%HERMES_HOME%\.env" (
    echo [ERROR] .env missing, sync from Mac
    exit /b 1
)

REM Start in background (like nohup on Mac)
echo [%date% %time%] Starting Hermes 1H Bot ...
start "HermesBTC1HBot" /B "%VIRTUAL_ENV%\Scripts\python.exe" hermes_1h_bot.py 1>"%HERMES_HOME%\data\hermes_bot.log" 2>&1

timeout /t 5 /nobreak >nul

REM Verify process
tasklist /FI "IMAGENAME eq python.exe" /FO CSV 2>nul | findstr /I "python.exe" >nul
if %errorlevel%==0 (
    echo [OK] bot started
    echo [LOG] tail -f %HERMES_HOME%\data\hermes_bot.log
) else (
    echo [ERROR] bot did not start, check log
    exit /b 1
)

endlocal
