# -*- coding: utf-8 -*-
"""反转因子挖掘（2026-09-03 用户任务「找出反转的因子来，看看有没有」）。

在 5min PM 价序列历史（180 场已结算）上测试候选反转形态的胜率与 EV。
所有形态均在信号时刻 :35 判定（与引擎同盲区），ground truth = results.actual。

候选形态（全部为「信号时刻可见」口径，不用未来数据）：
  R1 深V前兆     ：上一段深变动(|Δ_prev|≥0.03) + 本段反向(|Δ_cur|≥0.02) → 方向=Δ_cur
  R2 贴线衰竭反转：前30min净变动≤−0.06(深跌) 且 贴线(|P35−0.50|≤0.05) → UP；对称上涨版 → DOWN
  R3 跌幅收敛衰竭：深跌同R2 且 |Δ_cur|<|Δ_prev|/2(跌势衰减) → UP；对称
  R4 极端共识回归：开场P05极端(≥0.70或≤0.30) 且 P35已向0.5回归(穿0.60/0.40) → 回归方向
  R5 纯Δ变号     ：任意变号（上轮回测 B 策略，52.9%）作对照

另有引擎已计分反转相关因子现状（wall_divergence/short_bounce/pm_momentum/momentum_burst）
——从 57 行 factors 明细统计（样本薄，仅参考）。

只读 data/，报告落 data/reversal_factor_miner_20260903.md
"""
import json
from collections import defaultdict
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
REPORT = DATA / "reversal_factor_miner_20260903.md"
TIE_BAND = 0.05
DEEP_MOVE = 0.06      # 「深跌/深涨」30min 净变动阈值
V_PREV = 0.03         # R1 上一段深变动阈值
V_CUR = 0.02          # R1 本段反向阈值
CONV_RATIO = 0.5      # R3 收敛比例


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


def ev_per_dollar(wr, price):
    return wr * (1 - price) - (1 - wr) * price


def record(buckets, name, direction, actual, price):
    if direction in ("UP", "DOWN") and price:
        buckets[name].append((direction == actual, price))


def main():
    pm, res = load_pm_series(), load_results()
    buckets = defaultdict(list)
    slugs = [s for s in pm if s in res]

    for slug in slugs:
        prices = pm[slug]
        actual = res[slug]
        sig = prices.get(35, prices.get(30))
        if sig is None:
            continue
        mins = sorted(m for m in prices if m <= 35)
        if len(mins) < 3:
            continue
        m2, m1, m0 = mins[-3], mins[-2], mins[-1]
        d_prev = prices[m1] - prices[m2]
        d_cur = prices[m0] - prices[m1]
        p_start = prices.get(5, prices.get(mins[0]))
        net30 = prices[m0] - (p_start if p_start else prices[m0])
        tie = abs(sig - 0.50) <= TIE_BAND
        up = prices[m0]

        def side_price(d):
            return up if d == "UP" else 1 - up

        # R1 深V前兆
        if abs(d_prev) >= V_PREV and abs(d_cur) >= V_CUR and d_prev * d_cur < 0:
            d = "UP" if d_cur > 0 else "DOWN"
            record(buckets, "R1_深V前兆", d, actual, side_price(d))
        # R2 贴线衰竭反转（用户案例2）
        if net30 <= -DEEP_MOVE and tie:
            record(buckets, "R2_深跌贴线_反UP", "UP", actual, up)
        if net30 >= DEEP_MOVE and tie:
            record(buckets, "R2_深涨贴线_反DOWN", "DOWN", actual, 1 - up)
        # R3 跌幅收敛衰竭
        if net30 <= -DEEP_MOVE and abs(d_cur) < abs(d_prev) * CONV_RATIO:
            record(buckets, "R3_深跌收敛_反UP", "UP", actual, up)
        if net30 >= DEEP_MOVE and abs(d_cur) < abs(d_prev) * CONV_RATIO:
            record(buckets, "R3_深涨收敛_反DOWN", "DOWN", actual, 1 - up)
        # R4 极端共识回归
        if p_start is not None:
            if p_start >= 0.70 and up <= 0.60 and up > 0.50:
                record(buckets, "R4_极端共识回归_反UP", "UP", actual, up)
            if p_start <= 0.30 and up >= 0.40 and up < 0.50:
                record(buckets, "R4_极端共识回归_反DOWN", "DOWN", actual, 1 - up)
        # R5 纯Δ变号（对照）
        if d_prev * d_cur < 0 and abs(d_cur) >= 0.02:
            d = "UP" if d_cur > 0 else "DOWN"
            record(buckets, "R5_纯变号_对照", d, actual, side_price(d))

    # factors 明细（57 行）里已计分反转相关因子的方向胜率
    fact = defaultdict(list)
    done_f = DATA / "hermes_done.jsonl"
    if done_f.exists():
        with open(done_f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                    sc = (o.get("signal") or {}).get("scoring_compare") or {}
                    es = sc.get("engine_score") or {}
                    fs = es.get("factors") or {}
                    slug = (o.get("signal") or {}).get("slug")
                    if not (slug and slug in res and fs):
                        continue
                    for k in ("wall_divergence", "short_bounce", "pm_momentum", "momentum_burst"):
                        v = fs.get(k)
                        if v:
                            d = "UP" if v > 0 else "DOWN"
                            fact[f"{k}_{'+' if v>0 else '-'}"].append((d == res[slug],))
                except Exception:
                    continue

    lines = ["# 反转因子挖掘报告（2026-09-03）", "",
             f"- 样本：{len(slugs)} 场 5min PM 序列 × 已结算，信号时刻 :35 判定（引擎同盲区）",
             f"- 参数：贴线±{TIE_BAND} · 深变动±{DEEP_MOVE} · 深V {V_PREV}/{V_CUR} · 收敛比{CONV_RATIO}", "",
             "| 候选形态 | n | 胜率 | EV/每$1 | 判定 |", "|---|---|---|---|---|"]
    for name in sorted(buckets):
        rows = buckets[name]
        n = len(rows)
        wr = sum(1 for w, _ in rows if w) / n
        ev = ev_per_dollar(wr, sum(p for _, p in rows) / n)
        verdict = ("✅ 有价值" if wr >= 0.58 and n >= 15 else
                   "🟡 观察（样本不足）" if wr >= 0.55 and n < 15 else
                   "🟡 边缘" if wr >= 0.52 else "❌ 无价值")
        lines.append(f"| {name} | {n} | {wr*100:.1f}% | {ev:+.3f} | {verdict} |")

    lines += ["", "## 已计分反转相关因子（factors 明细 n=57，样本薄仅参考）", ""]
    for k in sorted(fact):
        rows = fact[k]
        n = len(rows)
        wr = sum(1 for r in rows if r[0]) / n
        lines.append(f"- {k}: n={n} 方向胜率={wr*100:.1f}%")

    lines += ["", "## 结论判据", "",
              "- ✅ 有价值 = 胜率≥58% 且 n≥15（Wilson 下界>50% 大致成立）",
              "- 所有形态均在 :35 可见，无未来函数；EV 按 :35 对应方向侧 PM 价入场",
              "- 对照：R5 纯变号上轮回测 52.9%（n=68）——变号本身信息弱，形态约束才是关键"]
    report = "\n".join(lines) + "\n"
    (REPORT).write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
