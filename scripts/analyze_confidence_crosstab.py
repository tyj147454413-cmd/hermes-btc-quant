#!/usr/bin/env python3
"""
置信度重构校准数据：|净分|区间 × 贴线 × PM同向 × Regime 的交叉胜率。

用法：python3 scripts/analyze_confidence_crosstab.py
输出：四维交叉表 + 各维度的独立/联合预测力
"""

from __future__ import annotations

import json, sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from signal_validator import extract_factors
from rule_engine import judge

SNAPSHOT_DIR = Path("/Users/tangyijun/Desktop/hermes btc/data/snapshots")
RESULTS_FILE = Path("/Users/tangyijun/Desktop/hermes btc/data/hermes_results.jsonl")


def load_results() -> dict[str, str]:
    results: dict[str, str] = {}
    if not RESULTS_FILE.exists():
        return results
    for line in RESULTS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("actual"):
            results[r["slug"]] = r["actual"]
    return results


def get_latest_snapshots() -> dict[str, Path]:
    slugs: dict[str, list[Path]] = defaultdict(list)
    for p in sorted(SNAPSHOT_DIR.glob("*.json")):
        slug = p.name.split("_202")[0]
        slugs[slug].append(p)
    return {slug: sorted(paths, key=lambda x: x.stat().st_mtime)[-1] for slug, paths in slugs.items()}


def net_bucket(net: float) -> str:
    an = abs(net)
    if an >= 6:
        return "≥6"
    if an >= 3:
        return "3-5"
    return "0-2"


