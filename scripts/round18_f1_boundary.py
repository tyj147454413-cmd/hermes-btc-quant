#!/usr/bin/env python3
"""round18 F1 翻转的针对性边界 bug 验证（2026-08-14）。

验证 3 个核心缺陷：
A. F1 翻转后新方向是否绕过 G4/H9 价格位置硬约束（|dev|≥0.15% 时翻转方向逆价格位置）。
B. F1 命中场若 |dev|<0.05（贴线激活），9.7#4 是否残留 scale=0.5（违反"翻转级不缩仓"）。
C. F1 翻转后 9.7#1/#2 顺共识风险标注语义错位（翻转前 DOWN 顺共识已降级，翻转后 UP 逆共识）。

构造场景，直接调用 judge 验证。
"""
from __future__ import annotations

import sys

sys.path.insert(0, "scripts")
from rule_engine import judge  # noqa: E402


def main() -> int:
    print("=== A) F1 翻转绕开 G4/H9 价格位置校验 ===")
    print("场景：原始 DOWN + 贴线 + 顺共识 + 衰竭 + |hist4|<50 + 净分≤-3，但 |dev|=0.30%（逆价格位置）")
    print("→ F1 满足（未检查价格位置），翻转成 UP。但 UP 逆 price_dir=DOWN，G4 本应拦截。\n")
    # 构造：原始 S0=-6 DOWN，PM DOWN 65%（顺共识），dev=-0.30%（price_dir=DOWN，翻转后 UP 逆价格位置）
    # 注意：G4 在翻转前执行，direction=DOWN 与 price_dir=DOWN 同向，G4 不拦截。F1 后翻转成 UP 未再校验。
    # 但实际 extract_factors 的 price_position 因子 = sign(dev)*w = DOWN，会拉向 DOWN。
    dA = judge({
        "pm_up": 35.0,           # pm_up<50 → pm_dir=DOWN，顺共识（60-80%）
        "deviation_pct": -0.30,  # |dev|≥0.15%，price_dir=DOWN
        "tie_line_active": True,
        "_tie_mode": True,
        "macd_1h_hist": 5.0, "macd_4h_hist": 30.0,   # |hist4|=30<50
        "taker_1h_buy_pct": 50.0,
        "macd_4h_accel": 5.0,     # DOWN + accel>0 → 衰竭
        "historical_up_rate": 0.58,  # >0.55 偏多，预测DOWN=反向历史
        "factors": {"price_position": -2, "taker_bs": -2, "order_flow": -2, "taker_1h": 0},
    })
    print(f"  → direction={dA.final_direction} net={dA.net:+.1f} scale={dA.position_scale} conf={dA.confidence}")
    print(f"    dev=-0.30% → price_dir=DOWN，若翻转成 UP 则逆价格位置（G4 未拦截！）")
    for r in dA.risks:
        if "翻转" in r or "H9" in r or "G4" in r:
            print(f"    risk: {r}")
    print()

    print("=== B) F1 命中场 |dev|<0.05 → 9.7#4 残留 scale=0.5 ===")
    print("场景：原始 DOWN + 贴线激活(|dev|<0.05) + 顺共识 + 衰竭 + |hist4|<50 + 净分≤-3")
    print("→ F1 命中翻转 UP，但 9.7#4（贴线+顺PM）已注入 scale=0.5 → 翻转级实际缩仓，违反设计\n")
    dB = judge({
        "pm_up": 35.0,
        "deviation_pct": -0.03,   # |dev|<0.05 → 贴线激活，9.7#4 会触发
        "tie_line_active": True,
        "_tie_mode": True,
        "macd_1h_hist": 5.0, "macd_4h_hist": 20.0,   # |hist4|=20<50
        "taker_1h_buy_pct": 50.0,
        "macd_4h_accel": 5.0,
        "historical_up_rate": 0.58,
        "factors": {"price_position": -2, "taker_bs": -2, "order_flow": -2, "taker_1h": 0},
    })
    print(f"  → direction={dB.final_direction} net={dB.net:+.1f} scale={dB.position_scale} conf={dB.confidence}")
    print(f"    |dev|=0.03% 贴线激活 + 顺共识 → 9.7#4 注入 0.5。翻转级要求不缩仓，但 scale={dB.position_scale}")
    for r in dB.risks:
        if "翻转" in r or "贴线" in r or "共识" in r:
            print(f"    risk: {r}")
    print()

    print("=== C) F1 翻转后顺共识风险标注语义错位 ===")
    print("场景：原始 DOWN 顺共识 → 翻转 UP（逆共识）。9.7#1 已标注'顺共识区，庄家收割风险'")
    print("→ 翻转后实为逆共识，但风险标注仍为'顺共识区'，且 confidence 已被降级\n")
    dC = judge({
        "pm_up": 37.5,
        "deviation_pct": -0.0735,  # 贴线维持（|dev|>0.05，9.7#4 不触发）
        "tie_line_active": False,
        "_tie_mode": True,
        "macd_1h_hist": -5.69, "macd_4h_hist": -9.44,  # |hist4|<50
        "taker_1h_buy_pct": 47.3,
        "macd_4h_accel": 2.57, "historical_up_rate": 0.571,
        "factors": {"price_position": -1, "taker_bs": -1, "taker_1h": 0, "order_flow": -2},
    })
    print(f"  → direction={dC.final_direction} net={dC.net:+.1f} scale={dC.position_scale} conf={dC.confidence}")
    print("    翻转后 UP 实际是逆共识（pm_dir=DOWN），但 confidence 因翻转前 9.7#1 顺共识被降到低")
    for r in dC.risks:
        print(f"    risk: {r}")
    print()

    print("=== D) 对照：原始 DOWN 逆价格位置但方向正确不翻 ===")
    print("场景：原始 DOWN + 贴线 + 顺共识 + 衰竭 + |hist4|=60≥50（深势）→ 观察级不动\n")
    dD = judge({
        "pm_up": 35.0,
        "deviation_pct": -0.09,
        "tie_line_active": True,
        "_tie_mode": True,
        "macd_1h_hist": -80.0, "macd_4h_hist": -150.0,  # |hist4|=150≥50 深势
        "taker_1h_buy_pct": 45.0,
        "macd_4h_accel": 3.2, "historical_up_rate": 0.58,
        "factors": {"price_position": -2, "taker_bs": -2, "taker_1h": -2, "order_flow": -2},
    })
    print(f"  → direction={dD.final_direction} scale={dD.position_scale} conf={dD.confidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
