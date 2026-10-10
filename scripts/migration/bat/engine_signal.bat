@echo off
REM Hermes BTC 1H engine signal generator
REM Chain: hermes_collect --force -> engine judge -> write_signal -> review
REM Trigger: hourly at :40 by Task Scheduler (HermesBTC1HEngine)

setlocal

set HERMES_HOME=D:\hermes-btc
set VIRTUAL_ENV=D:\hermes-btc\.venv
set PATH=D:\hermes-btc\.venv\Scripts;%PATH%
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set HERMES_DIR=D:\hermes-btc

REM Direct connection works, no proxy needed
set PROXY=
set HTTP_PROXY=
set HTTPS_PROXY=
set ALL_PROXY=
set NO_PROXY=

cd /d "%HERMES_HOME%"

echo [%date% %time%] engine pipe start ... >> "%HERMES_HOME%\data\hermes_engine_signal.log" 2>&1
"%VIRTUAL_ENV%\Scripts\python.exe" scripts\engine_signal.py >> "%HERMES_HOME%\data\hermes_engine_signal.log" 2>&1
set ERR=%errorlevel%
echo [%date% %time%] engine pipe done exit=%ERR% >> "%HERMES_HOME%\data\hermes_engine_signal.log" 2>&1

endlocal & exit /b %ERR%
