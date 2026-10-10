#!/usr/bin/env bash
# scripts/hermes_run.sh — Hermes BTC 命令包装器
#
# 自动 source hermes_env.sh 并 cd 到项目目录,然后执行传入的命令
# 用于避免每次手动处理空格路径
#
# 用法:
#   bash scripts/hermes_run.sh <任意命令>
#
# 示例:
#   bash scripts/hermes_run.sh ls data/snapshots/ | tail -5
#   bash scripts/hermes_run.sh cat > analysis_report.md << 'EOF' ... EOF
#   bash scripts/hermes_run.sh .venv/bin/python hermes_collect.py --force --slim

# 获取脚本所在目录(无论从哪里调用都准)
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Source 环境变量
source "$SCRIPT_DIR/hermes_env.sh"

# cd 到项目目录
cd "$HERMES_DIR" || exit 1

# 执行传入的命令
exec "$@"
