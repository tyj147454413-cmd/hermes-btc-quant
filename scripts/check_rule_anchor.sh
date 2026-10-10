#!/usr/bin/env bash
# ============================================================
# Hermes 规则版本锚点一致性检查（升级计划书 任务 0.2 · 只读版）
#
# 权威基准：prompt_exec.md（用户指定：它是最新版本，其他文件对照它）
#
# 安全声明：纯只读。只 grep 各文件的锚点行并比对主版本号，
#           不修改、不写入任何文件（输出仅到 stdout）。
#
# 比对语义（与项目自身口径一致）：
#   - 主版本号 = round 后的第一个数字（如 round41.3 → 主版本 41）
#   - 硬性一致（基准 vs）：SKILL.md、rule_engine.py —— 主版本不一致 → exit 1
#   - CLAUDE.md：主版本不一致 → ⚠️ 警告但不失败（项目定义
#     「仅末尾补丁小数不同不算大改」；主版本不同属大改未同步，应尽快按 SYNC_MAP 同步）
#   - prompt.md：人工档案，允许滞后 → 仅提示 ℹ️
#
# 兼容性：macOS 自带 bash 3.2（无 declare -A），本脚本只用普通变量。
#
# 用法：
#   bash scripts/check_rule_anchor.sh            # 打印报告
#   bash scripts/check_rule_anchor.sh --selftest # 追加跑 rule_engine 自检
#   （cron/launchd 用 exit code 判断是否告警）
# ============================================================

set -u
cd "$(dirname "$0")/.." || exit 1

RED=$'\033[31m'; YEL=$'\033[33m'; GRN=$'\033[32m'; CYN=$'\033[36m'; OFF=$'\033[0m'

FAIL=0
WARN=0

# ---- 提取器 ------------------------------------------------------
get_round_token() {   # 一行文本 → 第一个 round<N>[.M] 记号
    printf '%s' "$1" | grep -oE 'round[0-9]+(\.[0-9]+)?' | head -1
}

get_main_version() {  # round41.3 → 41 ；无匹配 → 空
    printf '%s' "$1" | sed -E 's/^round([0-9]+).*/\1/'
}

# check_one <名称> <文件> <定位pattern>
# 结果写入全局 LAST_OK / LAST_TOKEN / LAST_MAIN
check_one() {
    LAST_OK=0; LAST_TOKEN=""; LAST_MAIN=""
    local name="$1" file="$2" pattern="$3" line token main
    if [ ! -f "$file" ]; then
        echo "${RED}✗ $name：文件不存在 ($file)${OFF}"
        return
    fi
    line=$(grep -m1 -E "$pattern" "$file" 2>/dev/null)
    if [ -z "$line" ]; then
        echo "${RED}✗ $name：找不到锚点行 ($file)${OFF}"
        return
    fi
    token=$(get_round_token "$line")
    if [ -z "$token" ]; then
        echo "${RED}✗ $name：锚点行存在但解析不出 round 版本号${OFF}"
        return
    fi
    main=$(get_main_version "$token")
    LAST_OK=1; LAST_TOKEN="$token"; LAST_MAIN="$main"
    echo "${CYN}· $name${OFF}: $token（主版本 $main） ← ${file}"
}

echo "═══ Hermes 规则锚点一致性检查（基准 = prompt_exec.md） ═══"

# ---- 基准 ----
check_one "prompt_exec.md(基准)" "prompt_exec.md" '\*\*版本锚点\*\*'
V_BASE="$LAST_MAIN"
if [ "$LAST_OK" -eq 0 ] || [ -z "$V_BASE" ]; then
    echo "${RED}✗ 基准锚点缺失，无法继续比对${OFF}"
    exit 1
fi

# ---- 硬性一致组 ----
check_one "rule_engine.py" "scripts/rule_engine.py" '^RULE_VERSION *= *"'
V_ENGINE="$LAST_MAIN"; T_ENGINE="$LAST_TOKEN"
if [ "$LAST_OK" -eq 0 ]; then FAIL=$((FAIL+1));
elif [ "$V_ENGINE" != "$V_BASE" ]; then
    echo "${RED}✗ 硬性不一致：rule_engine.py (${T_ENGINE}) vs 基准主版本 $V_BASE${OFF}"
    FAIL=$((FAIL+1))
fi

check_one "SKILL.md" ".codebuddy/skills/hermes-btc-analysis/SKILL.md" '当前版本锚点'
V_SKILL="$LAST_MAIN"; T_SKILL="$LAST_TOKEN"
if [ "$LAST_OK" -eq 0 ]; then FAIL=$((FAIL+1));
elif [ "$V_SKILL" != "$V_BASE" ]; then
    echo "${RED}✗ 硬性不一致：SKILL.md (${T_SKILL}) vs 基准主版本 $V_BASE${OFF}"
    FAIL=$((FAIL+1))
