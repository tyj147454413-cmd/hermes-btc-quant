@echo off
REM ============================================================
REM Hermes Bot Watchdog - launcher
REM 每 5 分钟由计划任务 HermesBotWatchdog 调用
REM bot 进程死亡则经 schtasks /Run 自动拉起（防风暴保护期 10 分钟）
REM ============================================================
"D:\hermes-btc\.venv\Scripts\python.exe" "D:\hermes-btc\bot_watchdog.py"
