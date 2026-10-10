#!/usr/bin/env python3
"""置信度校准方案验证：新旧概率映射 Brier/LogLoss/BSS 对比。

思路：
- 用 lessons 200 条（confidence 归一化 + side/actual）作为校准集。
- 旧方案：直接用 AI confidence 概率作预测。
- 新方案 A（分档校准）：按档位映射到"贝叶斯收缩后的经验胜率"。
- 新方案 B（等渗近似+收缩）：相邻档平滑。
- 对比 Brier/LogLoss/BSS，判断校准是否真正改善。

防过拟合：K 折交叉验证（随机分 5 折，每折用其余 4 折拟合分档胜率）。
"""
from __future__ import annotations

import json
import math
import random
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path("/Users/tangyijun/Desktop/hermes btc")
LESSONS = ROOT / "data" / "review-lessons.json"
RESULTS = ROOT / "data" / "hermes_results.jsonl"

RNG = random.Random(42)


def norm_prob(c) -> float | None:
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


def band_of(p: float) -> str:
    if p < 55:
        return "a_50_55"
    if p < 60:
        return "b_55_60"
    if p < 65:
        return "c_60_65"
    if p < 70:
        return "d_65_70"
    if p < 75:
        return "e_70_75"
    if p < 80:
        return "f_75_80"
    return "g_80_100"


BANDS = ["a_50_55", "b_55_60", "c_60_65", "d_65_70", "e_70_75", "f_75_80", "g_80_100"]


def fit_band_rates(pts: list[tuple[float, int]], base_rate: float, shrink_k: float = 12) -> dict[str, float]:
    """分档拟合经验胜率 + 贝叶斯收缩向基准。返回 band -> 收缩后胜率。"""
    agg = defaultdict(lambda: [0, 0])
    for p, y in pts:
        agg[band_of(p)][0] += y
        agg[band_of(p)][1] += 1
    rates: dict[str, float] = {}
    for b in BANDS:
        wn, nn = agg[b]
        # 贝叶斯收缩：向 base_rate 收缩，伪计数 shrink_k
        rate = (wn + shrink_k * base_rate) / (nn + shrink_k)
        rates[b] = rate
    return rates


def brier_logloss(preds: list[tuple[float, int]]) -> tuple[float, float, int]:
    brier = 0.0
    ll = 0.0
    n = 0
    for p, y in preds:
        pc = min(max(p, 1e-9), 1 - 1e-9)
        brier += (pc - y) ** 2
        ll += -(y * math.log(pc) + (1 - y) * math.log(1 - pc))
        n += 1
    return brier / n, ll / n, n


