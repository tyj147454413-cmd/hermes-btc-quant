#!/usr/bin/env python3
"""
图表分析.md v2.0 规则引擎回测（基于特征有效性回测修正，与 v2.0 框架对齐）

核心（v2.0）：
- 移除连庄硬编码（回测证伪顺势 32%），百家乐改为 AI 综合参考
- 量化信号决定方向：订单流顺势(71.4%) / 历史均值回归(75%) / prev反转(59.3%) / 散户反指(55.6%)
- 德州扑克读庄：OI 验证价值 vs 诈唬（taker + oi 组合）
- "信号一致才下注"：score>=1.5 下注(75%) / score>=2 下注(80%) / score<1.5 不下注

纯本地，无网络。不改 bot 不下单。
"""
# pyright: reportAny=false

import json
import math
import sys
from datetime import datetime
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
DATA_DIR = THIS_DIR.parent / "data"
SNAP_DIR = DATA_DIR / "snapshots"
RESULTS = DATA_DIR / "hermes_results.jsonl"


def load_jsonl(path):
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def wilson_ci(w, n, z=1.96):
    if n == 0:
        return 0.0, 0.0
    p = w / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return center - half, center + half


def _s(v, d=0.0):
    try:
        return float(v) if v is not None else d
    except (TypeError, ValueError):
        return d


def extract(r):
    """提取 v2.0 量化特征（只用历史，不用当前小时）。"""
    return {
        "slug": r["slug"],
        "actual": r["actual"],
        "prev_side": r["prev_side"],
        "of": _s((r.get("capital_flow") or {}).get("order_flow", 0)) if isinstance((r.get("capital_flow") or {}).get("order_flow"), dict) else _s(r.get("order_flow"), 0),
        "taker": _s(r.get("taker_bs_ratio"), 1.0),
        "oi": _s(r.get("oi_change_24h_pct"), 0.0),
        "lsr": _s(r.get("long_short_ratio"), 1.0),
        "up_rate": _s(r.get("up_rate"), 0.5),
    }


def judge_v2(f):
    """
    v2.0 量化规则引擎。返回 (方向, score, 明细)。
    score 为信号一致性强度，|score|>=1.5 才下注。
    """
    score = 0.0
    parts = []

    # 1. prev 方向反转
    rev = "DOWN" if f["prev_side"] == "UP" else "UP"
    score += 1.0 if rev == "UP" else -1.0
    parts.append(f"反转({f['prev_side']}→{rev})±1")

    # 2. 订单流顺势（最强）
    of = f["of"]
    if of < -0.1:
        score -= 1.0
        parts.append(f"订单流卖{of:.2f}-1")
    elif of > 0.1:
        score += 1.0
        parts.append(f"订单流买{of:.2f}+1")

    # 3. 历史时段均值回归
    if f["up_rate"] > 0.6:
        score -= 0.8
        parts.append(f"历史偏多{up_rate_str(f):.2f}-0.8")
    elif f["up_rate"] < 0.4:
        score += 0.8
        parts.append(f"历史偏空{up_rate_str(f):.2f}+0.8")

    # 4. 散户拥挤反指
    if f["lsr"] > 1.3:
        score -= 0.5
        parts.append(f"散户拥挤{lsr_str(f):.2f}-0.5")
    elif f["lsr"] < 0.8:
        score += 0.5
        parts.append(f"散户偏空{lsr_str(f):.2f}+0.5")

    # 5. 德州扑克读庄：OI 验证价值 vs 诈唬
    taker, oi = f["taker"], f["oi"]
    if taker >= 1.3 and oi > 0:
        score += 0.5
        parts.append(f"价值多头(taker{taker:.2f}+OI{oi:+.0f}%)+0.5")
    elif taker >= 1.3 and oi < 0:
        score -= 0.5
        parts.append(f"诈唬拉高(taker{taker:.2f}-OI{oi:+.0f}%)-0.5")
    elif taker <= 0.77 and oi > 0:
        score -= 0.5
        parts.append(f"真空头(taker{taker:.2f}+OI{oi:+.0f}%)-0.5")
    elif taker <= 0.77 and oi < 0:
        score += 0.5
        parts.append(f"砸盘洗盘(taker{taker:.2f}-OI{oi:+.0f}%)+0.5")

    direction = "UP" if score >= 0 else "DOWN"
    return direction, score, "; ".join(parts) if parts else "无"


