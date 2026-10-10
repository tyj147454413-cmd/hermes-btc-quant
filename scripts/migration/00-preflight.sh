#!/bin/bash
# 00-preflight.sh — Preflight check (read-only, no side effects on .22)
# 验证：SSH 通、key 免密、Python/Node 路径、磁盘、网络

set -euo pipefail

TARGET_HOST="192.168.1.22"
TARGET_USER="Administrator"
TARGET_PORT=22

echo "=========================================="
echo "🔍 Hermes BTC 1H 迁移 Preflight"
echo "=========================================="
echo ""

# 1. SSH 连通 + key 免密
echo "--- [1/5] SSH 连通性 ---"
if ! ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no "${TARGET_USER}@${TARGET_HOST}" "echo OK" >/dev/null 2>&1; then
    echo "❌ SSH 不通或 key 免密失败"
    echo "   测试: ssh ${TARGET_USER}@${TARGET_HOST}"
    exit 1
fi
echo "✅ SSH 通 + key 免密"

# 2. Python 版本
echo "--- [2/5] Python ---"
PY_VER=$(ssh -o BatchMode=yes ${TARGET_USER}@${TARGET_HOST} "python --version" 2>&1 | tr -d '\r')
echo "   ${PY_VER}"
if ! echo "${PY_VER}" | grep -qE "Python 3\.(11|12|13)"; then
    echo "❌ Python 版本不在 3.11-3.13,装新 Python 或用兼容 venv"
    exit 1
fi
echo "✅ Python 兼容"

# 3. Node 版本
echo "--- [3/5] Node ---"
NODE_VER=$(ssh -o BatchMode=yes ${TARGET_USER}@${TARGET_HOST} "node --version" 2>&1 | tr -d '\r')
echo "   ${NODE_VER}"
if ! echo "${NODE_VER}" | grep -qE "v(20|22|24)"; then
    echo "❌ Node 版本不在 v20-v24"
    exit 1
fi
echo "✅ Node 兼容"

# 4. 磁盘空间 (D 盘)
echo "--- [4/5] 磁盘空间 ---"
DISK_FREE=$(ssh -o BatchMode=yes ${TARGET_USER}@${TARGET_HOST} "powershell -Command \"(Get-CimInstance Win32_LogicalDisk -Filter 'DeviceID=''D:''').FreeSpace/1GB\"" 2>&1 | tr -d '\r' | tail -1)
DISK_FREE_INT=${DISK_FREE%.*}
echo "   D 盘可用: ${DISK_FREE_INT} GB"
if [[ ${DISK_FREE_INT} -lt 10 ]]; then
    echo "❌ D 盘空间不足 10GB,需要清理"
    exit 1
fi
echo "✅ 磁盘充足"

# 5. 网络 (Polymarket 直连)
echo "--- [5/5] Polymarket 网络 ---"
PM_HTTP=$(ssh -o BatchMode=yes ${TARGET_USER}@${TARGET_HOST} "powershell -Command \"try{(Invoke-WebRequest -Uri 'https://clob.polymarket.com' -UseBasicParsing -TimeoutSec 8).StatusCode}catch{'ERR'}\"" 2>&1 | tr -d '\r' | tail -1)
echo "   clob.polymarket.com → ${PM_HTTP}"
if [[ "${PM_HTTP}" != "200" ]]; then
    echo "❌ Polymarket 不可达,bot 无法下单"
    exit 1
fi
echo "✅ Polymarket 直连"

echo ""
echo "=========================================="
echo "✅ Preflight 全部通过,可以进入 Step 1 (rsync)"
echo "=========================================="
