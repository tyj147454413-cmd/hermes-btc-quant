#!/usr/bin/env python3
"""round41「1H 4H 方向序列」宏观趋势因子回测(2026-08-22)。

目标：验证"最近 4 小时方向"作为评分项(权重 ×1,涨≥3 +1 / 跌≥3 -1 / 2涨2跌 0)
是否提升胜率。数据源：snapshot 的 macro_trend_1h_4h.dirs(采集器 round41 新增)。

回测规则：
  - 优先读 macro_trend_1h_4h.dirs(round41 起新 snapshot 都有)
  - 旧 snapshot 兜底:用 timeframes.1H.consecutive_up/down(最近 6 根内涨次数)
    ≥4 → 全涨(+1),≤2 → 全跌(-1),3 → 中性(0)
  - 计分:up_count>=3 +1 / down_count>=3 -1 / 2涨2跌 0
"""
from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, "scripts")

from rule_engine import judge  # noqa: E402
from signal_validator import extract_factors, _SkipValidation  # noqa: E402

ROOT = Path(os.environ.get("HERMES_DIR", "/Users/tangyijun/Desktop/hermes btc"))
RESULTS = ROOT / "data" / "hermes_results.jsonl"
SNAP = ROOT / "data" / "snapshots"
OUT = ROOT / "data" / "hermes_macro_trend_4h_replay_20260822.md"

BREAKEVEN = 0.50
Z = 1.96


def wilson_lower(k: int, n: int, z: float = Z) -> float:
    if n <= 0:
        return 0.0
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - half)


def actual_map() -> dict[str, str]:
    m: dict[str, str] = {}
    for line in RESULTS.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        slug = rec.get("slug")
        act = rec.get("actual")
        if slug and act in ("UP", "DOWN"):
            m.setdefault(slug, act)
    return m