fi

# ---- CLAUDE.md（警告级）----
check_one "CLAUDE.md" "CLAUDE.md" '当前铁律锚点'
V_CLAUDE="$LAST_MAIN"; T_CLAUDE="$LAST_TOKEN"
if [ "$LAST_OK" -eq 0 ]; then WARN=$((WARN+1))
elif [ "$V_CLAUDE" != "$V_BASE" ]; then
    echo "${YEL}⚠️ CLAUDE.md 锚点(${T_CLAUDE}, 主版本 $V_CLAUDE)落后于基准($V_BASE)——属大改未同步，请按 SYNC_MAP.md 连带更新${OFF}"
    WARN=$((WARN+1))
else
    # 主版本一致但补丁号可能不同 → 提示
    T_FULL=$(get_round_token "$(grep -m1 '当前铁律锚点' CLAUDE.md)")
    B_FULL=$(get_round_token "$(grep -m1 -E '\*\*版本锚点\*\*' prompt_exec.md)")
    [ "$T_FULL" != "$B_FULL" ] && \
        echo "${YEL}⚠️ CLAUDE.md(${T_FULL}) 与基准(${B_FULL})主版本相同、补丁号不同——不算大改，建议顺手同步${OFF}" && WARN=$((WARN+1))
fi

# ---- prompt.md（提示级，档案性质）----
check_one "prompt.md(档案)" "prompt.md" '当前版本锚点'
V_PROMPT="$LAST_MAIN"; T_PROMPT="$LAST_TOKEN"
if [ "$LAST_OK" -eq 1 ] && [ "$V_PROMPT" != "$V_BASE" ]; then
    echo "ℹ️ prompt.md 锚点为 ${T_PROMPT}，滞后于基准 $V_BASE——人工档案允许滞后；如需对齐请人工确认后同步"
fi

# ---- C3(2026-08-23) WF 关卡留痕（审计 P1-7 制度化）----
# 规则：当前主版本在 data/wf_reports/index.jsonl 必须有 ≥1 份 WF 报告。
# 主版本变更（round NN→MM）前必须先跑 python3 scripts/wf_gate.py run 拿报告编号，
# 才可登记 SYNC_MAP。报告文件缺失/被改也会在此暴露。
WF_INDEX="data/wf_reports/index.jsonl"
if [ ! -f "$WF_INDEX" ]; then
    echo "${RED}✗ [WF关卡] 无任何 WF 报告($WF_INDEX 不存在)——主版本 $V_BASE 变更登记前须先跑: python3 scripts/wf_gate.py run${OFF}"
    FAIL=$((FAIL+1))
else
    _wf_hits=$(grep -c "\"for_major\": \"${V_BASE}\"" "$WF_INDEX" 2>/dev/null)
    [ -z "${_wf_hits:-}" ] && _wf_hits=0
    _wf_latest=$(tail -1 "$WF_INDEX" | grep -oE 'WF-[0-9]{8}-[0-9]{4}-[0-9a-f]{4}' | head -1)
    if [ "${_wf_hits}" -ge 1 ]; then
        echo "${CYN}· WF关卡${OFF}: 主版本 $V_BASE 已有 ${_wf_hits} 份 WF 报告｜最新 ${_wf_latest:-?}"
    else
        echo "${RED}✗ [WF关卡] 主版本 $V_BASE 无 WF 报告留痕——上线/登记 SYNC_MAP 前先跑: python3 scripts/wf_gate.py run${OFF}"
        FAIL=$((FAIL+1))
    fi
fi

echo ""

# ---- 可选：引擎自检 ----
if [ "${1:-}" = "--selftest" ]; then
    echo "─── 附带 rule_engine.py --selftest ───"
    if python3 scripts/rule_engine.py --selftest >/dev/null 2>&1; then
        echo "${GRN}✓ rule_engine selftest 通过${OFF}"
    else
        echo "${RED}✗ rule_engine selftest 失败${OFF}"
        FAIL=$((FAIL+1))
    fi
    echo ""
fi

if [ "$FAIL" -eq 0 ]; then
    echo "${GRN}✓ 核心一致性通过（基准主版本 $V_BASE）｜警告=$WARN${OFF}"
else
    echo "${RED}✗ 存在硬性不一致（$FAIL 处），禁止在漂移状态下改规则/上线新版本${OFF}"
fi

echo "═══ 结果：硬性失败=$FAIL 警告=$WARN ═══"
[ "$FAIL" -eq 0 ]
