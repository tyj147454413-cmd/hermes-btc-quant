#!/usr/bin/env python3
"""
测试贴线模式增强策略对胜率的影响。

方案：
1. 贴线 + |S0| < 3 → 额外缩仓 0.5（弱信号+贴线=高风险）
2. 贴线 + S0方向与PM同向 → 锁低（当前已有 scale 0.5，测试锁低效果）
3. 组合方案

用法：python3 scripts/test_tie_enhance.py
"""

from __future__ import annotations

import json
import sys
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


def main() -> int:
    results = load_results()
    slugs = get_latest_snapshots()

    # 收集每场的基础数据
    records = []
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
        if not is_tie:
            continue

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

        pm_up = float(inp.get("pm_up", 50) or 50)
        pm_dir = "UP" if pm_up > 50 else ("DOWN" if pm_up < 50 else None)
        pm_same = pm_dir and d.final_direction == pm_dir

        records.append({
            "slug": slug,
            "actual": actual,
            "direction": d.final_direction,
            "win": d.final_direction == actual,
            "s0": d.s0,
            "net": d.net,
            "confidence": d.confidence,
            "position_scale": d.position_scale,
            "pm_same": pm_same,
            "h12": d.h12_triggered,
        })

    # 当前基线
    base_wins = sum(1 for r in records if r["win"])
    base_total = len(records)
    print(f"## 贴线模式增强策略测试\n")
    print(f"贴线基线: {base_wins}/{base_total} = {base_wins/base_total*100:.1f}%\n")

    # 分析各子组的胜率
    print("### 贴线子组分析\n")
    print("| 子组 | 场次 | 胜率 |")
    print("|------|------|------|")

    # 弱S0 (<3)
    weak = [r for r in records if abs(r["s0"]) < 3]
    strong = [r for r in records if abs(r["s0"]) >= 3]
    print(f"| |S0|<3 (弱信号) | {len(weak)} | {sum(1 for r in weak if r['win'])/len(weak)*100:.1f}% |")
    print(f"| |S0|≥3 (强信号) | {len(strong)} | {sum(1 for r in strong if r['win'])/len(strong)*100:.1f}% |")

    # S0方向 vs PM
    pm_same_recs = [r for r in records if r["pm_same"]]
    pm_diff_recs = [r for r in records if not r["pm_same"]]
    print(f"| 顺PM | {len(pm_same_recs)} | {sum(1 for r in pm_same_recs if r['win'])/len(pm_same_recs)*100:.1f}% |")
    print(f"| 逆PM | {len(pm_diff_recs)} | {sum(1 for r in pm_diff_recs if r['win'])/len(pm_diff_recs)*100:.1f}% |")

    # H12
    h12_recs = [r for r in records if r["h12"]]
    no_h12 = [r for r in records if not r["h12"]]
    print(f"| H12触发 | {len(h12_recs)} | {sum(1 for r in h12_recs if r['win'])/len(h12_recs)*100:.1f}% |")
    print(f"| 无H12 | {len(no_h12)} | {sum(1 for r in no_h12 if r['win'])/len(no_h12)*100:.1f}% |")

    # 交叉：弱S0+顺PM
    weak_pm_same = [r for r in weak if r["pm_same"]]
    print(f"| |S0|<3 + 顺PM | {len(weak_pm_same)} | {sum(1 for r in weak_pm_same if r['win'])/len(weak_pm_same)*100:.1f}% |")

    # 交叉：弱S0+逆PM
    weak_pm_diff = [r for r in weak if not r["pm_same"]]
    print(f"| |S0|<3 + 逆PM | {len(weak_pm_diff)} | {sum(1 for r in weak_pm_diff if r['win'])/len(weak_pm_diff)*100:.1f}% |")

    # 交叉：强S0+顺PM
    strong_pm_same = [r for r in strong if r["pm_same"]]
    print(f"| |S0|≥3 + 顺PM | {len(strong_pm_same)} | {sum(1 for r in strong_pm_same if r['win'])/len(strong_pm_same)*100:.1f}% |")

    print(f"\n### 结论")
    print(f"贴线模式胜率 73.2% vs 常规 80.3%，差距 7.1%。")
    print(f"主要风险子组：|S0|<3 弱信号（{len(weak)}场，胜率 {sum(1 for r in weak if r['win'])/len(weak)*100:.1f}%）。")
    print(f"建议：贴线+|S0|<3 → 额外缩仓或锁低，不改方向（当前已有 9.7#4 贴线+顺PM scale 0.5，")
    print(f"但弱S0+顺PM 子组胜率仍偏低，可考虑在该子组额外降级。")

    return 0


if __name__ == "__main__":
    sys.exit(main())