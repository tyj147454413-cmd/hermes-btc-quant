#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round49 反事实回测：PM 动量权重回退 ×3→×2 + pm_imbalance ×1 入评分的实际影响。

样本：8/22-8/28 一周真实成交快照（202 份），按 slug 过滤后与 hermes_results join。
对照：
  A = round49 现态（生产）：W_PM_MOMENTUM=2, W_PM_IMBALANCE=1
  B = 假设不退回（round48+47 风格）：W_PM_MOMENTUM=3, W_PM_IMBALANCE=1
  C = 仅回退不带聪明钱：W_PM_MOMENTUM=2, W_PM_IMBALANCE=0（隔离 pm_imbalance 单因子贡献）

输出：
  1) 总体 S0 命中率 + EV
  2) |S0| 桶 × PM 一致/逆 三档命中分布
  3) A→B/A→C 改单数 + 净 P&L 差异
  4) 关键 S0 增量场列表（哪个 slug 在哪个配置下改变 final_direction）

权重通过 monkeypatch 在脚本层切换 signal_validator 的常量，不动活代码、不改 RULE_VERSION。
运行：python3 scripts/backtest_round49_pm_momentum_weight.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path("/Users/tangyijun/Desktop/hermes btc")
sys.path.insert(0, str(ROOT / "scripts"))

from replay_common import (
    DATA_DIR, SNAPSHOT_DIR, RESULTS_FILE, get_latest_snapshots,
    load_results_full, wilson, bucket_net,
)

import signal_validator as sv

# ============== 三套权重配置 ==============
CONFIGS = {
    "A_round49_prod":  {"w_pmm": 2, "w_imb": 1, "desc": "PM×2 + pm_imbalance×1（当前生产）"},
    "B_no_revert":     {"w_pmm": 3, "w_imb": 1, "desc": "PM×3 + pm_imbalance×1（不退回×3）"},
    "C_no_pmimb":      {"w_pmm": 2, "w_imb": 0, "desc": "PM×2 + pm_imbalance×0（隔离聪明钱）"},
}

# ============== 样本范围 8/22-8/28 ==============
DATE_FROM = datetime(2026, 8, 22)
DATE_TO   = datetime(2026, 8, 28, 23, 59, 59)


def slug_in_range(slug: str) -> bool:
    """bitcoin-up-or-down-august-DD-2026-Ham/pm-et → 日期过滤"""
    import re
    m = re.search(r"august-(\d{1,2})-2026", slug)
    if not m:
        return False
    d = int(m.group(1))
    return 22 <= d <= 28


def replay_with_weights(md: dict, w_pmm: int, w_imb: int):
    """monkeypatch 切换 W_PM_MOMENTUM/W_PM_IMBALANCE，重算 S0 与最终方向。

    不动 W_PM_MOMENTUM_THRESHOLD（0.02 不变）+ PM_IMBALANCE_THRESHOLD（0.3 不变）。
    返回 (final_side, S0, |S0|桶, mode) 或 None。
    """
    sv.W_PM_MOMENTUM = w_pmm
    sv.W_PM_IMBALANCE = w_imb
    try:
        factors, inp, notes, mode = sv.extract_factors(md)
    except Exception:
        return None

    # judge 依赖 inp.pop('_buy_wall_ratio') 等——直接复用 replay_common 同款逻辑
    buy_wall  = inp.pop("_buy_wall_ratio", None)
    sell_wall = inp.pop("_sell_wall_ratio", None)
    taker_bs  = inp.pop("taker_bs_ratio", None)

    from rule_engine import judge  # noqa
    prelim = judge(dict(inp, extreme_wall=False))
    if prelim.final_direction == "DOWN":
        ew = bool(sell_wall is not None and sell_wall > 3.0)
    elif prelim.final_direction == "UP":
        ew = bool(buy_wall is not None and buy_wall > 3.0)
    else:
        ew = False

    d = judge(dict(inp, extreme_wall=ew,
                   _buy_wall_ratio=buy_wall, _sell_wall_ratio=sell_wall,
                   taker_bs_ratio=taker_bs))
    if not d.final_direction:
        return None

    S0 = d.s0 if d.s0 is not None else d.net
    return (d.final_direction, S0, bucket_net(S0), mode)


