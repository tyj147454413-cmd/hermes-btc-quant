#!/usr/bin/env python3
"""
概率函数校准：用回测历史胜率替代固定映射公式。

对每个 (|净分|区间, 置信度) 组合统计实际胜率，
输出可直接用于 _probability 函数的映射表。

用法：python3 scripts/calibrate_probability.py
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


def bucket_net(net: float) -> str:
    """净分分桶，与 _probability 的 p0 档位对齐。"""
    an = abs(net)
    if an >= 6:
        return "|net|>=6"
    if an >= 3:
        return "3<=|net|<6"
    return "|net|<3"


def main() -> int:
    results = load_results()
    slugs = get_latest_snapshots()

    # 收集每场的 (net, confidence, actual_win)
    records: list[dict] = []

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

        buy_wall = inp.pop("_buy_wall_ratio")
        sell_wall = inp.pop("_sell_wall_ratio")
        taker_bs = inp.pop("taker_bs_ratio", None)

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
                       taker_bs_ratio=taker_bs))

        if not d.final_direction:
            continue

        win = d.final_direction == actual
        net_bucket = bucket_net(d.net)
        conf = d.confidence

        records.append({
            "slug": slug,
            "net": d.net,
            "net_bucket": net_bucket,
            "confidence": conf,
            "direction": d.final_direction,
            "actual": actual,
            "win": win,
            "prob_old": d.probability,
        })

    # 按 (net_bucket, confidence) 分组统计
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in records:
        groups[(r["net_bucket"], r["confidence"])].append(r)

    print("## 概率校准结果\n")
    print("| 净分区间 | 置信度 | 场次 | 胜 | 负 | 实际胜率 | 旧映射概率 | 偏差 |")
    print("|---------|--------|------|-----|-----|---------|----------|------|")

    # 按净分区间排序
    bucket_order = ["|net|>=6", "3<=|net|<6", "|net|<3"]
    conf_order = ["高", "中", "低"]

    calibration: dict[str, dict[str, float]] = {}

    for nb in bucket_order:
        for cf in conf_order:
            key = (nb, cf)
            g = groups.get(key, [])
            n = len(g)
            if n == 0:
                continue
            w = sum(1 for r in g if r["win"])
            rate = w / n * 100
            old_prob = sum(r["prob_old"] for r in g) / n if g else 0
            diff = rate - old_prob
            print(f"| {nb} | {cf} | {n} | {w} | {n-w} | {rate:.1f}% | {old_prob:.1f}% | {diff:+.1f}% |")

            if nb not in calibration:
                calibration[nb] = {}
            calibration[nb][cf] = rate

    # 输出 Python 代码可直接使用的映射表
    print("\n## 建议的 _probability 函数映射表\n")
    print("```python")
    print("# 基于 {total} 场回测历史胜率校准".format(total=len(records)))
    print("PROBABILITY_TABLE = {")
    for nb in bucket_order:
        print(f'    "{nb}": {{')
        for cf in conf_order:
            if nb in calibration and cf in calibration[nb]:
                print(f'        "{cf}": {calibration[nb][cf]:.1f},  # n={len(groups.get((nb, cf), []))}')
        print("    },")
    print("}")
    print("```")

    # 全量统计
    total = len(records)
    total_wins = sum(1 for r in records if r["win"])
    print(f"\n全量: {total} 场, 胜率 {total_wins/total*100:.1f}%")

    # 旧 probability 的 MAE
    mae = sum(abs(r["prob_old"] - (100 if r["win"] else 0)) for r in records) / total
    print(f"旧 probability 平均绝对误差 (MAE): {mae:.1f}%")

    return 0


if __name__ == "__main__":
    sys.exit(main())