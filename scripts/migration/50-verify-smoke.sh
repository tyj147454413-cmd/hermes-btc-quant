#!/bin/bash
# 50-verify-smoke.sh — 迁移完成后的冒烟测试
# 验证：bot --paper 跑一次、dashboard 起在 5050、collect 跑一次有输出、4 任务 Ready

set -euo pipefail

TARGET_HOST="192.168.1.22"
TARGET_USER="Administrator"
TARGET_DIR='D:/hermes-btc'
VENV_PY='D:/hermes-btc/.venv/Scripts/python.exe'

echo "=========================================="
echo "🧪 Smoke Test"
echo "=========================================="

# 1. bot --paper --once
echo "--- [1/4] bot --paper --once ---"
ssh -o BatchMode=yes ${TARGET_USER}@${TARGET_HOST} "cd /d '${TARGET_DIR}' && .venv/Scripts/python.exe hermes_1h_bot.py --paper --once 2>&1 | Select-Object -First 30" 2>&1 | tail -10

# 2. dashboard 起在 5050
echo "--- [2/4] dashboard :5050 ---"
ssh -o BatchMode=yes ${TARGET_USER}@${TARGET_HOST} "powershell -Command \"
Start-Process -FilePath '${VENV_PY}' -ArgumentList 'dashboard.py' -WorkingDirectory '${TARGET_DIR}' -RedirectStandardOutput '${TARGET_DIR}/data/dashboard_smoke.log' -RedirectStandardError '${TARGET_DIR}/data/dashboard_smoke.err'
Start-Sleep -Seconds 5
try {
    \$r = Invoke-WebRequest -Uri 'http://127.0.0.1:5050' -UseBasicParsing -TimeoutSec 5
    Write-Host \\\"dashboard HTTP \$(\$r.StatusCode) OK\\\"
} catch {
    Write-Host \\\"❌ dashboard 不可达: \$(\$_.Exception.Message)\\\"
}
\"" 2>&1 | tail -5

# 3. collect 跑一次
echo "--- [3/4] collect 跑一次 ---"
ssh -o BatchMode=yes ${TARGET_USER}@${TARGET_HOST} "cd /d '${TARGET_DIR}' && .venv/Scripts/python.exe hermes_collect.py 2>&1 | Select-Object -First 20" 2>&1 | tail -15

# 4. Task Scheduler 4 任务 Ready
echo "--- [4/4] Task Scheduler 状态 ---"
ssh -o BatchMode=yes ${TARGET_USER}@${TARGET_HOST} "powershell -Command \"Get-ScheduledTask | Where-Object {\$_.TaskName -match 'HermesBTC1H'} | Select-Object TaskName,State | Format-Table -AutoSize\"" 2>&1 | tail -10

echo ""
echo "=========================================="
echo "✅ Smoke test 完成"
echo "=========================================="