def main() -> int:
    doc = json.loads(LESSONS.read_text())
    lessons = doc.get("lessons", [])
    results = [json.loads(l) for l in RESULTS.read_text().splitlines() if l.strip()]

    base_rate = sum(1 for r in results if r.get("side") == r.get("actual")) / len(results)

    pts = []
    for l in lessons:
        p = norm_prob(l.get("confidence"))
        if p is None:
            continue
        y = 1 if l.get("side") == l.get("actual") else 0
        pts.append((p, y))
    print(f"可校准样本：{len(pts)} 条（lessons {len(lessons)}，跳过 {len(lessons)-len(pts)}）")
    print(f"基准胜率：{base_rate:.1%}（results {len(results)} 场）\n")

    # 打乱做 5 折
    RNG.shuffle(pts)
    k = 5
    folds = [pts[i::k] for i in range(k)]

    # ---- 旧方案：AI confidence 直接作概率 ----
    old_preds = [(p / 100, y) for p, y in pts]
    b, ll, n = brier_logloss(old_preds)
    brier_base = base_rate * (1 - base_rate) ** 2 + (1 - base_rate) * base_rate ** 2
    ll_base = -(base_rate * math.log(max(base_rate, 1e-9)) + (1 - base_rate) * math.log(max(1 - base_rate, 1e-9)))
    print(f"=== 旧方案（AI confidence 直出）===  Brier={b:.4f} BSS={1-b/brier_base:+.3f} LogLoss={ll:.4f}")

    # ---- 新方案 A：分档校准（5 折交叉验证）----
    cv_a_preds: list[tuple[float, int]] = []
    cv_a_bands: list[tuple[str, int]] = []
    for fold_i, test in enumerate(folds):
        train = [p for j, p in enumerate(pts) if j // k != fold_i]  # 不对，用 idx 过滤
    # 重新正确切分
    cv_a_preds = []
    for i in range(k):
        test = folds[i]
        train = [p for j in range(k) if j != i for p in folds[j]]
        rates = fit_band_rates(train, base_rate)
        for p, y in test:
            cv_a_preds.append((rates[band_of(p)], y))
    b_a, ll_a, n_a = brier_logloss(cv_a_preds)
    print(f"=== 新方案 A（分档校准 + 收缩 k=12，5折CV）===  Brier={b_a:.4f} BSS={1-b_a/brier_base:+.3f} LogLoss={ll_a:.4f}")

    # ---- 新方案 B：等渗近似（累计加权平滑，PAVA 简化版）----
    # 用全样本拟合，5 折 CV 评估（每折用其余 4 折的累计胜率做单调约束）
    def fit_iso(train: list[tuple[float, int]], base_rate: float) -> dict[str, float]:
        agg = defaultdict(lambda: [0, 0, 0.0])
        for p, y in train:
            agg[band_of(p)][0] += y
            agg[band_of(p)][1] += 1
            agg[band_of(p)][2] += p
        # 按 band 顺序做累计均值（保序近似）
        rates = {}
        cum_w = cum_n = 0
        seq = []
        for b in BANDS:
            wn, nn, _ = agg[b]
            rates[b] = (wn + base_rate * 12) / (nn + 12)  # 先收缩
            seq.append((b, rates[b]))
        # PAVA 简化：若后档 < 前档，取平均
        changed = True
        while changed:
            changed = False
            i = 0
            while i < len(seq) - 1:
                if seq[i][1] > seq[i + 1][1]:
                    # 合并相邻两档
                    w0 = agg[seq[i][0]][0] + agg[seq[i + 1][0]][0]
                    n0 = agg[seq[i][0]][1] + agg[seq[i + 1][0]][1]
                    val = (w0 + 12 * base_rate) / (n0 + 12)
                    seq[i] = (seq[i][0], val)
                    seq[i + 1] = (seq[i + 1][0], val)
                    changed = True
                i += 1
        for b, v in seq:
            rates[b] = v
        return rates

    cv_b_preds = []
    for i in range(k):
        test = folds[i]
        train = [p for j in range(k) if j != i for p in folds[j]]
        rates = fit_iso(train, base_rate)
        for p, y in test:
            cv_b_preds.append((rates[band_of(p)], y))
    b_b, ll_b, n_b = brier_logloss(cv_b_preds)
    print(f"=== 新方案 B（分档校准 + 保序收缩，5折CV）===  Brier={b_b:.4f} BSS={1-b_b/brier_base:+.3f} LogLoss={ll_b:.4f}")

    # ---- 结论 ----
    print("\n=== 对比结论 ===")
    for name, (bb, bss_, ll_) in [("旧方案", (b, 1 - b / brier_base, ll)),
                                   ("新A", (b_a, 1 - b_a / brier_base, ll_a)),
                                   ("新B", (b_b, 1 - b_b / brier_base, ll_b))]:
        print(f"{name}: Brier={bb:.4f} BSS={bss_:+.3f} LogLoss={ll_:.4f}")
    print(f"\n基准常数: Brier={brier_base:.4f} LogLoss={ll_base:.4f}")

    # ---- 输出最终重映射表（全样本拟合，供 prompt/rule_engine 落地）----
    print("\n=== 最终重映射表（全样本拟合 + 收缩 k=12，供落地）===")
    rates = fit_band_rates(pts, base_rate)
    rates_iso = fit_iso(pts, base_rate)
    print("| 置信度档 | 样本 | 实际胜率 | 收缩后(直接) | 保序后(PAVA) | 建议采用 |")
    print("|---------|------|---------|-------------|-------------|---------|")
    agg = defaultdict(lambda: [0, 0])
    for p, y in pts:
        agg[band_of(p)][0] += y
        agg[band_of(p)][1] += 1
    for b in BANDS:
        wn, nn = agg[b]
        raw = wn / nn if nn else 0
        if nn == 0:
            continue
        lo_ci, hi_ci = wilson_ci(wn, nn)
        adopt = rates_iso[b]
        print(f"| {b} | {nn} | {raw:.1%} (CI[{lo_ci:.0%},{hi_ci:.0%}]) | {rates[b]:.1%} | {rates_iso[b]:.1%} | {adopt:.1%} |")

    return 0


if __name__ == "__main__":
    sys.exit(main())
