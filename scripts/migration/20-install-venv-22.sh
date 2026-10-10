#!/bin/bash
# 20-install-venv-22.sh — 在 .22 上建 D:\hermes-btc\.venv 独立 venv + 装依赖
# 独立 venv,不复用 hermes-agent/venv (用户决策:隔离稳)

set -euo pipefail

TARGET_HOST="192.168.1.22"
TARGET_USER="Administrator"
TARGET_DIR='D:/hermes-btc'
VENV_DIR='D:/hermes-btc/.venv'

echo "=========================================="
echo "🐍 独立 venv: ${VENV_DIR}"
echo "=========================================="

# 1. 如果 .venv 已存在,改名备份
echo "--- [1/4] 检查旧 venv ---"
ssh -o BatchMode=yes ${TARGET_USER}@${TARGET_HOST} "powershell -Command \"
if (Test-Path '${VENV_DIR}/Scripts/python.exe') {
    \$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    Move-Item -Force '${VENV_DIR}' '${VENV_DIR}.bak-\$stamp'
    Write-Host '备份旧 venv 到 .venv.bak-\$stamp'
} else {
    Write-Host '无旧 venv,直接建'
}
\"" 2>&1 | tail -5

# 2. 用系统 Python 3.11 建新 venv
echo "--- [2/4] 创建 venv (Python 3.11) ---"
ssh -o BatchMode=yes ${TARGET_USER}@${TARGET_HOST} "cd /d '${TARGET_DIR}' && python -m venv .venv" 2>&1 | tail -5
echo "✅ venv 建好"

# 3. 升级 pip + 装依赖
echo "--- [3/4] 装依赖 ---"
ssh -o BatchMode=yes ${TARGET_USER}@${TARGET_HOST} "cd /d '${TARGET_DIR}' && .venv/Scripts/python.exe -m pip install --upgrade pip wheel setuptools" 2>&1 | tail -5
ssh -o BatchMode=yes ${TARGET_USER}@${TARGET_HOST} "cd /d '${TARGET_DIR}' && .venv/Scripts/python.exe -m pip install -r requirements.txt" 2>&1 | tail -10
echo "✅ requirements.txt 装好"

# 4. 装 fcntl 替代品 + Polymarket 客户端
echo "--- [4/4] 装平台依赖 ---"
ssh -o BatchMode=yes ${TARGET_USER}@${TARGET_HOST} "cd /d '${TARGET_DIR}' && .venv/Scripts/python.exe -m pip install portalocker polymarket-client py_clob_client_v2 python-dotenv" 2>&1 | tail -10

# 5. 验证
echo "--- [5/4] 验证 ---"
ssh -o BatchMode=yes ${TARGET_USER}@${TARGET_HOST} "powershell -Command \"& '${VENV_DIR}/Scripts/python.exe' -c 'import flask, requests, numpy, portalocker; print(\\\"all imports OK\\\")'\"" 2>&1 | tail -5

echo ""
echo "=========================================="
echo "✅ venv 完成,进入 Step 3 (代码改造)"
echo "=========================================="
