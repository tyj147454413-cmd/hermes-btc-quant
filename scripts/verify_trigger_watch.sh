#!/bin/bash
# 验证 hermes-btc-hourly-analysis 在 15:13 和 16:13 两个整点准点触发。
# 观察引擎 main.log 的 is due / Started run 时间，并监控校准器是否把 next_run_at 拉回 :13。
LOG="/Users/tangyijun/Library/Application Support/CodeBuddy CN/logs/20260810T012335/main.log"
DB="/Users/tangyijun/Library/Application Support/CodeBuddy CN/automations/automations.db"
OUT="/Users/tangyijun/Desktop/hermes btc/logs/trigger_verify.log"
: > "$OUT"
echo "验证启动: $(date '+%Y-%m-%d %H:%M:%S') | 目标: 15:13 与 16:13 准点触发" >> "$OUT"

# 监控 15:13 整点（观察窗口 15:10:00 ~ 15:20:00）
echo "--- 观察 15:13 整点 ---" >> "$OUT"
for i in $(seq 1 130); do
    ts=$(date '+%H:%M:%S')
    hr=$(date '+%H'); mm=$(date '+%M')
    if [ "$hr" = "15" ]; then
        if [ "$mm" = "13" ] || [ "$mm" = "14" ] || [ "$mm" = "15" ]; then
            grep -E "15:1[3-5]" "$LOG" 2>/dev/null | grep -iE "is due|Started run" | tail -3 >> "$OUT"
            echo "  于 $ts 采样 (mm=$mm): next_run_at=$(sqlite3 "$DB" "SELECT datetime(next_run_at/1000,'unixepoch','+8 hours') FROM automations WHERE id='hermes-btc-hourly-analysis';")" >> "$OUT"
        fi
    fi
    if [ "$hr" = "15" ] && [ "$mm" = "16" ]; then
        echo "--- 15:13 观察结束 ---" >> "$OUT"
        break
    fi
    sleep 10
done
echo "--- 观察 16:13 整点 ---" >> "$OUT"
for i in $(seq 1 130); do
    ts=$(date '+%H:%M:%S')
    hr=$(date '+%H'); mm=$(date '+%M')
    if [ "$hr" = "16" ]; then
        if [ "$mm" = "13" ] || [ "$mm" = "14" ] || [ "$mm" = "15" ]; then
            grep -E "16:1[3-5]" "$LOG" 2>/dev/null | grep -iE "is due|Started run" | tail -3 >> "$OUT"
            echo "  于 $ts 采样 (mm=$mm): next_run_at=$(sqlite3 "$DB" "SELECT datetime(next_run_at/1000,'unixepoch','+8 hours') FROM automations WHERE id='hermes-btc-hourly-analysis';")" >> "$OUT"
        fi
    fi
    if [ "$hr" = "16" ] && [ "$mm" = "16" ]; then
        echo "--- 16:13 观察结束 ---" >> "$OUT"
        break
    fi
    sleep 10
done
echo "验证完成: $(date '+%Y-%m-%d %H:%M:%S')" >> "$OUT"
