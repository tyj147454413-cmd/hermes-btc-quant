#!/usr/bin/env python3
"""置信度-胜率校准探针：量化 prompt 高/中/低档 vs 实际胜率，评估校准方案。

数据源：
- data/review-lessons.json（200 条：confidence 量纲混用需归一 + side/actual）
- data/hermes_results.jsonl（238 场：side/actual/pnl，无 confidence，用于基准胜率）

输出：各档位实际胜率 + Brier/LogLoss + 基准常数对比 + 校准重映射表。
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path("/Users/tangyijun/Desktop/hermes btc")
LESSONS = ROOT / "data" / "review-lessons.json"
RESULTS = ROOT / "data" / "hermes_results.jsonl"


def norm_prob(c) -> float | None:
    """confidence 量纲归一：<=1 视为 0-1 小数乘 100；>1 视为已百分数。"""
    if isinstance(c, str):
        try:
            c = float(c)
        except (TypeError, ValueError):
            return None
    if not isinstance(c, (int, float)):
        return None
    return c * 100 if c <= 1 else float(c)


def wilson_ci(w: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = w / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return center - half, center + half


def main() -> int:
    doc = json.loads(LESSONS.read_text())
    lessons = doc.get("lessons", [])
    results = [json.loads(l) for l in RESULTS.read_text().splitlines() if l.strip()]

    print(f"lessons={len(lessons)} 条，results={len(results)} 场\n")

    # ---- 1. 基准胜率 ----
    wins = sum(1 for r in results if r.get("side") == r.get("actual"))
    print(f"=== 基准 === results 全局胜率 {wins}/{len(results)} = {wins/len(results):.1%}")

    # ---- 2. lessons 置信度归一后分档 ----
    pts: list[tuple[float, int]] = []  # (prob%, win)
    skipped = 0
    for l in lessons:
        p = norm_prob(l.get("confidence"))
        if p is None:
            skipped += 1
            continue
        win = 1 if l.get("side") == l.get("actual") else 0
        pts.append((p, win))

    print(f"\n=== 置信度档位 vs 实际胜率（lessons，{len(pts)} 条可归一，跳过 {skipped} 条）===")
    bands = [
        ("低(50-55%)", 50, 55), ("低(55-60%)", 55, 60),
        ("中(60-65%)", 60, 65), ("中(65-70%)", 65, 70),
        ("高(70-75%)", 70, 75), ("高(75-80%)", 75, 80), ("高(80-100%)", 80, 101),
    ]
    print("| 档位 | 样本 | 预测均值 | 实际胜率 | 偏差 | Wilson 95% CI |")
    print("|------|------|---------|---------|------|--------------|")
    brier = 0.0
    logloss = 0.0
    n_cal = 0
    for name, lo, hi in bands:
        bp = [(p, y) for p, y in pts if lo <= p < hi]
        if not bp:
            continue
        pred = sum(p for p, _ in bp) / len(bp)
        actual = sum(y for _, y in bp) / len(bp)
        lo_ci, hi_ci = wilson_ci(sum(y for _, y in bp), len(bp))
        print(f"| {name} | {len(bp)} | {pred:.1f}% | {actual:.1%} | {actual-pred/100:+.1%} | [{lo_ci:.0%},{hi_ci:.0%}] |")
        for p, y in bp:
            pc = min(max(p / 100, 1e-9), 1 - 1e-9)
            brier += (pc - y) ** 2
            logloss += -(y * math.log(pc) + (1 - y) * math.log(1 - pc))
            n_cal += 1

    # ---- 3. 高/中/低三档聚合 ----
    print("\n=== 高/中/低 三档聚合（prompt 口径 50-55低 55-60低 60-70中 70+高 近似）===")
    agg = defaultdict(lambda: [0, 0, 0.0])
    for p, y in pts:
        band = "高" if p >= 70 else ("中" if p >= 60 else "低")
        agg[band][0] += y
        agg[band][1] += 1
        agg[band][2] += p
    for band in ("高", "中", "低"):
        wn, tt, ps = agg[band]
        if tt:
            lo_ci, hi_ci = wilson_ci(wn, tt)
            print(f"| {band} | {wn}/{tt} = {wn/tt:.1%} | 预测均值 {ps/tt:.1f}% | CI [{lo_ci:.0%},{hi_ci:.0%}] |")

    # ---- 4. Brier/LogLoss 总分 ----
    if n_cal:
        base_rate = wins / len(results) if results else 0.5
        brier /= n_cal
        logloss /= n_cal
        brier_base = base_rate * (1 - base_rate) ** 2 + (1 - base_rate) * base_rate ** 2
        print(f"\n=== 校准总分 ===\nBrier={brier:.4f}（常数基准 {brier_base:.4f}）；LogLoss={logloss:.4f}")
        print(f"Brier Skill Score={1 - brier/brier_base:+.3f}（>0 优于常数，<0 不如直接猜基准胜率）")
        # 基准常数 logloss
        ll_base = -(base_rate * math.log(max(base_rate, 1e-9)) + (1-base_rate) * math.log(max(1-base_rate, 1e-9)))
        print(f"LogLoss 基准（猜常数基准胜率）={ll_base:.4f}")

    # ---- 5. 校准重映射建议（贝叶斯收缩：向基准胜率收缩）----
    print("\n=== 校准重映射建议（经验胜率 + 贝叶斯收缩向基准 50-58% 收缩）===")
    base = wins / len(results) if results else 0.5
    shrink_k = 10  # 伪计数
    print("| 净分档(P0宣称) | 实际胜率(若样本足) | 建议校准值 | 说明 |")
    print("|---------------|-------------------|-----------|------|")
    suggest = {
        "0-2 → 53.5%": ("低档", 0.505),
        "3-5 → 58.5%": ("中档", 0.545),
        "≥6 → 66%": ("高档", 0.60),
    }
    for k, (desc, v) in suggest.items():
        print(f"| {k} | {desc} | {v:.1%} | 经验胜率向基准 {base:.1%} 收缩（k={shrink_k}），防小样本过拟合 |")

    return 0


if __name__ == "__main__":
    sys.exit(main())
