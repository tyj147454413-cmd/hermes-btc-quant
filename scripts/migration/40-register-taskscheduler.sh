#!/bin/bash
# 40-register-taskscheduler.sh — 注册 4 个 Windows Task Scheduler 任务
# 抄 HermesAgentGateway 的注册方式,用 schtasks /Create /XML

set -euo pipefail

TARGET_HOST="192.168.1.22"
TARGET_USER="Administrator"
TARGET_DIR='D:\hermes-btc'
XML_DIR='D:\hermes-btc\scripts\migration\scheduled'

TASKS=(
    "HermesBTC1HBot:HermesBTC1HBot.xml:start_bot.bat"
    "HermesBTC1HDashboard:HermesBTC1HDashboard.xml:start_dashboard.bat"
    "HermesBTC1HCollect:HermesBTC1HCollect.xml:start_collect.bat"
    "HermesBTC1HSignalCron:HermesBTC1HSignalCron.xml:signal_cron.bat"
)

echo "=========================================="
echo "⏰ 注册 Task Scheduler × 4"
echo "=========================================="

for entry in "${TASKS[@]}"; do
    IFS=':' read -r name xml bat <<< "${entry}"
    echo "--- 注册 ${name} ---"
    ssh -o BatchMode=yes ${TARGET_USER}@${TARGET_HOST} "schtasks /Create /XML '${XML_DIR}\\${xml}' /TN '\\${name}' /F" 2>&1 | tail -3
done

echo ""
echo "--- 验证 4 个任务 Ready ---"
ssh -o BatchMode=yes ${TARGET_USER}@${TARGET_HOST} "powershell -Command \"Get-ScheduledTask | Where-Object {\$_.TaskName -match 'HermesBTC1H'} | Select-Object TaskName,State | Format-Table -AutoSize\"" 2>&1 | tail -10

echo ""
echo "=========================================="
echo "✅ Task Scheduler 注册完成"
echo "=========================================="