def up_rate_str(f):
    return f["up_rate"]


def lsr_str(f):
    return f["lsr"]


def main():
    results = load_jsonl(RESULTS)
    actual_map = {r.get("slug", ""): r.get("actual") for r in results}

    rows = []
    for f in sorted(SNAP_DIR.glob("*.json")):
        if f.stat().st_size < 40000:
            continue
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        slug = d.get("slug", "")
        if slug not in actual_map:
            continue
        ph = d.get("prev_hour") or {}
        cf = d.get("capital_flow") or {}
        hist = d.get("historical") or {}
        of = (cf.get("order_flow") or {})
        of_val = of.get("imbalance_5m", 0) if isinstance(of, dict) else 0
        rows.append({
            "slug": slug, "actual": actual_map[slug],
            "prev_side": ph.get("side"),
            "order_flow": of_val,
            "taker_bs_ratio": _s(cf.get("taker_bs_ratio"), 1.0),
            "oi_change_24h_pct": _s(cf.get("oi_change_24h_pct"), 0.0),
            "long_short_ratio": _s(cf.get("long_short_ratio"), 1.0),
            "up_rate": _s(hist.get("up_rate"), 0.5),
        })

    n = len(rows)
    print(f"样本: {n}")

    # 押反转基准
    rev_w = sum(1 for r in rows if r["prev_side"] and (("DOWN" if r["prev_side"] == "UP" else "UP") == r["actual"]))
    lo, hi = wilson_ci(rev_w, n)
    print(f"[基准] 押prev反转: {rev_w}/{n} = {rev_w/n:.1%} [{lo:.1%},{hi:.1%}]")

    # 全量判断
    all_detail = []
    for r in rows:
        f = extract(r)
        pred, score, detail = judge_v2(f)
        w = 1 if pred == r["actual"] else 0
        all_detail.append((r["slug"], r["actual"], pred, w, score, detail))

    wins = sum(d[3] for d in all_detail)
    lo, hi = wilson_ci(wins, n)
    print(f"\n[v2全量] {wins}/{n} = {wins/n:.1%} [{lo:.1%},{hi:.1%}]")

    # "信号一致才下注"：按阈值
    print("\n[信号一致才下注]")
    for thresh, name in [(1.5, ">=1.5下注"), (2.0, ">=2.0下注")]:
        sub = [d for d in all_detail if abs(d[4]) >= thresh]
        if sub:
            sw = sum(d[3] for d in sub)
            slo, shi = wilson_ci(sw, len(sub))
            print(f"  |score|{name}: {sw}/{len(sub)} = {sw/len(sub):.1%} [{slo:.1%},{shi:.1%}]  (下注率{len(sub)/n:.0%})")
        else:
            print(f"  |score|{name}: 无样本")

    # 分桶
    print("\n按|score|分桶:")
    for band in [(0, 1.5, "弱<1.5"), (1.5, 2.5, "中1.5-2.5"), (2.5, 10, "强>=2.5")]:
        l, h, name = band
        sub = [d for d in all_detail if l <= abs(d[4]) < h]
        if sub:
            sw = sum(d[3] for d in sub)
            slo, shi = wilson_ci(sw, len(sub))
            print(f"  {name}: {sw}/{len(sub)} = {sw/len(sub):.1%} [{slo:.1%},{shi:.1%}]")

    # 明细到 md
    out = ["# 图表分析.md v2.0 规则引擎回测", "",
           f"样本 {n} | 全量胜 {wins} ({wins/n:.1%}) | 历史天花板 ~63%", "",
           f"押反转基准: {rev_w/n:.1%}",
           "| slug | actual | pred | win | score | 判定依据 |", "|---|---|---|---|---|---|"]
    for slug, actual, pred, w, score, detail in all_detail:
        out.append(f"| {slug} | {actual} | {pred} | {'✓' if w else '✗'} | {score:.1f} | {detail} |")
    report = DATA_DIR / f"shape_flow_v2_backtest_{datetime.now().strftime('%Y%m%d')}.md"
    report.write_text("\n".join(out), encoding="utf-8")
    print(f"\n明细 → {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
