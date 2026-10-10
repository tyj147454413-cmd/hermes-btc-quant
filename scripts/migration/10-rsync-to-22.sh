#!/bin/bash
# 10-rsync-to-22.sh — tar + scp 替代 rsync(因 .22 没装 rsync)
# Mac 打 tar.gz → scp 推过去 → .22 上 tar -xzf 解压
# Windows 10 1803+ 自带 tar.exe(.22 是 19045 满足)

set -euo pipefail

TARGET_HOST="192.168.1.22"
TARGET_USER="Administrator"
TARGET_DIR='D:/hermes-btc'
SOURCE_DIR="/Users/tangyijun/Desktop/hermes btc"
TARBALL="/tmp/hermes-btc-migration-$(date +%Y%m%d_%H%M%S).tar.gz"

echo "=========================================="
echo "📦 tar + scp: ${SOURCE_DIR}"
echo "           → ${TARGET_USER}@${TARGET_HOST}:${TARGET_DIR}"
echo "=========================================="

# 1. 在 .22 上建目录
echo "--- [1/4] 建目标目录 ---"
ssh -o BatchMode=yes ${TARGET_USER}@${TARGET_HOST} "powershell -Command \"if (-not (Test-Path '${TARGET_DIR}')) { New-Item -ItemType Directory -Force -Path '${TARGET_DIR}' | Out-Null }; Write-Host 'OK: ${TARGET_DIR}'\"" 2>&1 | tail -2

# 2. Mac 端打 tar(排除 venv / cache / git objects / node_modules / launchd / 数据目录)
echo "--- [2/4] 打 tar.gz ---"
cd "${SOURCE_DIR}"
tar -czf "${TARBALL}" \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.git/objects' \
    --exclude='.git/index' \
    --exclude='.git/logs' \
    --exclude='.git/refs' \
    --exclude='node_modules' \
    --exclude='launchd' \
    --exclude='*.command' \
    --exclude='.DS_Store' \
    --exclude='archive' \
    --exclude='hermes-dashboard-current.png' \
    --exclude='logs' \
    --exclude='paper_trades' \
    --exclude='predictions' \
    --exclude='result' \
    --exclude='data/snapshots' \
    --exclude='data/claude_signal.jsonl' \
    --exclude='data/hermes_*.log' \
    --exclude='data/hermes_*.jsonl' \
    --exclude='data/claude_signal.jsonl' \
    --exclude='BTC_1H_Trade_Journal/_*' \
    --exclude='.codebuddy' \
    --exclude='.playwright-mcp' \
    --exclude='.trae' \
    --exclude='.gstack' \
    --exclude='.mcp.json' \
    --exclude='CLAUDE.md.bak_*' \
    --exclude='hermes_*.py.bak_*' \
    --exclude='hermes_config.json.bak_*' \
    --exclude='prompt_exec.md.bak_*' \
    .
TARBALL_SIZE=$(du -h "${TARBALL}" | cut -f1)
echo "✅ tar 完成: ${TARBALL} (${TARBALL_SIZE})"

# 3. scp 推到 .22
echo "--- [3/4] scp 推送 ---"
TARBALL_NAME=$(basename "${TARBALL}")
scp -o BatchMode=yes "${TARBALL}" "${TARGET_USER}@${TARGET_HOST}:/tmp/${TARBALL_NAME}" 2>&1 | tail -3
echo "✅ scp 完成"

# 4. .22 上解压
echo "--- [4/4] .22 解压 ---"
ssh -o BatchMode=yes ${TARGET_USER}@${TARGET_HOST} "powershell -Command \"
\$tmp = 'C:\\Users\\Administrator\\AppData\\Local\\Temp\\${TARBALL_NAME}'
if (Test-Path \$tmp) {
    & tar -xzf \$tmp -C '${TARGET_DIR}' 2>&1 | Out-Null
    Remove-Item \$tmp -Force
    Write-Host 'OK decompressed'
} else {
    Write-Host 'FAIL: tarball missing at' \$tmp
    exit 1
}
\"" 2>&1 | tail -5

# 5. 远端验证
echo "--- 远端验证 ---"
ssh -o BatchMode=yes ${TARGET_USER}@${TARGET_HOST} "powershell -Command \"Get-ChildItem '${TARGET_DIR}' -File -ErrorAction SilentlyContinue | Select-Object -First 15 Name,@{n='KB';e={[math]::Round(\$_.Length/1KB,1)}} | Format-Table -AutoSize\"" 2>&1 | tail -20

# 6. 清 Mac tarball
rm -f "${TARBALL}"
echo ""
echo "=========================================="
echo "✅ tar+scp 完成"
echo "=========================================="