def main() -> int:
    results = load_results()
    slugs = get_latest_snapshots()

    rows = []
    for slug, path in sorted(slugs.items()):
        actual = results.get(slug)
        if not actual:
            continue
        try:
            md = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        try:
            factors, inp, notes, mode = extract_factors(md)
        except Exception:
            continue

        is_tie = bool(inp.get("_tie_mode"))
        regime = inp.get("regime", "?")
        pm_up = float(inp.get("pm_up", 50) or 50)

        buy_wall = inp.pop("_buy_wall_ratio")
        sell_wall = inp.pop("_sell_wall_ratio")
        taker_bs_r = inp.pop("taker_bs_ratio", None)

        prelim = judge(dict(inp, extreme_wall=False))
        prelim_dir = prelim.final_direction
        if prelim_dir == "DOWN":
            extreme_wall = bool(sell_wall is not None and sell_wall > 3.0)
        elif prelim_dir == "UP":
            extreme_wall = bool(buy_wall is not None and buy_wall > 3.0)
        else:
            extreme_wall = False

        d = judge(dict(inp, extreme_wall=extreme_wall,
                       _buy_wall_ratio=buy_wall, _sell_wall_ratio=sell_wall,
                       taker_bs_ratio=taker_bs_r))

        if not d.final_direction:
            continue

        pm_dir = "UP" if pm_up > 50 else ("DOWN" if pm_up < 50 else None)
        pm_same = pm_dir and d.final_direction == pm_dir

        rows.append({
            "slug": slug,
            "win": d.final_direction == actual,
            "net": d.net,
            "net_bucket": net_bucket(d.net),
            "is_tie": is_tie,
            "regime": regime,
            "pm_same": pm_same,
            "s0": d.s0,
        })

    total = len(rows)
    total_wins = sum(1 for r in rows if r["win"])
    print(f"# 全量基线: {total_wins}/{total} = {total_wins/total*100:.1f}%\n")

    # === 一维分析 ===
    print("## 一、单维度胜率\n")
    for dim, key in [
        ("|净分|区间", "net_bucket"),
        ("贴线模式", "is_tie"),
        ("Regime", "regime"),
        ("PM同向", "pm_same"),
    ]:
        print(f"### {dim}")
        print("| 值 | 场次 | 胜率 |")
        print("|----|------|------|")
        groups = defaultdict(lambda: {"wins": 0, "total": 0})
        for r in rows:
            groups[r[key]]["wins"] += r["win"]
            groups[r[key]]["total"] += 1
        for val in sorted(groups.keys(), key=str):
            g = groups[val]
            wr = g["wins"] / g["total"] * 100
            print(f"| {val} | {g['total']} | {wr:.1f}% |")
        print()

    # === 二维交叉：|净分| × 贴线 ===
    print("## 二、|净分|区间 × 贴线模式\n")
    print("| |净分| | 贴线 | 场次 | 胜率 |")
    print("|------|------|------|------|")
    groups = defaultdict(lambda: {"wins": 0, "total": 0})
    for r in rows:
        key = (r["net_bucket"], "贴线" if r["is_tie"] else "常规")
        groups[key]["wins"] += r["win"]
        groups[key]["total"] += 1
    for nb in ["0-2", "3-5", "≥6"]:
        for tie in ["常规", "贴线"]:
            g = groups.get((nb, tie), {"wins": 0, "total": 0})
            if g["total"] > 0:
                wr = g["wins"] / g["total"] * 100
                print(f"| {nb} | {tie} | {g['total']} | {wr:.1f}% |")
    print()

    # === 二维交叉：|净分| × PM同向 ===
    print("## 三、|净分|区间 × PM同向\n")
    print("| |净分| | PM | 场次 | 胜率 |")
    print("|------|------|------|------|")
    groups = defaultdict(lambda: {"wins": 0, "total": 0})
    for r in rows:
        key = (r["net_bucket"], "顺PM" if r["pm_same"] else "逆PM")
        groups[key]["wins"] += r["win"]
        groups[key]["total"] += 1
    for nb in ["0-2", "3-5", "≥6"]:
        for pm in ["顺PM", "逆PM"]:
            g = groups.get((nb, pm), {"wins": 0, "total": 0})
            if g["total"] > 0:
                wr = g["wins"] / g["total"] * 100
                print(f"| {nb} | {pm} | {g['total']} | {wr:.1f}% |")
    print()

    # === 二维交叉：|净分| × Regime ===
    print("## 四、|净分|区间 × Regime\n")
    print("| |净分| | Regime | 场次 | 胜率 |")
    print("|------|------|------|------|")
    groups = defaultdict(lambda: {"wins": 0, "total": 0})
    for r in rows:
        key = (r["net_bucket"], r["regime"])
        groups[key]["wins"] += r["win"]
        groups[key]["total"] += 1
    for nb in ["0-2", "3-5", "≥6"]:
        for reg in ["摆动市", "趋势市"]:
            g = groups.get((nb, reg), {"wins": 0, "total": 0})
            if g["total"] > 0:
                wr = g["wins"] / g["total"] * 100
                print(f"| {nb} | {reg} | {g['total']} | {wr:.1f}% |")
    print()

    # === 三维交叉：|净分| × 贴线 × PM ===
    print("## 五、|净分|区间 × 贴线 × PM同向（核心校准表）\n")
    print("| |净分| | 贴线 | PM | 场次 | 胜率 |")
    print("|------|------|------|------|------|")
    groups = defaultdict(lambda: {"wins": 0, "total": 0})
    for r in rows:
        key = (r["net_bucket"], "贴线" if r["is_tie"] else "常规", "顺PM" if r["pm_same"] else "逆PM")
        groups[key]["wins"] += r["win"]
        groups[key]["total"] += 1
    for nb in ["0-2", "3-5", "≥6"]:
        for tie in ["常规", "贴线"]:
            for pm in ["顺PM", "逆PM"]:
                g = groups.get((nb, tie, pm), {"wins": 0, "total": 0})
                if g["total"] >= 3:  # 至少3场才显示
                    wr = g["wins"] / g["total"] * 100
                    print(f"| {nb} | {tie} | {pm} | {g['total']} | {wr:.1f}% |")
    print()

    # === 汇总：推荐置信度映射 ===
    print("## 六、推荐置信度映射方案\n")
    print("基于 |净分| 区间 + 贴线 + PM 三个维度，样本足够的组合直接使用历史胜率；")
    print("样本不足的组合回退到更粗粒度。\n")
    print("| 组合 | 场次 | 胜率 | 建议置信度 |")
    print("|------|------|------|-----------|")

    # 粗粒度回退：|净分|区间 为主
    for nb in ["0-2", "3-5", "≥6"]:
        g = defaultdict(lambda: {"wins": 0, "total": 0})
        for r in rows:
            if r["net_bucket"] == nb:
                g["all"]["wins"] += r["win"]
                g["all"]["total"] += 1
        wr = g["all"]["wins"] / g["all"]["total"] * 100
        conf = "高" if wr >= 80 else ("中" if wr >= 70 else "低")
        print(f"| |净分|{nb} | {g['all']['total']} | {wr:.1f}% | **{conf}** |")

    # 细粒度：有足够样本的组合
    for nb in ["0-2", "3-5", "≥6"]:
        for tie in ["常规", "贴线"]:
            for pm in ["顺PM", "逆PM"]:
                g = groups.get((nb, tie, pm), {"wins": 0, "total": 0})
                if g["total"] >= 5:
                    wr = g["wins"] / g["total"] * 100
                    conf = "高" if wr >= 80 else ("中" if wr >= 70 else "低")
                    print(f"| |净分|{nb} + {tie} + {pm} | {g['total']} | {wr:.1f}% | **{conf}** |")

    print(f"\n全量: {total} 场, {total_wins} 胜")

    return 0


if __name__ == "__main__":
    sys.exit(main())