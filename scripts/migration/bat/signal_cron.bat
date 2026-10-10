@echo off
REM ============================================================
REM Hermes BTC 1H 信号生成(可选)
REM 触发 Claude Code CLI 调 hermes-btc-analysis skill 写信号到 JSONL
REM 替代 Mac 上的 Claude Desktop 计划任务
REM
REM 已知限制:
REM   - .22 上 Claude Code CLI v2.1.74 不一定支持 --skill 参数(待验证)
REM   - 真正部署时可能要用 /loop 或 claude -p "..."
REM   - 如果不能自动触发,改为人工跑
REM ============================================================

setlocal

set HERMES_HOME=D:\hermes-btc
set SKILL_PATH=C:\Users\Administrator\Claude\Scheduled\btc-1h-hourly-analysis-v2\SKILL.md
set SIGNAL_OUTPUT=D:\hermes-btc\data\claude_signal.jsonl

if not exist "%SKILL_PATH%" (
    echo [ERROR] SKILL.md 不存在: %SKILL_PATH%
    echo [FIX]   从 Mac 同步 SKILL.md 目录:
    echo          rsync -avz Mac:~/Claude/Scheduled/btc-1h-hourly-analysis-v2/ "C:\Users\Administrator\Claude\Scheduled\btc-1h-hourly-analysis-v2\"
    exit /b 1
)

cd /d "%HERMES_HOME%"

echo [%date% %time%] 触发 Claude Code CLI 信号生成 ...

REM 调用 Claude Code CLI(占位语法,实际参数待 .22 上验证)
REM 目标:让 Claude 读 SKILL.md,执行 11 步,写信号到 %SIGNAL_OUTPUT%
claude --model claude-haiku-4-5 --skill "%SKILL_PATH%" --output "%SIGNAL_OUTPUT%"

if %errorlevel%==0 (
    echo [OK] 信号生成完成: %SIGNAL_OUTPUT%
) else (
    echo [WARN] claude CLI 调用失败(可能 --skill 参数不支持),改人工触发
    exit /b 1
)

endlocal
