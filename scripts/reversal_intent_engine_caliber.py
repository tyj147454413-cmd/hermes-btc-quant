# -*- coding: utf-8 -*-
"""reversal_intent 引擎口径验证（2026-09-03 用户拍板 ±1 前的最后一步）。

口径修正说明：回测脚本用的是 5min 快照 Δ（57.4% n=54），但采集器 up_price_delta 是
小时级（本时刻 vs 上小时末快照，hermes_collect.py L1077）。为避免「研究口径与引擎口径
脱节」（round52 taker_bs_15m 教训），本脚本按引擎真实口径重验：

  Δ_cur  = P(cur, :40)  − P(prev_hour, :55)   ≈ 最近 45min 净变动
  Δ_prev = P(prev_hour, :55) − P(prev2_hour, :55)  ≈ 上小时净变动
  深V    = |Δ_prev|≥0.03 且 Δ_cur 与 Δ_prev 变号 且 |Δ_cur|≥0.02 → 方向=Δ_cur

（引擎 :40 触发读快照；若 :40 缺失退 :35/:45 邻近档）
ground truth = 该场 actual。只读 data/，报告落 data/reversal_intent_engine_caliber_20260903.md
"""
import json
from collections import defaultdict
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
REPORT = DATA / "reversal_intent_engine_caliber_20260903.md"
SIG_MIN_CANDIDATES = (40, 35, 45)


def load_pm_series():
    """slug -> {minute: up_cur}，slug 按名排序即时间序（slug 含月日时分）"""
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
    slugs = sorted(s for s in pm if s in res)
    deepv, nonv, allv = [], [], []
    detail_dir = defaultdict(list)

    for i, slug in enumerate(slugs):
        if i < 2:
            continue
        prev2_slug, prev_slug = slugs[i - 2], slugs[i - 1]
        prev2_55 = pm[prev2_slug].get(55, pm[prev2_slug].get(50))
        prev_55 = pm[prev_slug].get(55, pm[prev_slug].get(50))
        cur_prices = pm[slug]
        cur = next((cur_prices[m] for m in SIG_MIN_CANDIDATES if m in cur_prices), None)
        if None in (prev2_55, prev_55, cur) or 0 in (prev2_55, prev_55, cur):
            continue
        d_cur = cur - prev_55
        d_prev = prev_55 - prev2_55
        if abs(d_cur) < 0.02:  # 现行位移口径触发门槛
            continue
        direction = "UP" if d_cur > 0 else "DOWN"
        price = cur if direction == "UP" else 1 - cur
        won = direction == res[slug]
        allv.append((won, price))
        if abs(d_prev) >= 0.03 and d_prev * d_cur < 0:
            deepv.append((won, price))
            detail_dir[f"反{direction}"].append((won, price))
        else:
            nonv.append((won, price))

    lines = ["# reversal_intent 引擎口径验证（2026-09-03）", "",
             "- 口径：Δ_cur=P(:40)−P(上小时:55)，Δ_prev=上小时:55−上上小时:55（采集器真实口径）",
             f"- 样本：{len(allv)} 场位移可判（总 {len(slugs)} 场已结算）", "",
             "## 引擎口径核心对比", "", "| 分组 | n | 胜率 | EV/每$1 |", "|---|---|---|---|",
             fmt("全体位移口径(现行±2基础)", allv),
             fmt("深V子集(拟+±1)", deepv),
             fmt("非深V位移子集(维持±2)", nonv)]

    if deepv and nonv:
        wr_dv = sum(1 for w, _ in deepv if w) / len(deepv)
        wr_nv = sum(1 for w, _ in nonv if w) / len(nonv)
        wr_al = sum(1 for w, _ in allv if w) / len(allv)
        verdict = ("强子集，+±1 有增量 ✅" if wr_dv > wr_nv + 0.02 else
                   "弱子集，加码降胜率 ❌" if wr_dv < wr_nv - 0.02 else "相当 ⚠️")
        lines += ["", f"**判定**：深V {wr_dv*100:.1f}% vs 非深V {wr_nv*100:.1f}% vs 全体 {wr_al*100:.1f}% → {verdict}"]

    if detail_dir:
        lines += ["", "## 方向分桶", "", "| 方向 | n | 胜率 |", "|---|---|---|"]
        for d in sorted(detail_dir):
            lines.append(fmt(f"反{d[1:]}", detail_dir[d]))

    report = "\n".join(lines) + "\n"
    REPORT.write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
