#!/bin/bash
# 30-patch-fcntl-22.sh — 在 .22 上原地打 patch,把 fcntl 改成 portalocker
# fcntl 是 Unix-only,Windows 直接抛 ModuleNotFoundError

set -euo pipefail

TARGET_HOST="192.168.1.22"
TARGET_USER="Administrator"
TARGET_DIR='D:/hermes-btc'
BOT_PY="${TARGET_DIR}/hermes_1h_bot.py"

echo "=========================================="
echo "🔧 fcntl → portalocker 改造"
echo "=========================================="

# 1. 检查 fcntl 引用位置
echo "--- [1/4] 扫描 fcntl 引用 ---"
ssh -o BatchMode=yes ${TARGET_USER}@${TARGET_HOST} "powershell -Command \"Select-String -Path '${BOT_PY}' -Pattern 'fcntl' | Select-Object LineNumber,Line | Format-Table -AutoSize\"" 2>&1 | tail -10

# 2. 注入 portalocker import + 兼容层
echo "--- [2/4] 注入 portalocker import ---"
ssh -o BatchMode=yes ${TARGET_USER}@${TARGET_HOST} "powershell -Command \"
\$path = '${BOT_PY}'
\$content = Get-Content \$path -Raw -Encoding UTF8

# 在 'import os, json, time, signal, fcntl, re, calendar' 这行替换 fcntl
\$old = 'import os, json, time, signal, fcntl, re, calendar'
\$new = @'
try:
    import fcntl as _fcntl_mod
    _HAS_FCNTL = True
except ImportError:
    _fcntl_mod = None
    _HAS_FCNTL = False
import portalocker

import os, json, time, signal, re, calendar
'@

if (\$content -like \"*\$old*\") {
    \$content = \$content -replace [regex]::Escape(\$old), \$new
    Set-Content -Path \$path -Value \$content -Encoding UTF8 -NoNewline
    Write-Host '✅ import 段已改'
} else {
    Write-Host '⚠️  找不到原 import 段,需手动检查'
}
\"" 2>&1 | tail -5

# 3. fcntl.flock 调用替换
echo "--- [3/4] 替换 fcntl.flock → portalocker.lock ---"
ssh -o BatchMode=yes ${TARGET_USER}@${TARGET_HOST} "powershell -Command \"
\$path = '${BOT_PY}'
\$content = Get-Content \$path -Raw -Encoding UTF8

# 替换 fcntl.flock(f, fcntl.LOCK_EX) → portalocker.lock(f, portalocker.LOCK_EX)
\$content = \$content -replace 'fcntl\.flock\((\w+),\s*fcntl\.LOCK_EX', 'portalocker.lock(\$1, portalocker.LOCK_EX'
\$content = \$content -replace 'fcntl\.flock\((\w+),\s*fcntl\.LOCK_EX\s*\|\s*fcntl\.LOCK_NB', 'portalocker.lock(\$1, portalocker.LOCK_EX | portalocker.LOCK_NB'
\$content = \$content -replace 'fcntl\.flock\((\w+),\s*fcntl\.LOCK_UN', 'portalocker.unlock(\$1)'
\$content = \$content -replace 'fcntl\.LOCK_EX', 'portalocker.LOCK_EX'
\$content = \$content -replace 'fcntl\.LOCK_NB', 'portalocker.LOCK_NB'
\$content = \$content -replace 'fcntl\.LOCK_UN', 'portalocker.LOCK_UN'

Set-Content -Path \$path -Value \$content -Encoding UTF8 -NoNewline
Write-Host '✅ fcntl 调用已替换'
\"" 2>&1 | tail -5

# 4. 语法验证
echo "--- [4/4] 语法验证 ---"
ssh -o BatchMode=yes ${TARGET_USER}@${TARGET_HOST} "powershell -Command \"& '${VENV_DIR}/Scripts/python.exe' -m py_compile '${BOT_PY}'\"" 2>&1 | tail -5

echo ""
echo "=========================================="
echo "✅ fcntl 改造完成"
echo "=========================================="
