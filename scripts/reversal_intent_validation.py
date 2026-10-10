# -*- coding: utf-8 -*-
"""reversal_intent ±2 权重落地前验证（2026-09-03 用户指令「先回测验证有没有效果，权重±2」）。

核心问题：深V 形态（|Δ_prev|≥0.03 且 Δ_cur 反向 ≥0.02）场次里，按 Δ_cur 方向
（= 现行 pm_momentum 位移口径方向）的胜率，是否值得在现行 ±2 之上再 ±2。

验证维度：
  V1 深V子集 vs 非深V位移子集 vs 全体位移口径 —— 增量方向对不对
  V2 阈值参数扫描 (prev_thr, cur_thr) 4 组 —— 形态是否稳健（防过拟合）
  V3 时间分段（前半/后半样本）—— 防时间集中偏差（round48 pm_imbalance 教训）
  V4 方向分桶（反UP/反DOWN）
  V5 EV 对比：现行±2 vs 现行±2再+reversal±2 的假设组合影响

只读 data/，报告落 data/reversal_intent_validation_20260903.md
"""
import json
from collections import defaultdict
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
REPORT = DATA / "reversal_intent_validation_20260903.md"
TIE_BAND = 0.05


def load_pm_series():
    series = defaultdict(dict)
    for f in sorted(DATA.glob("pm_prices/pm_2026*.jsonl")):
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                slug, m = r.get("slug_cur"), r.get("slot_minute")
                if slug and m is not None:
                    series[slug][int(m)] = float(r.get("up_cur") or 0)
    return series


def load_results():
    out = {}
    with open(DATA / "hermes_results.jsonl", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("slug") and r.get("actual") in ("UP", "DOWN"):
                out[r["slug"]] = r["actual"]
    return out


def ev(wr, p):
    return wr * (1 - p) - (1 - wr) * p


def fmt(name, rows):
    n = len(rows)
    if n == 0:
        return f"| {name} | 0 | — | — |"
    wr = sum(1 for w, _ in rows if w) / n
    p_avg = sum(p for _, p in rows) / n
    return f"| {name} | {n} | {wr*100:.1f}% | {ev(wr, p_avg):+.3f} |"


def main():
    pm, res = load_pm_series(), load_results()
    slugs = [s for s in pm if s in res]
    detail = []  # (slug, ts_idx, is_deepv, direction, won, price)

    for slug in slugs:
        prices = pm[slug]
        actual = res[slug]
        mins = sorted(m for m in prices if m <= 35)
        if len(mins) < 3:
            continue
        m2, m1, m0 = mins[-3], mins[-2], mins[-1]
        d_prev = prices[m1] - prices[m2]
        d_cur = prices[m0] - prices[m1]
        if abs(d_cur) < 0.02:  # 现行位移口径触发门槛
            continue
        direction = "UP" if d_cur > 0 else "DOWN"
        price = prices[m0] if direction == "UP" else 1 - prices[m0]
        deepv = abs(d_prev) >= 0.03 and d_prev * d_cur < 0
        won = direction == actual
        ts_order = sorted(slugs).index(slug)  # slug 字典序≈时间序（slug 含日期）
        detail.append((slug, ts_order, deepv, direction, won, price))

    lines = ["# reversal_intent ±2 落地前验证报告（2026-09-03）", ""]

    # V1 核心对比
    all_disp = [(w, p) for _, _, _, _, w, p in detail]
    deepv_rows = [(w, p) for _, _, dv, _, w, p in detail if dv]
    non_rows = [(w, p) for _, _, dv, _, w, p in detail if not dv]
    lines += ["## V1 核心：深V是现行口径的强子集还是弱子集？", "",
              "| 分组 | n | 胜率 | EV/每$1 |", "|---|---|---|---|",
              fmt("全体位移口径(现行±2基础)", all_disp),
              fmt("深V子集(拟再+±2)", deepv_rows),
              fmt("非深V位移子集(维持±2)", non_rows)]

    if deepv_rows:
        dv_wr = sum(1 for w, _ in deepv_rows if w) / len(deepv_rows)
        non_wr = sum(1 for w, _ in non_rows if w) / len(non_rows) if non_rows else 0
        lines += ["", f"**判定**：深V子集胜率 {dv_wr*100:.1f}% vs 非深V {non_wr*100:.1f}% —— "
                  + ("深V 是**强**子集，加码有增量价值 ✅" if dv_wr > non_wr + 0.02 else
                     "深V 是**弱**子集，±2 加码 = 给表现更差的场次加倍仓位 ❌" if dv_wr < non_wr - 0.02 else
                     "两组相当，加码无增量 ⚠️")]

    # V2 参数扫描
    lines += ["", "## V2 阈值参数扫描（形态稳健性）", "",
              "| prev_thr | cur_thr | n | 胜率 |", "|---|---|---|---|"]
    for pt, ct in ((0.02, 0.015), (0.03, 0.02), (0.04, 0.025), (0.05, 0.03)):
        rows = []
        for slug in slugs:
            prices = pm[slug]
            actual = res[slug]
            mins = sorted(m for m in prices if m <= 35)
            if len(mins) < 3:
                continue
            m2, m1, m0 = mins[-3], mins[-2], mins[-1]
            dp, dc = prices[m1] - prices[m2], prices[m0] - prices[m1]
            if abs(dp) >= pt and dp * dc < 0 and abs(dc) >= ct:
                d = "UP" if dc > 0 else "DOWN"
                rows.append((d == actual, prices[m0] if d == "UP" else 1 - prices[m0]))
        lines.append(fmt(f"{pt}", rows).replace("| " + f"{pt}", f"| {pt}"))

    # V3 时间分段
    if deepv_rows:
        lines += ["", "## V3 时间分段（防时间集中）", "",
                  "| 段 | n | 胜率 |", "|---|---|---|"]
        n_half = len(slugs) // 2
        for tag, lo, hi in (("前半", 0, n_half), ("后半", n_half, len(slugs))):
            rows = [(w, p) for s, i, dv, _, w, p in detail
                    if dv and lo <= i < hi]
            lines.append(fmt(tag, rows))

    # V4 方向分桶
    if deepv_rows:
        lines += ["", "## V4 方向分桶", "", "| 方向 | n | 胜率 |", "|---|---|---|"]
        for d in ("UP", "DOWN"):
            rows = [(w, p) for s, i, dv, dd, w, p in detail if dv and dd == d]
            lines.append(fmt(f"反转→{d}", rows))

    # V5 组合影响估算
    if deepv_rows and all_disp:
        lines += ["", "## V5 ±2 加码的 EV 影响（每$1 现行仓位口径）", ""]
        wr_all = sum(1 for w, _ in all_disp if w) / len(all_disp)
        wr_dv = sum(1 for w, _ in deepv_rows if w) / len(deepv_rows)
        p_all = sum(p for _, p in all_disp) / len(all_disp)
        p_dv = sum(p for _, p in deepv_rows) / len(deepv_rows)
        base = ev(wr_all, p_all)
        # 深V场额外 ±2 → 该场仓位翻倍，组合 EV = 基础EV + 深V超额EV×(深V占比)
        share = len(deepv_rows) / len(all_disp)
        extra = (ev(wr_dv, p_dv) - ev(wr_all, p_all)) * share
        lines.append(f"- 现行组合 EV ≈ {base:+.4f}/每$1")
        lines.append(f"- ±2 加码后 ≈ {base + extra:+.4f}/每$1（深V场占比 {share*100:.0f}%，"
                     f"增量 {(extra)*100:+.2f}pt）")

    report = "\n".join(lines) + "\n"
    REPORT.write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
