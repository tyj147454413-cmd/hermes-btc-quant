@echo off
REM ============================================================
REM Hermes BTC 1H data collect - single run (hourly :02 by Task Scheduler)
REM Calls hermes_collect.py
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
set HTTP_PROXY=
set HTTPS_PROXY=

REM 9/4: removed the for /f .env loader line below (it broke under Task Scheduler,
REM   quoting got truncated and every run printed a "not recognized" error).
REM   HERMES_DIR is already hardcoded above and matches .env; scripts default to
REM   their own directory anyway, so the line was redundant.
REM NOTE: keep this file pure ASCII - cmd.exe reads .bat files in the ANSI codepage,
REM   UTF-8 Chinese comments get garbled and break parsing.

cd /d "%HERMES_HOME%"

REM Run in foreground so Task Scheduler sees exit code
"%VIRTUAL_ENV%\Scripts\python.exe" hermes_collect.py

exit /b %errorlevel%

endlocal
