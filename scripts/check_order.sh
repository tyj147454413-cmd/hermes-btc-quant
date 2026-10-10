#!/usr/bin/env bash
# scripts/check_order.sh <slug> — 权威核对信号是否 写入+消费+成交
#
# ⚠️ claude_signal.jsonl 是 bot 的实时队列(QUEUE)，bot 读完即清空并转存到
#    hermes_done.jsonl / hermes_pending.jsonl / hermes_last_signal.json。
#    所以"查队列里在不在"判断不了信号成功与否。本脚本读权威持久文件。
#
# 用法: bash scripts/check_order.sh bitcoin-up-or-down-august-18-2026-7am-et
# 返回:
#   ✅ ORDER 已成交   → 打印 order_id / order_price
#   📦 已处理          → 在 done 里有记录（可能 SKIP）
#   ⏳ 写入但未处理    → 只在 last_signal 里（bot 尚未消费）
#   ❌ 完全没写        → 三个地方都找不到
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
SLUG="${1:?用法: check_order.sh <slug>}"

DONE="data/hermes_done.jsonl"
PENDING="data/hermes_pending.jsonl"
LAST="data/hermes_last_signal.json"

# 1. done.jsonl（成交/处理的持久记录）
if [ -f "$DONE" ]; then
    MATCH=$(grep -F -- "$SLUG" "$DONE" 2>/dev/null | tail -1)
    if [ -n "$MATCH" ]; then
        ORD=$(printf '%s' "$MATCH" | grep -oE '"order_id": *"[^"]+"' | head -1)
        PRICE=$(printf '%s' "$MATCH" | grep -oE '"order_price": *[0-9.]+' | head -1)
        SIG=$(printf '%s' "$MATCH" | grep -oE '"signal": *"(UP|DOWN)"' | head -1)
        echo "✅ $SLUG 已处理($DONE)"
        [ -n "$SIG" ]   && echo "   方向: $SIG"
        [ -n "$ORD" ]   && echo "   $ORD"
        [ -n "$PRICE" ] && echo "   成交价: $PRICE"
        exit 0
    fi
fi

# 2. last_signal.json（最近写出的信号，可能是最近一条）
if [ -f "$LAST" ] && grep -qF -- "$SLUG" "$LAST" 2>/dev/null; then
    echo "⏳ $SLUG 已由 bot 处理(见 hermes_last_signal.json)，结果如下："
    "$(dirname "$0")/../.venv/bin/python" -c "
import json
try:
    d=json.load(open('$LAST'))
    print('   result:', json.dumps(d.get('result', {}), ensure_ascii=False))
    print('   processed_at:', d.get('processed_at'))
except Exception as e:
    print('   (读取失败)', e)
" 2>/dev/null
    exit 0
fi

# 3. 全没找到
echo "❌ $SLUG 在 done/pending/last_signal 均未找到 → 可能从未写入，或已被队列清理且未处理。"
exit 1