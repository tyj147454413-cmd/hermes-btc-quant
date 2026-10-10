#!/bin/bash
# Hermes 样本外回测每日自动运行脚本
# 触发时间：每天 20:15 ET（美东收盘后，全天结算结果已出）
# 调度方式：openclaw cron 调用
# 用法：bash scripts/oos_daily.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_FILE="$PROJECT_DIR/data/oos_daily.log"

cd "$PROJECT_DIR"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始样本外回测..." >> "$LOG_FILE"

python3 scripts/backtest_oos.py --since 2026-08-17 --daily --save >> "$LOG_FILE" 2>&1

EXIT_CODE=$?
if [ $EXIT_CODE -eq 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 样本外回测完成" >> "$LOG_FILE"
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 样本外回测失败 (exit code: $EXIT_CODE)" >> "$LOG_FILE"
    exit $EXIT_CODE
fi

# Walk-Forward 效率比
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Walk-Forward 效率比分析..." >> "$LOG_FILE"
python3 scripts/walk_forward_analysis.py >> "$LOG_FILE" 2>&1
WF_EXIT=$?
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Walk-Forward 完成 (exit: $WF_EXIT)" >> "$LOG_FILE"

exit 0