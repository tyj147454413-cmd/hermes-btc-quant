#!/usr/bin/env python3
"""三宇宙成交偏差回测（8/21 A1，学习系统数据完整性层）。

解决的问题：round37 条件封顶（praw>=0.70 → cap 0.70）的数据依据
"praw>=0.70 全量胜率 71.6% → 0.70 微正 EV" 存在三个统计缺口：
  1. 档案不可复现：done.jsonl 424 条仅 9 条带 probability_raw（round36 后才有该字段）
  2. 口径混合：71.6% 是"信号宇宙"方向胜率（不分能否成交），而 EV 决策需要"可成交宇宙"胜率
  3. 无置信区间：点估计 + 微正 EV（+1.6pp）在统计噪声内不可分辨

本脚本用引擎重放统一口径（与 regime_split_replay.py 同机制），把同一批信号在三个
宇宙下分别统计：
  - 信号宇宙：全部已结算场（方向质量，无偏但不含执行约束）
  - taker 可成交宇宙：快照 PM 价(方向侧)+spread ≤ round37 有效 cap（cap 决策应依据的口径）
  - 实际成交宇宙：results.filled=True（历史 P&L，maker 时代为主，含逆向选择偏差）

只读分析：不改引擎、不写信号队列、不写 drift_alerts（报告落 data/）。

用法：python3 scripts/fill_bias_backtest.py [--spread 0.01]
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from replay_common import (
    load_results_full as load_results,
    load_fills,
    load_done,
    get_latest_snapshots as latest_snapshots,
    wilson,
    ev_at,
    replay_judge_full,
    ROOT,
)

# round37 规则镜像（hermes_1h_bot.py _effective_price_cap）
HIGH_PROB_CAP = 0.70
HIGH_PROB_MIN = 0.70
BASE_CAP = 0.60

SPREADS = [0.00, 0.01, 0.02]  # ask 代理 = 快照 PM 价 + spread（Gamma 中点→真实 ask 的敏感性）

# 注：wilson / ev_at / load_results / load_fills / load_done / latest_snapshots 已迁至
# scripts/replay_common.py。改引擎表或回测口径必须同步 replay_common（SYNC_MAP #11）。


def main() -> int:
    results = load_results()
    fills = load_fills()
    done = load_done()
    snaps = latest_snapshots()

    records: list[dict] = []
    for slug, path in sorted(snaps.items()):
        res = results.get(slug)
        if not res:
            continue
        actual = res["actual"].upper()
        try:
            md = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        # —— 引擎重放（两遍 extreme_wall 逻辑迁至 scripts/replay_common.py）——
        replay = replay_judge_full(md)
        if replay is None:
            continue
        d, _factors, _inp = replay
        if d.probability is None:
            continue

        praw = d.probability / 100.0  # 引擎 pseudo-praw（4维表查表值）
        cap = HIGH_PROB_CAP if praw >= HIGH_PROB_MIN else BASE_CAP

        # —— 快照 PM 价（方向侧）——
        pm = md.get("polymarket") or {}
        pm_side_price = pm.get("up_price" if d.final_direction == "UP" else "down_price")
        if pm_side_price is None:
            continue

        # —— 实际成交状态（results 优先，fills 回填）——
        filled = res.get("filled")
        if filled is None:
            filled = fills.get(res.get("order_id"))

        live = done.get(slug) or {}
        live_side = (live.get("side") or live.get("signal") or "").upper() or None

        records.append({
            "slug": slug,
            "direction": d.final_direction,
            "praw": praw,
            "cap": cap,
            "pm_side_price": float(pm_side_price),
            "win": d.final_direction == actual,
            "live_side": live_side,
            "live_win": (live_side == actual) if live_side else None,
            "filled": filled,
            "buy_price": res.get("buy_price"),
            "ts": res.get("ts", ""),
        })

    total = len(records)
    eng_wins = sum(1 for r in records if r["win"])
    live_pairs = [r for r in records if r["live_side"]]
    live_agree = sum(1 for r in live_pairs if r["live_side"] == r["direction"])
    live_wins = sum(1 for r in live_pairs if r["live_win"])
    filled_known = [r for r in records if r["filled"] is not None]
    filled_true = [r for r in records if r["filled"] is True]

    lines: list[str] = []
    lines.append("# 三宇宙成交偏差回测（fill selection bias，8/21 A1）\n")
    lines.append(f"- 运行时间：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"- 样本：{total} 场（快照×已结算×引擎重放匹配；PM 价缺失已剔除）")
    lines.append(f"- 引擎方向胜率：{eng_wins}/{total} = {eng_wins / total * 100:.1f}%")
    if live_pairs:
        lines.append(f"- 实盘 AI 信号可匹配 {len(live_pairs)} 场：AI 胜率 {live_wins}/{len(live_pairs)} = "
                     f"{live_wins / len(live_pairs) * 100:.1f}%；AI 与引擎方向一致率 "
                     f"{live_agree}/{len(live_pairs)} = {live_agree / len(live_pairs) * 100:.1f}%"
                     f"（引擎宇宙是 AI 策略的确定性代理，一致率越高代理越可靠）")
    lines.append(f"- 成交状态已知：{len(filled_known)} 场（filled=True {len(filled_true)} 场；"
                 f"其余为 maker 时代无成交追踪）")
    lines.append("")
    lines.append("口径说明：pseudo-praw = 引擎四维表查表概率（历史信号 415/424 缺 probability_raw，"
                 "只能引擎重放统一口径）；ask 代理 = 快照 PM 价(方向侧) + spread；"
                 "cap 按 round37 规则（praw≥0.70→0.70，否则 0.60）。\n")

    # ============ 分桶 × 宇宙统计 ============
    def bucket_of(praw: float) -> str:
        if praw >= 0.70:
            return "praw>=0.70"
        if praw >= 0.60:
            return "0.60-0.70"
        return "<0.60"

    def stat_row(label: str, subset: list[dict]) -> str:
        n = len(subset)
        if n == 0:
            return f"| {label} | 0 | — | — | — | — | — |"
        w = sum(1 for r in subset if r["win"])
        wr = w / n
        wl, wu = wilson(w, n)
        return (f"| {label} | {n} | {w} | {wr * 100:.1f}% | [{wl:.1f}, {wu:.1f}] "
                f"| {ev_at(wr, 0.60):+.3f} | {ev_at(wr, 0.70):+.3f} |")

    lines.append("## 主表：praw 桶 × 宇宙（胜率 / Wilson95 / EV）\n")
    lines.append("| 桶 × 宇宙 | n | 胜 | 胜率 | Wilson95 | EV@0.60 | EV@0.70 |")
    lines.append("|---|---|---|---|---|---|---|")

    verdict_lines: list[str] = []
    for b in ["praw>=0.70", "0.60-0.70", "<0.60"]:
        sub = [r for r in records if bucket_of(r["praw"]) == b]
        lines.append(stat_row(f"{b} · 信号宇宙", sub))
        for sp in SPREADS:
            taker = [r for r in sub if r["pm_side_price"] + sp <= r["cap"]]
            lines.append(stat_row(f"{b} · taker可成交(spread {sp:.2f})", taker))
        filled_sub = [r for r in sub if r["filled"] is True]
        lines.append(stat_row(f"{b} · 实际成交", filled_sub))
        lines.append("| | | | | | | |")

        if b == "praw>=0.70":
            for sp in SPREADS:
                taker = [r for r in sub if r["pm_side_price"] + sp <= r["cap"]]
                n = len(taker)
                if n == 0:
                    continue
                w = sum(1 for r in taker if r["win"])
                wr = w / n
                wl, _ = wilson(w, n)
                verdict_lines.append(
                    f"- spread {sp:.2f}: n={n}, 胜率 {wr * 100:.1f}%, Wilson95 下界 {wl:.1f}%, "
                    f"EV@0.70 {ev_at(wr, 0.70):+.3f}（盈亏平衡点 70.0%）")

    # ============ 逆向选择量化：信号宇宙 vs taker 可成交宇宙 ============
    lines.append("\n## 边界逆向选择量化（信号宇宙胜率 − taker可成交胜率）\n")
    lines.append("差值为正 → 越过 cap 边界买不到的单恰好是赢面高的单（边界处存在逆向选择），"
                 "cap 决策必须用 taker 宇宙而非信号宇宙口径。\n")
    for b in ["praw>=0.70", "0.60-0.70", "<0.60"]:
        sub = [r for r in records if bucket_of(r["praw"]) == b]
        if not sub:
            continue
        sig_wr = sum(1 for r in sub if r["win"]) / len(sub)
        parts = []
        for sp in SPREADS:
            taker = [r for r in sub if r["pm_side_price"] + sp <= r["cap"]]
            if taker:
                t_wr = sum(1 for r in taker if r["win"]) / len(taker)
                parts.append(f"spread{sp:.2f}: {t_wr * 100:.1f}%（Δ{(sig_wr - t_wr) * 100:+.1f}pp, n={len(taker)}）")
        lines.append(f"- {b}: 信号宇宙 {sig_wr * 100:.1f}%（n={len(sub)}） | " + " | ".join(parts))

    # ============ round37 判决 ============
    lines.append("\n## round37 判决（praw≥0.70 taker 可成交宇宙 vs 盈亏平衡点）\n")
    lines.append("round37 依据（bot 注释）：'praw≥0.70 全量胜率 71.6% → 0.70 微正 EV'。"
                 "本节用可成交宇宙复检：Wilson95 下界 ≥70% → 基线成立；65-70% → 边缘；<65% → 基线不成立。\n")
    lines.extend(verdict_lines)

    sig70 = [r for r in records if bucket_of(r["praw"]) == "praw>=0.70"]
    if sig70:
        w = sum(1 for r in sig70 if r["win"])
        wl, wu = wilson(w, len(sig70))
        lines.append(f"- 对照·信号宇宙口径: n={len(sig70)}, 胜率 {w / len(sig70) * 100:.1f}%, "
                     f"Wilson95 [{wl:.1f}, {wu:.1f}]（71.6% 应落在此区间内方可复现）")
    lines.append("")

    # ============ 实际成交宇宙（历史 P&L 偏差）============
    if filled_true:
        w = sum(1 for r in filled_true if r["win"])
        wl, wu = wilson(w, len(filled_true))
        lines.append("## 实际成交宇宙（历史 P&L 口径，maker 时代为主）\n")
        lines.append(f"- filled=True: n={len(filled_true)}, 引擎方向胜率 {w / len(filled_true) * 100:.1f}%, "
                     f"Wilson95 [{wl:.1f}, {wu:.1f}]")
        unfilled = [r for r in records if r["filled"] is False]
        if unfilled:
            wu_ = sum(1 for r in unfilled if r["win"])
            lines.append(f"- filled=False: n={len(unfilled)}, 引擎方向胜率 "
                         f"{wu_ / len(unfilled) * 100:.1f}%（未成交组胜率显著更高 = 挂低价逆向选择，"
                         f"8/19 发现 87.5% vs 53.4% 的引擎口径复核）")
        lines.append("")

    report = "\n".join(lines)
    out_file = ROOT / "data" / f"hermes_fill_bias_backtest_{datetime.now().strftime('%Y%m%d')}.md"
    out_file.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n报告已写入: {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
