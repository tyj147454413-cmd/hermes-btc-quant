#!/usr/bin/env python3
"""short_bounce_factor 权重扫荡测试 — round47 审计

对比: 权重 ×0 (废) / ×1 (现状) / ×2 (round42.1 前) / ×3 (PM动量同档) 四个版本下的:
  ① 模型 S0 方向正确率(全样本)
  ② 模型 S0 方向正确率(short_bounce 触发子集 n=86)
  ③ 模型 S0 方向正确率(short_bounce 未触发子集)
  ④ S0 翻向场次(short_bounce 改动权重后 S0 变号)的方向命中率(看 short_bounce 是否真在"翻车")
  ⑤ 模型净分(P&L 中性)分布变化
  ⑥ short_bounce 因子触发时其方向贡献正确率
"""
import json, os, re, sys
from pathlib import Path
from collections import defaultdict

ROOT = Path(os.environ["HERMES_DIR"])
sys.path.insert(0, str(ROOT / "scripts"))

# 复用 factor_attribution 的数据加载逻辑
from factor_attribution import (
    load_results, load_latest_snapshot_map, slug_of_snapshot
)

# extract_factors 从 signal_validator 复用
sys.path.insert(0, str(ROOT / "scripts"))
from signal_validator import extract_factors  # type: ignore

def sign(x):
    return 1 if x > 0 else (-1 if x < 0 else 0)

def model_dir(s0_sum, sb_val, weight, is_pm_same, pm_up):
    """模拟 final S0 含 PM 行, 然后方向判定
    PM 行: S0 与 PM 同向 → 0; 逆共识梯度 → -1~-3 (此处简化只看是否翻向)
    """
    s0 = s0_sum + sb_val * weight
    # 简化: PM 同向不加分, 逆共识 PM 80% 加 -1 (60-70 加 -1, 70-80 加 -2, ≥80 加 -3)
    if not is_pm_same and pm_up is not None:
        if pm_up >= 0.80:
            s0 -= 3
        elif pm_up >= 0.70:
            s0 -= 2
        elif pm_up >= 0.60:
            s0 -= 1
    if s0 > 0:
        return "UP"
    if s0 < 0:
        return "DOWN"
    return None  # 0 分时由其他逻辑决定, 这里不计