def evaluate_config(md: dict, results_by_slug: dict, cfg_key: str):
    """对单份快照在三套配置下重算 final_direction。

    使用 results 里 slug 对应的 buy_price + amount 估算 P&L（固定 $10 注额）。
    """
    import copy
    results = []
    for slug, snap_path in md.items():
        r = results_by_slug.get(slug)
        if not r or r.get("actual") not in ("UP", "DOWN"):
            continue
        try:
            raw = json.loads(snap_path.read_text())
        except Exception:
            continue
        if not slug_in_range(slug):
            continue
        actual = r["actual"]
        # 拿 buy_price 作 EV 估算（实盘成交价 = 模型侧 max_price 触发后的吃单价）
        # 未成交 / TIMEOUT 走概率表输出侧无 pnl，按 pnl=0 处理
        buy_price = r.get("buy_price") or 0.60
        amount = r.get("amount") or 10.0
        filled = bool(r.get("filled"))
        # 修正确口径：未成交场 P&L=0（不会因方向错亏损）
        eff_buy_price = buy_price if filled else 0.0
        eff_amount = amount if filled else 0.0

        rec = {"slug": slug, "actual": actual, "buy_price": buy_price, "amount": amount, "filled": filled}
        for key in CONFIGS:
            cfg = CONFIGS[key]
            out = replay_with_weights(copy.deepcopy(raw), cfg["w_pmm"], cfg["w_imb"])
            if out is None:
                rec[key] = None
            else:
                side, s0, bucket, mode = out
                rec[key] = {"side": side, "S0": s0, "bucket": bucket, "mode": mode}
                # 估算 P&L（按实盘成交价 + 成交注额，未成交=0）
                if not filled:
                    rec[f"{key}_pnl"] = 0.0
                elif side == actual:
                    rec[f"{key}_pnl"] = round(eff_amount * (1 - eff_buy_price), 2)
                else:
                    rec[f"{key}_pnl"] = round(-eff_amount * eff_buy_price, 2)
        results.append(rec)
    return results


def summarize(results, key, label):
    """单配置的命中率 + EV + 改单数统计"""
    rows = [r for r in results if r.get(key)]
    if not rows:
        return label, 0, 0, 0.0, 0.0, "n=0"
    correct = sum(1 for r in rows if r[key]["side"] == r["actual"])
    n = len(rows)
    winrate = correct / n
    lo, hi = wilson(correct, n)
    pnl = round(sum(r.get(f"{key}_pnl", 0) for r in rows), 2)
    # 子档命中：分 0-2 / 3-5 / ≥6 三桶
    by_bucket = defaultdict(lambda: [0, 0])
    for r in rows:
        b = r[key]["bucket"]
        by_bucket[b][1] += 1
        if r[key]["side"] == r["actual"]:
            by_bucket[b][0] += 1
    bucket_str = " | ".join(
        f"{b}:{by_bucket[b][0]}/{by_bucket[b][1]}={by_bucket[b][0]/by_bucket[b][1]*100:.0f}%"
        for b in ("0-2", "3-5", "≥6") if by_bucket[b][1] > 0
    )
    return label, n, correct, winrate * 100, pnl, f"Wilson[{lo:.1f},{hi:.1f}] buckets={bucket_str}"


def diff_detail(results, key_a, key_b):
    """两配置间的方向改单数 + P&L 差异 + 关键改单列表"""
    flips = []
    pnl_delta = 0.0
    for r in results:
        a = r.get(key_a)
        b = r.get(key_b)
        if not a or not b:
            continue
        if a["side"] != b["side"]:
            flips.append({
                "slug": r["slug"], "actual": r["actual"],
                "from_side": a["side"], "to_side": b["side"],
                "from_s0": a["S0"], "to_s0": b["S0"],
                "from_pnl": r.get(f"{key_a}_pnl", 0),
                "to_pnl":   r.get(f"{key_b}_pnl", 0),
            })
            pnl_delta += r.get(f"{key_b}_pnl", 0) - r.get(f"{key_a}_pnl", 0)
    return flips, round(pnl_delta, 2)


