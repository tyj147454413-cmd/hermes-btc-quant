#!/bin/bash
# 31-fix-hardcoded-paths.sh — 把 hermes_collect.py 里的硬编码 Mac 路径参数化
# 让代码用环境变量,而不是写死 /Users/tangyijun/...

set -euo pipefail

TARGET_HOST="192.168.1.22"
TARGET_USER="Administrator"
TARGET_DIR='D:/hermes-btc'
COLLECT_PY="${TARGET_DIR}/hermes_collect.py"
ENV_FILE="${TARGET_DIR}/.env"

echo "=========================================="
echo "🛠️  硬编码路径参数化"
echo "=========================================="

# 1. 探测当前硬编码行
echo "--- [1/4] 扫描硬编码 ---"
ssh -o BatchMode=yes ${TARGET_USER}@${TARGET_HOST} "powershell -Command \"Select-String -Path '${COLLECT_PY}' -Pattern '/Users/tangyijun' | Select-Object LineNumber,Line | Format-Table -AutoSize -Wrap\"" 2>&1 | tail -15

# 2. 替换为环境变量读
echo "--- [2/4] 替换为环境变量 ---"
ssh -o BatchMode=yes ${TARGET_USER}@${TARGET_HOST} "powershell -Command \"
\$path = '${COLLECT_PY}'
\$content = Get-Content \$path -Raw -Encoding UTF8

# 在文件顶部 import 段后面插入 HERMES_DIR / TV_* 读取
\$old_imports = 'from hermes_common import'
\$new_block = @'
import os
from pathlib import Path
HERMES_DIR = Path(os.environ.get('HERMES_DIR', str(Path(__file__).resolve().parent)))
_TV_CLI = os.environ.get('TV_CLI', '')
_TV_MCP_DIR = os.environ.get('TV_MCP_DIR', '')
_TV_NODE = os.environ.get('TV_NODE', 'node')  # 默认走系统 PATH

# 若 TV_CLI 为空,自动探测 ../../tradingview-mcp/src/cli/index.js
if not _TV_CLI:
    candidate = HERMES_DIR.parent / 'tradingview-mcp' / 'src' / 'cli' / 'index.js'
    if candidate.exists():
        _TV_CLI = str(candidate)

'@

# 找 import os 行附近插入
if (\$content -match 'import os[^\n]*\n') {
    \$content = \$content -replace '(import os[^\n]*\n)', (\$new_block + '\`$1\`')
}

# 替换硬编码行
\$content = \$content -replace '_TV_CLI = \"[^\"]*\"', '_TV_CLI = os.environ.get(\"TV_CLI\", _TV_CLI)'
\$content = \$content -replace '_TV_MCP_DIR = \"[^\"]*\"', '_TV_MCP_DIR = os.environ.get(\"TV_MCP_DIR\", _TV_MCP_DIR)'
\$content = \$content -replace '_TV_NODE = \"[^\"]*\"', '_TV_NODE = os.environ.get(\"TV_NODE\", _TV_NODE)'

Set-Content -Path \$path -Value \$content -Encoding UTF8 -NoNewline
Write-Host '✅ hermes_collect.py 已改'
\"" 2>&1 | tail -5

# 3. 在 .env 里追加 .22 路径配置
echo "--- [3/4] 追加 .env 段 ---"
ssh -o BatchMode=yes ${TARGET_USER}@${TARGET_HOST} "powershell -Command \"
\$path = '${ENV_FILE}'
if (-not (Test-Path \$path)) { New-Item -ItemType File -Force -Path \$path | Out-Null }
\$add = @'

# ===== 192.168.1.22 (Windows) 路径覆盖 =====
HERMES_DIR=D:\\hermes-btc
TV_CLI=
TV_MCP_DIR=C:\\Users\\Administrator\\tradingview-mcp
TV_NODE=node
PROXY=
'@
Add-Content -Path \$path -Value \$add -Encoding UTF8
Write-Host '✅ .env 已追加'
\"" 2>&1 | tail -5

# 4. 语法验证
echo "--- [4/4] 语法验证 ---"
ssh -o BatchMode=yes ${TARGET_USER}@${TARGET_HOST} "powershell -Command \"& 'D:/hermes-btc/.venv/Scripts/python.exe' -m py_compile '${COLLECT_PY}'\"" 2>&1 | tail -5

echo ""
echo "=========================================="
echo "✅ 硬编码路径已参数化"
echo "=========================================="
