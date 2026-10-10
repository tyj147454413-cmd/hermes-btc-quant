#!/usr/bin/env python3
"""
引擎代码审计（阶段 D）

逐段审计 rule_engine.py + signal_validator.py 的潜在问题。
不改代码，仅输出可疑点。

用法：python3 scripts/engine_audit.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from signal_validator import extract_factors
from rule_engine import judge, _probability, H12_DEPTH_THRESHOLD

SNAPSHOT_DIR = Path("/Users/tangyijun/Desktop/hermes btc/data/snapshots")


def main() -> int:
    issues: list[dict] = []
    counts: dict[str, int] = defaultdict(int)

    for p in sorted(SNAPSHOT_DIR.glob("*.json")):
        slug = p.name.split("_202")[0]
        try:
            md = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        try:
            factors, inp, notes, mode = extract_factors(md)
        except Exception:
            continue

        buy_wall = inp.pop("_buy_wall_ratio")
        sell_wall = inp.pop("_sell_wall_ratio")
        taker_bs = inp.pop("taker_bs_ratio", None)

        prelim = judge(dict(inp, extreme_wall=False))
        prelim_dir = prelim.final_direction
        if prelim_dir == "DOWN":
            extreme_wall = bool(sell_wall is not None and sell_wall > 3.0)
        elif prelim_dir == "UP":
            extreme_wall = bool(buy_wall is not None and buy_wall > 3.0)
        else:
            extreme_wall = False

        d = judge(dict(inp, extreme_wall=extreme_wall,
                       _buy_wall_ratio=buy_wall, _sell_wall_ratio=sell_wall,
                       taker_bs_ratio=taker_bs))

        # 审计1: H12 降权不覆盖 price_position / wall_divergence
        # ⚠️ round44（2026-08-26）起 wall_divergence 记分 ±4→±1（W_WALL_DIV=1），
        #    本审计的 abs(wd)>=4 阈值自此恒为 0 命中——保留仅作历史口径对照，
        #    如需继续监测贴线主导因子请改用 abs(wd)>=1。
        if d.h12_triggered:
            counts["h12_triggered"] += 1
            pp = factors.get("price_position", 0)
            wd = factors.get("wall_divergence", 0)
            # 如果 price_position 是主导方向来源，H12 降权后方向可能仍未改变
            if abs(pp) >= 2 and abs(wd) >= 1:  # round44: 原 >=4（旧 ±4 口径）
                counts["h12_with_pp_wd"] += 1

        # 审计2: probability 函数 EV 检查
        if d.probability is not None:
            prob = d.probability
            if prob >= 66:
                counts["prob_at_max"] += 1
            # 计算 EV: prob% × 赢(1-0.60) - (1-prob%) × 输(0.60)
            ev = (prob / 100 * (1 - 0.60)) - ((1 - prob / 100) * 0.60)
            if ev < 0:
                counts["prob_negative_ev"] += 1

        # 审计3: 贴线 + 摆动市互斥
        # 摆动市时贴线禁用，但若偏差<0.05% 摆动市下被禁贴线
        regime = inp.get("regime")
        dev = float(inp.get("deviation_pct", 0) or 0)
        if regime == "摆动市" and abs(dev) < 0.05:
            counts["swing_tie_dev"] += 1

        # 审计4: G5.4 深度档 cover_to 可达性
        trace_str = "|".join(d.trace)
        if "G5.4 覆盖" in trace_str:
            counts["g54_cover_to"] += 1

        # 审计5: F1U/BB3 触发次数
        if "F1U" in trace_str:
            counts["f1u_triggered"] += 1
        if "BB3" in trace_str:
            counts["bb3_triggered"] += 1

        # 审计6: position_scale "min" 触发次数
        if d.position_scale == "min":
            counts["scale_min"] += 1

        # 审计7: 结算期降权正确性
        if "结算期" in mode:
            counts["settlement_mode"] += 1
            # 检查 Taker/订单流是否被降权
            for k in ("taker_bs", "taker_1h", "order_flow"):
                v = factors.get(k, 0)
                if v != 0:
                    counts[f"settlement_{k}"] += 1

        # 审计8: extreme_wall 两遍判定
        # prelim 方向 vs 最终方向是否一致
        if prelim_dir and d.final_direction and prelim_dir != d.final_direction:
            counts["extreme_wall_dir_change"] += 1

        counts["total"] += 1

    print("## 引擎代码审计\n")
    print(f"样本: {counts['total']} 场\n")

    # 审计1
    print("### 审计1: H12 降权覆盖范围")
    print(f"- H12 触发: {counts['h12_triggered']} 次")
    print(f"- 其中 price_position≥2 且 wall_divergence≠0 并存: {counts['h12_with_pp_wd']} 次（round44 起 wd=±1 口径）")
    if counts["h12_with_pp_wd"] > 0:
        print("  ⚠️ H12 降权不降 price_position 和 wall_divergence，贴线+深势时这两个因子可能主导 S0")
    else:
        print("  ✅ 未发现 price_position 和 wall_divergence 同时主导 H12 场景")
    print()

    # 审计2
    print("### 审计2: probability 函数与 EV")
    print(f"- probability 达到上限 66%: {counts['prob_at_max']} 场")
    print(f"- 负 EV 场次: {counts['prob_negative_ev']} 场")
    print(f"  EV = 概率% × (1-0.60) - (1-概率%) × 0.60")
    print(f"  最高概率 66%: EV = 0.66×0.40 - 0.34×0.60 = 0.264 - 0.204 = +0.06")
    print(f"  概率 53.5%: EV = 0.535×0.40 - 0.465×0.60 = 0.214 - 0.279 = -0.065")
    print(f"  ⚠️ 概率低于 60% 时 EV 为负，但当前封顶 0.60 下保本 EV 概率 = 60%")
    print()

    # 审计3
    print("### 审计3: 贴线 + 摆动市互斥")
    print(f"- 摆动市 + 偏差<0.05% 场景: {counts['swing_tie_dev']} 场")
    if counts["swing_tie_dev"] > 0:
        print("  ⚠️ 摆动市下贴线被禁用，这些场景使用了常规矩阵而非贴线专列")
        print("  但偏差<0.05% 意味着价格极贴锚定，常规矩阵因子（MACD/K线等）可能噪音大")
    else:
        print("  ✅ 当前样本中无摆动市+偏差<0.05% 场景")
    print()

    # 审计4
    print("### 审计4: G5.4 深度档 cover_to 可达性")
    print(f"- G5.4 覆盖翻向触发: {counts['g54_cover_to']} 次")
    if counts["g54_cover_to"] == 0:
        print("  ⚠️ 深度档 cover_to 在全部样本中 0 次触发——逻辑永远不可达，可能是死代码")
        print("  触发条件: 深度多头/空头 + 衰竭 + 诱多/诱空 + fakeout_dir != trend_dir")
        print("  需要同时满足: macd1/macd4 同向深势 + |hist|≥30 + 1H Taker >70%/<30% + fakeout 存在")
    else:
        print(f"  ✅ 触发 {counts['g54_cover_to']} 次")
    print()

    # 审计5
    print("### 审计5: round18 候选翻转型 F1U/BB3")
    print(f"- F1U 触发: {counts['f1u_triggered']} 次")
    print(f"- BB3 触发: {counts['bb3_triggered']} 次")
    if counts["f1u_triggered"] == 0 and counts["bb3_triggered"] == 0:
        print("  ⚠️ 候选翻转型全部 0 触发——占位符逻辑，建议移除或调整阈值使其可达")
    print()

    # 审计6
    print("### 审计6: position_scale 'min' 触发")
    print(f"- scale='min' 触发: {counts['scale_min']} 次")
    if counts["scale_min"] == 0:
        print("  ⚠️ 9.7#5（贴线+尾盘<10min+PM≥70%同向→scale=min）0 触发，条件过于严苛")
    print()

    # 审计7
    print("### 审计7: 结算期降权统计")
    print(f"- 结算期模式: {counts['settlement_mode']} 场")
    print(f"- 其中 taker_bs 降权: {counts['settlement_taker_bs']} 次")
    print(f"- 其中 taker_1h 降权: {counts['settlement_taker_1h']} 次")
    print(f"- 其中 order_flow 降权: {counts['settlement_order_flow']} 次")
    print()

    # 审计8
    print("### 审计8: extreme_wall 两遍判定")
    print(f"- 预判方向 vs 最终方向不一致: {counts['extreme_wall_dir_change']} 次")
    if counts["extreme_wall_dir_change"] > 0:
        print(f"  ⚠️ extreme_wall 注入后方向改变 {counts['extreme_wall_dir_change']} 次，需确认是否因 G5.4 覆盖等合理原因")
    else:
        print("  ✅ 预判方向与最终方向一致，两遍判定无方向偏差")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())