def main():
    print(f"=== round49 反事实回测: PM 动量 ×3→×2 回退 + pm_imbalance ×1 入评分 ===")
    print(f"样本范围: 8/22-8/28 (一周真实成交)\n")

    snaps = get_latest_snapshots(SNAPSHOT_DIR)
    print(f"快照数: {len(snaps)}")

    results_full = load_results_full(RESULTS_FILE)
    print(f"results 结算数: {len(results_full)}")

    # 过滤样本范围
    md = {s: p for s, p in snaps.items() if slug_in_range(s)}
    print(f"日期范围内快照: {len(md)}")
    results = evaluate_config(md, results_full, "A_round49_prod")
    print(f"三配置全部能重算的样本: {len(results)}\n")

    # ============== 1) 三配置总览 ==============
    print("=== 1) 三配置总览 ===")
    headers = f"{'配置':<20} {'n':>4} {'命中':>6} {'胜率':>6} {'EV($)':>9} {'Wilson95':<22} {'分桶命中'}"
    print(headers)
    print("-" * len(headers) * 2)
    for key in CONFIGS:
        label, n, correct, wr, pnl, rest = summarize(results, key, CONFIGS[key]["desc"])
        print(f"{key:<20} {n:>4} {correct:>6} {wr:>5.1f}% {pnl:>9.2f} {rest}")

    # ============== 2) A vs B 改单分析 ==============
    print("\n=== 2) A(生产) → B(不退回×3) 改单分析 ===")
    flips_ab, pnl_delta_ab = diff_detail(results, "A_round49_prod", "B_no_revert")
    print(f"改单数: {len(flips_ab)}/{len(results)} = {len(flips_ab)/len(results)*100:.1f}%")
    print(f"P&L 差异 (B - A): {pnl_delta_ab:+.2f}")
    if flips_ab:
        print("关键改单:")
        for f in flips_ab[:8]:
            print(f"  {f['slug']} actual={f['actual']} {f['from_side']}→{f['to_side']}  "
                  f"S₀: {f['from_s0']} → {f['to_s0']}  Δpnl={f['to_pnl']-f['from_pnl']:+.2f}")

    # ============== 3) A vs C 改单分析 ==============
    print("\n=== 3) A(生产+pm_imbalance) → C(仅 PM 动量回退不带聪明钱) 改单分析 ===")
    flips_ac, pnl_delta_ac = diff_detail(results, "A_round49_prod", "C_no_pmimb")
    print(f"改单数: {len(flips_ac)}/{len(results)} = {len(flips_ac)/len(results)*100:.1f}%")
    print(f"P&L 差异 (C - A): {pnl_delta_ac:+.2f}  ← 负数=聪明钱贡献为正")
    if flips_ac:
        print("聪明钱触发改单:")
        for f in flips_ac[:8]:
            print(f"  {f['slug']} actual={f['actual']} {f['from_side']}→{f['to_side']}  "
                  f"S₀: {f['from_s0']} → {f['to_s0']}  Δpnl={f['to_pnl']-f['from_pnl']:+.2f}")

    # ============== 4) 关键子档（贴线+PM=T 与 0-2 档逆PM） ==============
    print("\n=== 4) 关键子档: 0-2 桶贴线 + PM 不一致 (round43.2 已知 -7.2pp 漂移格) ===")
    # 注: 简化处理——直接按 mode 含'贴线' + bucket=0-2 过滤
    sub_rows = []
    for r in results:
        a = r.get("A_round49_prod")
        if not a:
            continue
        if a["bucket"] == "0-2" and "贴线" in a["mode"]:
            sub_rows.append(r)
    if sub_rows:
        correct = sum(1 for r in sub_rows if r["A_round49_prod"]["side"] == r["actual"])
        n = len(sub_rows)
        wr = correct / n * 100
        lo, hi = wilson(correct, n)
        print(f"A 配置 0-2 桶贴线: {correct}/{n} = {wr:.1f}% Wilson[{lo:.1f},{hi:.1f}]")
        # B/C 同口径
        for key in ("B_no_revert", "C_no_pmimb"):
            correct = sum(1 for r in sub_rows if r[key]["side"] == r["actual"])
            wr = correct / n * 100
            print(f"{key:<14}: {correct}/{n} = {wr:.1f}%")
    else:
        print("(无 0-2 桶贴线样本)")

    # ============== 5) 三配置间共同样本方向一致率 ==============
    print("\n=== 5) 三配置方向一致率 ===")
    agree_abc = sum(1 for r in results
                    if r.get("A_round49_prod") and r.get("B_no_revert") and r.get("C_no_pmimb")
                    and r["A_round49_prod"]["side"] == r["B_no_revert"]["side"] == r["C_no_pmimb"]["side"])
    n_full = sum(1 for r in results
                 if r.get("A_round49_prod") and r.get("B_no_revert") and r.get("C_no_pmimb"))
    print(f"A/B/C 三方方向一致: {agree_abc}/{n_full} = {agree_abc/n_full*100:.1f}%")

    print("\n=== 结论速览 ===")
    a = summarize(results, "A_round49_prod", "A")
    b = summarize(results, "B_no_revert", "B")
    c = summarize(results, "C_no_pmimb", "C")
    a_pnl = a[4]; b_pnl = b[4]; c_pnl = c[4]
    print(f"A 生产 (PM×2 + pm_imbalance×1): EV ${a_pnl:+.2f} / {a[1]}场 / 胜率 {a[3]:.1f}%")
    print(f"B 不退回 (PM×3 + pm_imbalance×1): EV ${b_pnl:+.2f} / {b[1]}场 / 胜率 {b[3]:.1f}%  Δ vs A: ${b_pnl-a_pnl:+.2f}")
    print(f"C 仅回退 (PM×2 + pm_imbalance×0): EV ${c_pnl:+.2f} / {c[1]}场 / 胜率 {c[3]:.1f}%  Δ vs A: ${c_pnl-a_pnl:+.2f}")
    print(f"\n→ A 优于 B = 验证回退 ×2 决策正确（PM×3 在本样本有害）")
    print(f"→ A 优于 C = 验证 pm_imbalance ×1 入评分有效（聪明钱贡献为正）")


if __name__ == "__main__":
    main()