def load_latest_snapshot(slug: str) -> dict | None:
    cands = sorted(SNAP.glob(f"{slug}_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in cands:
        try:
            md = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if md.get("slug") == slug:
            return md
    return None


def compute_macro_signal(md: dict) -> tuple[list[str], int, str]:
    """返回 (dirs 数组, up_count, 数据源)。"""
    # 优先读 round41 新字段
    m41 = md.get("macro_trend_1h_4h")
    if m41 and isinstance(m41, dict) and m41.get("dirs"):
        dirs = m41["dirs"]
        if len(dirs) >= 3:
            up_count = sum(1 for d in dirs if d == "UP")
            return dirs[-4:], up_count, "macro_trend_1h_4h"
    # 旧 snapshot 兜底:用 timeframes.1H.consecutive_up/down
    tf1h = md.get("timeframes", {}).get("1H", {})
    up = tf1h.get("consecutive_up", 0) or 0
    down = tf1h.get("consecutive_down", 0) or 0
    # 兜底口径:consecutive_up 是"最近 6 根内涨的次数"
    # 简化判定:≥4 → 全涨;≤2 → 全跌;3 → 中性
    if up >= 4:
        return ["UP"] * 4, 4, "consecutive_up_down_fallback"
    if down >= 4:
        return ["DOWN"] * 4, 0, "consecutive_up_down_fallback"
    return ["UP", "UP", "DOWN", "DOWN"], 2, "consecutive_up_down_fallback"


def macro_signal_to_score(up_count: int) -> int:
    """1H 4H 方向序列 → 计分。涨≥3 +1,跌≥3 -1,2涨2跌 0。"""
    if up_count >= 3:
        return 1
    if up_count <= 1:
        return -1
    return 0


def macro_direction(score: int) -> str | None:
    if score >= 1:
        return "UP"
    if score <= -1:
        return "DOWN"
    return None


def engine_direction(md: dict) -> tuple[str | None, str]:
    """用现有 rule_engine 重放，返回 (final_direction, mode)。SKIP/无方向返回 None。"""
    try:
        factors, inp, notes, mode = extract_factors(md)
    except _SkipValidation:
        return None, "skip"
    buy_wall = inp.pop("_buy_wall_ratio", None)
    sell_wall = inp.pop("_sell_wall_ratio", None)
    prelim = judge(dict(inp, extreme_wall=False))
    pdir = prelim.final_direction
    if pdir == "DOWN":
        extreme_wall = bool(sell_wall is not None and sell_wall > 3.0)
    elif pdir == "UP":
        extreme_wall = bool(buy_wall is not None and buy_wall > 3.0)
    else:
        extreme_wall = False
    d = judge(dict(inp, extreme_wall=extreme_wall))
    if d.action == "SKIP" or not d.final_direction:
        return None, mode
    return d.final_direction, mode


def main() -> int:
    amap = actual_map()
    rows = []
    unscanned = 0
    engine_stats = {"n": 0, "correct": 0, "skip": 0}

    for slug, act in amap.items():
        md = load_latest_snapshot(slug)
        if not md:
            unscanned += 1
            continue
        dirs, up_count, src = compute_macro_signal(md)
        score = macro_signal_to_score(up_count)
        mdir = macro_direction(score)
        mok = (mdir == act) if mdir else None

        edir, mode = engine_direction(md)
        if edir:
            engine_stats["n"] += 1
            engine_stats["correct"] += (edir == act)

        rows.append({
            "slug": slug,
            "dirs": dirs,
            "up_count": up_count,
            "score": score,
            "src": src,
            "macro_dir": mdir,
            "macro_ok": mok,
            "act": act,
            "engine_dir": edir,
            "engine_ok": (edir == act) if edir else None,
        })

    # 统计 macro 命中率(只在有方向时)
    macro_n = sum(1 for r in rows if r["macro_dir"] is not None)
    macro_k = sum(1 for r in rows if r["macro_ok"])
    macro_rate = macro_k / macro_n if macro_n else 0.0
    macro_wl = wilson_lower(macro_k, macro_n)

    # 数据源分布(round41 新字段 vs 兜底)
    src_m41 = sum(1 for r in rows if r["src"] == "macro_trend_1h_4h")
    src_fallback = sum(1 for r in rows if r["src"] == "consecutive_up_down_fallback")

    # 强/弱/中性分桶
    up_rows = [r for r in rows if r["score"] == 1]
    dn_rows = [r for r in rows if r["score"] == -1]
    neutral_rows = [r for r in rows if r["score"] == 0]

    def bucket_stat(sub):
        n = len(sub)
        k = sum(1 for r in sub if r["macro_ok"])
        wl = wilson_lower(k, n)
        return n, k, wl

    up_n, up_k, up_wl = bucket_stat(up_rows)
    dn_n, dn_k, dn_wl = bucket_stat(dn_rows)

    # 一致性 vs 引擎
    both_dir = [r for r in rows if r["macro_dir"] and r["engine_dir"]]
    both_dir_n = len(both_dir)
    both_correct = sum(1 for r in both_dir if r["macro_ok"] and r["engine_ok"])
    both_wl = wilson_lower(both_correct, both_dir_n) if both_dir_n else 0.0

    # ===== 输出 =====
    lines = []
    lines.append("# round41「1H 4H 方向序列」宏观趋势因子回测（2026-08-22）")
    lines.append("")
    lines.append(f"> 样本：结算记录 {len(amap)} 场，有快照可分析 {len(rows)} 场，无快照 {unscanned} 场。")
    lines.append("> 数据源：round41 起 snapshot 顶部有 `macro_trend_1h_4h.dirs`（采集器 slim 在删 klines_1h 前算好）；")
    lines.append("> 旧 snapshot 兜底：用 `timeframes.1H.consecutive_up/down`（最近 6 根内涨次数）按 ≥4 / ≤2 / 3 简化判定。")
    lines.append("")
    lines.append("## 数据源分布")
    lines.append("")
    lines.append(f"- macro_trend_1h_4h 新字段: **{src_m41}** 场")
    lines.append(f"- consecutive_up_down 兜底: **{src_fallback}** 场")
    lines.append("")
    lines.append("## 计分规则")
    lines.append("")
    lines.append("| 4 根 1H 方向 | up_count | 计分 | 备注 |")
    lines.append("|---|---|---|---|")
    lines.append("| 涨 ≥3 根(3UP1D / 4UP) | 3 或 4 | +1 | 偏多,权重 ×1 = +1 |")
    lines.append("| 跌 ≥3 根(1UP3D / 4DOWN) | 0 或 1 | -1 | 偏空,权重 ×1 = -1 |")
    lines.append("| 2 涨 2 跌 | 2 | 0 | 中性,不计入净分 |")
    lines.append("")
    lines.append("## 总体命中率")
    lines.append("")
    lines.append(f"- macro 触发样本 **{macro_n}** / {len(rows)} = {macro_n/len(rows)*100 if rows else 0:.1f}%, 命中率 **{macro_k}/{macro_n}={macro_rate*100:.1f}%**, Wilson95 下界 **{macro_wl*100:.1f}%**")
    if engine_stats['n']:
        lines.append(f"- 现有引擎(rule_engine.judge) 触发样本 **{engine_stats['n']}**, 命中率 **{engine_stats['correct']}/{engine_stats['n']}={engine_stats['correct']/engine_stats['n']*100:.1f}%**")
    else:
        lines.append("- 引擎 0 样本")
    lines.append("")
    lines.append("**保本线 50%**;显著有效 = 命中率 - 50% > 0 且 Wilson95 下界 > 50%。")
    lines.append("")
    lines.append("## UP/DOWN/中性 分桶")
    lines.append("")
    lines.append("| 桶 | 定义 | n | 命中 | 命中率 | Wilson95 下界 |")
    lines.append("|---|---|---|---|---|---|")
    lines.append(f"| UP 信号 | up_count ≥3 → +1 | {up_n} | {up_k} | {up_k/up_n*100 if up_n else 0:.1f}% | {up_wl*100:.1f}% |")
    lines.append(f"| DOWN 信号 | up_count ≤1 → -1 | {dn_n} | {dn_k} | {dn_k/dn_n*100 if dn_n else 0:.1f}% | {dn_wl*100:.1f}% |")
    lines.append(f"| 中性 | 2涨2跌 → 0 | {len(neutral_rows)} | - | - | - |")
    lines.append("")
    lines.append("## macro 与现有引擎一致性")
    lines.append("")
    lines.append(f"- 双方都给出方向样本 **{both_dir_n}**")
    lines.append(f"- 双方都正确 **{both_correct}**, 命中率 **{both_correct/both_dir_n*100 if both_dir_n else 0:.1f}%**, Wilson95 下界 **{both_wl*100:.1f}%**")
    lines.append("- 衡量 macro 是否补充/抵消引擎方向")
    lines.append("")
    lines.append("## 结论判定")
    lines.append("")
    if macro_n < 30:
        lines.append(f"⚠️ **样本不足**：macro 触发样本 {macro_n} < 30,按方法论铁律不作决策依据。需补充数据后再回测。")
    else:
        edge = macro_rate - 0.50
        if macro_wl > 0.50:
            lines.append(f"✅ **1H 4H 方向序列独立方向有效**：命中率 {macro_rate*100:.1f}% > 保本 50%, Wilson95 下界 {macro_wl*100:.1f}% > 50% (n={macro_n}, edge={edge*100:+.1f}pp)")
            lines.append(f"   → 建议：保留评分表 round41 新行(权重 ×1,涨≥3 +1 / 跌≥3 -1 / 2涨2跌 0)")
        elif macro_wl > 0.45:
            lines.append(f"⚠️ **边际有效**：命中率 {macro_rate*100:.1f}% 略 > 50%, Wilson95 下界 {macro_wl*100:.1f}% < 50% (n={macro_n})")
            lines.append(f"   → 建议：保留 ×1 但加 '仅 G5 大趋势辅助' 限制,或继续观察 30 场新数据")
        else:
            lines.append(f"❌ **无效或反向**：命中率 {macro_rate*100:.1f}% (n={macro_n}), Wilson95 下界 {macro_wl*100:.1f}%")
            lines.append(f"   → 建议：回滚 round41 新评分行 + 删除 macro_trend_1h_4h 字段")
    lines.append("")

    OUT.write_text("\n".join(lines))
    print(f"\n✅ 回测完成,样本 {len(rows)} 场(macro 触发 {macro_n} 场),报告: {OUT}")
    print(f"   macro 命中率 {macro_rate*100:.1f}% (Wilson95 下界 {macro_wl*100:.1f}%)")
    print(f"   引擎命中率 {engine_stats['correct']/engine_stats['n']*100 if engine_stats['n'] else 0:.1f}% (n={engine_stats['n']})")
    print(f"   数据源: macro_trend_1h_4h={src_m41}场 / consecutive_up_down 兜底={src_fallback}场")
    return 0


if __name__ == "__main__":
    sys.exit(main())