def main():
    results = load_results()
    snap_map = load_latest_snapshot_map()

    # 数据桶
    weights = [0, 1, 2, 3]
    # per-weight: 全样本 + 触发子集 + 未触发子集 的方向正确率
    overall = {w: [0, 0] for w in weights}   # [correct, total]
    triggered = {w: [0, 0] for w in weights}
    untriggered = {w: [0, 0] for w in weights}
    flip_cases = defaultdict(list)            # weight_A vs weight_B 翻向
    pnl_impact = {w: [] for w in weights}    # 触发时 (sb * w) 增量贡献 vs actual 方向

    sb_triggered_slugs = []
    joined = skipped = 0
    sb_dist = {"+1": 0, "-1": 0, "0": 0}

    for slug, r in results.items():
        if slug not in snap_map:
            skipped += 1
            continue
        actual = r["actual"]
        if actual not in ("UP", "DOWN"):
            continue
        actual_sign = 1 if actual == "UP" else -1
        try:
            md = json.loads(snap_map[slug].read_text())
            factors, inp, notes, mode = extract_factors(md)
        except Exception as e:
            skipped += 1
            continue
        joined += 1
        sb_val = float(factors.get("short_bounce_factor", 0.0))
        sb_dist["+1" if sb_val > 0 else ("-1" if sb_val < 0 else "0")] += 1

        # S0 不含 short_bounce (从 factors 求和, 但减掉 sb_val * 1)
        s0_minus_sb = sum(factors.values()) - sb_val * 1

        # PM 共识判定 (粗略): 取 polymarket.up_price 与 down_price
        pm = md.get("polymarket") or {}
        pm_up = pm.get("up_price")
        pm_down = pm.get("down_price")
        is_pm_up_strong = (pm_up is not None and pm_up >= 0.60)
        is_pm_down_strong = (pm_down is not None and pm_down >= 0.60)
        is_pm_same = (s0_minus_sb > 0 and is_pm_up_strong) or (s0_minus_sb < 0 and is_pm_down_strong)

        # 各权重下的方向
        for w in weights:
            d = model_dir(s0_minus_sb, sb_val, w, is_pm_same, pm_up)
            overall[w][1] += 1
            if d == actual:
                overall[w][0] += 1
            if sb_val != 0:
                triggered[w][1] += 1
                if d == actual:
                    triggered[w][0] += 1
                pnl_impact[w].append((sb_val * w) * actual_sign)
            else:
                untriggered[w][1] += 1
                if d == actual:
                    untriggered[w][0] += 1

        # 翻向检测 (权重 A vs B)
        if sb_val != 0:
            d_x1 = model_dir(s0_minus_sb, sb_val, 1, is_pm_same, pm_up)
            d_x2 = model_dir(s0_minus_sb, sb_val, 2, is_pm_same, pm_up)
            d_x0 = model_dir(s0_minus_sb, sb_val, 0, is_pm_same, pm_up)
            d_x3 = model_dir(s0_minus_sb, sb_val, 3, is_pm_same, pm_up)
            sb_triggered_slugs.append({
                "slug": slug,
                "actual": actual,
                "sb_val": sb_val,
                "s0_minus_sb": s0_minus_sb,
                "pm_up": pm_up,
                "d_x0": d_x0, "d_x1": d_x1, "d_x2": d_x2, "d_x3": d_x3
            })

    # === 输出报告 ===
    print("=" * 70)
    print(f"short_bounce_factor 权重扫荡测试 — round47 审计")
    print(f"数据: join {joined} / skipped {skipped}")
    print(f"short_bounce 触发分布: +1={sb_dist['+1']}场 / -1={sb_dist['-1']}场 / 0={sb_dist['0']}场")
    print(f"触发率: {(sb_dist['+1']+sb_dist['-1'])/joined*100:.1f}%")
    print("=" * 70)

    print("\n### 一、模型 S0 方向正确率(全样本)")
    print(f"{'权重':>6} {'正确/总数':>14} {'正确率':>8} {'vs ×1':>10}")
    base = overall[1][0]/overall[1][1]*100
    for w in weights:
        c, t = overall[w]
        pct = c/t*100 if t else 0
        delta = pct - base
        print(f"×{w:<4} {c:>5}/{t:<7} {pct:>7.2f}% {delta:>+9.2f}pt")

    print("\n### 二、模型 S0 方向正确率(short_bounce 触发子集 n={})".format(sb_dist['+1']+sb_dist['-1']))
    print(f"{'权重':>6} {'正确/总数':>14} {'正确率':>8} {'vs ×1':>10}")
    base = triggered[1][0]/triggered[1][1]*100
    for w in weights:
        c, t = triggered[w]
        pct = c/t*100 if t else 0
        delta = pct - base
        print(f"×{w:<4} {c:>5}/{t:<7} {pct:>7.2f}% {delta:>+9.2f}pt")

    print("\n### 三、模型 S0 方向正确率(short_bounce 未触发子集)")
    print(f"{'权重':>6} {'正确/总数':>14} {'正确率':>8}")
    for w in weights:
        c, t = untriggered[w]
        pct = c/t*100 if t else 0
        print(f"×{w:<4} {c:>5}/{t:<7} {pct:>7.2f}%")

    print("\n### 四、short_bounce 触发时,因子贡献 = sb*w 方向 vs actual 方向一致率")
    print("(纯因子方向 = short_bounce 因子的投票是否与实际方向吻合)")
    print(f"  sb=+1 时(actual=UP 一致率): {sb_dist['+1']} 场")
    print(f"  sb=-1 时(actual=DOWN 一致率): {sb_dist['-1']} 场")
    correct_pos = sum(1 for s in sb_triggered_slugs if s["sb_val"] > 0 and s["actual"] == "UP")
    correct_neg = sum(1 for s in sb_triggered_slugs if s["sb_val"] < 0 and s["actual"] == "DOWN")
    total_trig = len(sb_triggered_slugs)
    print(f"  short_bounce 方向命中数: {correct_pos + correct_neg} / {total_trig} = {(correct_pos+correct_neg)/total_trig*100:.1f}%")

    print("\n### 五、关键翻向案例(权重 ×1 → ×2 时 S0 变号)")
    flips = [s for s in sb_triggered_slugs if s["d_x1"] != s["d_x2"]]
    print(f"  翻向数: {len(flips)} / 触发 {total_trig} 场 ({len(flips)/total_trig*100:.1f}%)")
    if flips:
        # 翻向后方向正确率
        correct = sum(1 for s in flips if s["d_x2"] == s["actual"])
        print(f"  ×2 后方向正确: {correct}/{len(flips)} = {correct/len(flips)*100:.1f}%")
        # ×1 旧方向正确率
        correct_x1 = sum(1 for s in flips if s["d_x1"] == s["actual"])
        print(f"  ×1 旧方向正确: {correct_x1}/{len(flips)} = {correct_x1/len(flips)*100:.1f}%")

    flips3 = [s for s in sb_triggered_slugs if s["d_x1"] != s["d_x3"]]
    print(f"\n### 六、权重 ×1 → ×3 翻向案例")
    print(f"  翻向数: {len(flips3)} / 触发 {total_trig} 场 ({len(flips3)/total_trig*100:.1f}%)")
    if flips3:
        correct = sum(1 for s in flips3 if s["d_x3"] == s["actual"])
        correct_x1 = sum(1 for s in flips3 if s["d_x1"] == s["actual"])
        print(f"  ×3 后方向正确: {correct}/{len(flips3)} = {correct/len(flips3)*100:.1f}%")
        print(f"  ×1 旧方向正确: {correct_x1}/{len(flips3)} = {correct_x1/len(flips3)*100:.1f}%")

    flips_x0_x1 = [s for s in sb_triggered_slugs if s["d_x0"] != s["d_x1"]]
    print(f"\n### 七、权重 ×0 (废) → ×1 (现状) 翻向案例")
    print(f"  翻向数: {len(flips_x0_x1)} / 触发 {total_trig} 场 ({len(flips_x0_x1)/total_trig*100:.1f}%)")
    if flips_x0_x1:
        correct_x1 = sum(1 for s in flips_x0_x1 if s["d_x1"] == s["actual"])
        correct_x0 = sum(1 for s in flips_x0_x1 if s["d_x0"] == s["actual"])
        print(f"  ×1 后方向正确: {correct_x1}/{len(flips_x0_x1)} = {correct_x1/len(flips_x0_x1)*100:.1f}%")
        print(f"  ×0 (废) 方向正确: {correct_x0}/{len(flips_x0_x1)} = {correct_x0/len(flips_x0_x1)*100:.1f}%")

    print("\n### 八、结论摘要")
    print(f"  - 现状 ×1 全样本命中率: {overall[1][0]/overall[1][1]*100:.2f}%")
    print(f"  - 现状 ×1 触发子集命中率: {triggered[1][0]/triggered[1][1]*100:.2f}%")
    print(f"  - ×2 全样本: {overall[2][0]/overall[2][1]*100:.2f}% (Δ vs ×1: {(overall[2][0]/overall[2][1]-overall[1][0]/overall[1][1])*100:+.2f}pt)")
    print(f"  - ×0 全样本: {overall[0][0]/overall[0][1]*100:.2f}% (Δ vs ×1: {(overall[0][0]/overall[0][1]-overall[1][0]/overall[1][1])*100:+.2f}pt)")
    print(f"  - ×3 全样本: {overall[3][0]/overall[3][1]*100:.2f}% (Δ vs ×1: {(overall[3][0]/overall[3][1]-overall[1][0]/overall[1][1])*100:+.2f}pt)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
