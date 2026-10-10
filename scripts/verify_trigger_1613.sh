#!/bin/bash
# 验证 hermes-btc-hourly-analysis 在 16:13 整点准点触发（fix_next_run 冻结修复后）。
# 观察引擎 main.log 的 is due / Started run 时间，并采样 next_run_at 是否被越位推进。
LOG="/Users/tangyijun/Library/Application Support/CodeBuddy CN/logs/20260810T151549/main.log"
DB="/Users/tangyijun/Library/Application Support/CodeBuddy CN/automations/automations.db"
OUT="/Users/tangyijun/Desktop/hermes btc/logs/trigger_verify_1613.log"
: > "$OUT"
echo "验证启动: $(date '+%Y-%m-%d %H:%M:%S') | 目标: 16:13 准点触发" >> "$OUT"

# 观察窗口 16:10:00 ~ 16:16:59，每 10s 采样一次
for i in $(seq 1 50); do
    ts=$(date '+%H:%M:%S')
    hr=$(date '+%H'); mm=$(date '+%M')
    if [ "$hr" = "16" ] && [ "$mm" -ge 10 ] && [ "$mm" -le 16 ]; then
        due=$(grep -E "16:1[0-6]" "$LOG" 2>/dev/null | grep -iE "is due|Started run" | tail -2)
        nr=$(sqlite3 "$DB" "SELECT datetime(next_run_at/1000,'unixepoch','+8 hours') FROM automations WHERE id='hermes-btc-hourly-analysis';" 2>/dev/null)
        echo "  于 $ts 采样: next_run_at=$nr | $(echo "$due" | tr '\n' ' ')" >> "$OUT"
    fi
    if [ "$hr" = "16" ] && [ "$mm" -ge 17 ]; then
        break
    fi
    sleep 10
done
echo "验证完成: $(date '+%Y-%m-%d %H:%M:%S')" >> "$OUT"
