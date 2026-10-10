#!/usr/bin/env python3
"""封顶价反事实回测：round15 封顶 0.60 是否过严？

对比封顶 0.60 / 0.70 / 0.80（以及无封顶基线）下的总 EV 与成交率。
用真实成交记录（data/hermes_done.jsonl 的 order_price=真实挂单价）关联
结算结果（data/hermes_results.jsonl 的 actual）分桶计算。

两个口径：
- 口径1「全成交」：每单成交价 = min(order_price, cap)，不砍单（理想化，反映价格修正后的方向价值）
- 口径2「回落率修正」：order_price <= cap 直接成交；order_price > cap 需价格回落到 cap 才成交，
  回落概率按实测（UP 44% / DOWN 30%，13min 内），不成交的单不计 EV（反映真实砍单效果）

用法：python3 scripts/backtest_price_cap.py
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path("/Users/tangyijun/Desktop/hermes btc")
DONE = ROOT / "data" / "hermes_done.jsonl"
RESULTS = ROOT / "data" / "hermes_results.jsonl"
OUT = ROOT / "data" / "hermes_cap_backtest_20260813.md"

# 回落率（8/13 实证：:15→:28 双快照 13min 内回落率）
FALLBACK_UP = 0.44
FALLBACK_DOWN = 0.30
CAPS = [0.55, 0.60, 0.65, 0.70, 0.80, 0.90]  # 0.90≈无封顶（历史最高挂价 0.87）


def load_trades() -> list[dict]:
    res_map: dict[str, str] = {}
    for line in RESULTS.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("actual") in ("UP", "DOWN"):
            res_map.setdefault(r.get("slug"), r.get("actual"))

    trades = []
    for line in DONE.read_text().splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        sig = e.get("signal", {})
        res = e.get("result", {})
        if not (res.get("ok") and not res.get("skipped") and res.get("order_id")):
            continue
        op = res.get("order_price")
        slug = sig.get("slug", "")
        side = (sig.get("side") or "").upper()
        if not op or not slug:
            continue
        trades.append({
            "slug": slug,
            "side": side,
            "op": float(op),
            "act": res_map.get(slug),
        })
    return trades


def ev_for(price: float, win: bool) -> float:
    """每 $1 投入的单场 EV：胜 (1/p-1)，负 -1"""
    if price <= 0:
        return 0.0
    return (1.0 / price - 1.0) if win else -1.0


def backtest(trades: list[dict], cap: float):
    """返回 (n_trades, n_fill, total_ev, avg_ev_filled, n_wins_filled, n_win_potential)"""
    n_fill = 0
    total_ev = 0.0
    n_wins_fill = 0
    # 口径2 专用统计
    for t in trades:
        if t["act"] not in ("UP", "DOWN"):
            continue
        op = t["op"]
        if op <= cap:
            # 封顶不生效，直接按原价成交
            price = op
            filled = True
        else:
            # 封顶生效：挂 cap 等回落
            price = cap
            fb = FALLBACK_UP if t["side"] == "UP" else FALLBACK_DOWN
            filled = (t["op"] <= cap * 1.0)  # 上方已处理
            # 用回落概率模拟成交（确定性口径：此处用期望，但单独输出）
            # 我们分两种：下面在 run() 里处理概率模拟
            filled = None  # 占位，run() 里按概率累计
            t["_fb"] = fb
        if filled is not None:
            n_fill += 1
            win = t["side"] == t["act"]
            if win:
                n_wins_fill += 1
            total_ev += ev_for(price, win)
    return n_fill, total_ev, n_wins_fill


def main() -> int:
    trades = load_trades()
    lab = [t for t in trades if t["act"] in ("UP", "DOWN")]
    print(f"总成交 {len(trades)} | 有结算 {len(lab)}")

    lines = []
    lines.append("# 封顶价反事实回测：round15 的 0.60 是否过严？（2026-08-13）\n")
    lines.append("> 数据：`data/hermes_done.jsonl` 197 条真实成交（`order_price`=真实挂单价）关联 `hermes_results.jsonl` 结算结果（194 条可结算）。")
    lines.append("> 方法：对封顶 cap，每单成交价 = min(order_price, cap)；胜 (1/p-1)/负 -1 算 EV。")
    lines.append("> 口径 1「全成交」= 所有单都按封顶后价成交（反映价格修正后的方向价值，高估成交）。")
    lines.append("> 口径 2「回落率修正」= order_price>cap 的单需价格回落到 cap 才成交，回落概率实测 UP 44% / DOWN 30%；不成交不计 EV（反映真实砍单）。\n")
    lines.append("---\n")

    # 基线：无封顶（cap=0.90 覆盖全部）
    base_ev = sum(ev_for(t["op"], t["side"] == t["act"]) for t in lab)
    base_n = len(lab)
    base_wins = sum(1 for t in lab if t["side"] == t["act"])
    lines.append(f"## 基线（无封顶，实际挂单价）\n")
    lines.append(f"- 成交 {base_n} 场，胜率 {base_wins/base_n*100:.1f}%，总 EV **{base_ev:+.2f}**（每单 {base_ev/base_n:+.3f}）\n")

    lines.append("## 口径 1「全成交」对比\n")
    lines.append("| 封顶 | 成交价后总EV | vs基线 | 每单EV |")
    lines.append("|------|-----------|-------|-------|")
    for cap in CAPS:
        ev = sum(ev_for(min(t["op"], cap), t["side"] == t["act"]) for t in lab)
        lines.append(f"| {cap:.2f} | {ev:+.2f} | {ev-base_ev:+.2f} | {ev/base_n:+.3f} |")
    lines.append("")

    lines.append("## 口径 2「回落率修正」（真实砍单模拟）\n")
    lines.append("| 封顶 | 成交场数 | 成交率 | 总EV | 每单EV | 砍掉EV |")
    lines.append("|------|---------|-------|------|--------|--------|")
    for cap in CAPS:
        n_fill = 0
        ev = 0.0
        n_wins = 0
        for t in lab:
            op = t["op"]
            if op <= cap:
                n_fill += 1
                win = t["side"] == t["act"]
                if win:
                    n_wins += 1
                ev += ev_for(op, win)
            else:
                # 封顶生效，需回落；按概率期望计入成交与 EV
                fb = FALLBACK_UP if t["side"] == "UP" else FALLBACK_DOWN
                win = t["side"] == t["act"]
                ev_fb = ev_for(cap, win) * fb  # 成交概率 fb，成交价 cap
                ev += ev_fb
                if win:
                    n_wins += fb
                n_fill += fb  # 期望成交数
        ev_cut = base_ev - ev
        lines.append(f"| {cap:.2f} | {n_fill:.1f} | {n_fill/base_n*100:.0f}% | {ev:+.2f} | {ev/n_fill:+.3f} | {ev_cut:+.2f} |")
    lines.append("")

    # 分桶明细（无封顶的真实 order_price 分桶）
    lines.append("## 真实 order_price 分桶（无封顶）\n")
    lines.append("| 价格桶 | 场次 | 胜率 | 每单EV |")
    lines.append("|--------|------|------|--------|")
    buckets = [("0.35-0.49", 0.35, 0.50), ("0.50-0.59", 0.50, 0.60),
               ("0.60-0.69", 0.60, 0.70), ("0.70-0.79", 0.70, 0.80),
               ("0.80-0.89", 0.80, 0.90), ("0.90+", 0.90, 1.01)]
    for name, lo, hi in buckets:
        bs = [t for t in lab if lo <= t["op"] < hi]
        if not bs:
            continue
        wins = sum(1 for t in bs if t["side"] == t["act"])
        ev = sum(ev_for(t["op"], t["side"] == t["act"]) for t in bs)
        lines.append(f"| {name} | {len(bs)} | {wins/len(bs)*100:.1f}% | {ev/len(bs):+.3f} |")
    lines.append("")

    lines.append("## 结论\n")
    lines.append("> 以口径 2（真实砍单）为准。对比各封顶下总 EV 与成交率，找到 EV 最高且不过度砍单的封顶价。")
    lines.append("> 若 0.70 比 0.60 总 EV 更高（因为 0.60-0.79 是正 EV 区，砍掉它们损失 > 避免 0.80+ 亏损），则 round15 的 0.60 过严。")
    lines.append("> 注意小样本：各高价桶 n=12-37，结论需结合 24-48h 实盘观察复核。\n")
    lines.append("---\n")
    lines.append("*报告由 scripts/backtest_price_cap.py 生成*")

    OUT.write_text("\n".join(lines) + "\n")
    print(f"已生成 {OUT}")

    # 终端摘要
    print("\n=== 口径2 结果汇总 ===")
    for cap in CAPS:
        n_fill = 0
        ev = 0.0
        for t in lab:
            op = t["op"]
            if op <= cap:
                n_fill += 1
                ev += ev_for(op, t["side"] == t["act"])
            else:
                fb = FALLBACK_UP if t["side"] == "UP" else FALLBACK_DOWN
                ev += ev_for(cap, t["side"] == t["act"]) * fb
                n_fill += fb
        print(f"  cap={cap:.2f}: 成交{n_fill:.1f}/{base_n} ({n_fill/base_n*100:.0f}%) 总EV {ev:+.2f} (vs基线{base_ev:+.2f}, {ev-base_ev:+.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
