#!/usr/bin/env python3
"""
贴线模式深度分析：因子构成、胜率分布、可行增强方向。

用法：python3 scripts/analyze_tie_line.py
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

    # 收集贴线模式的详细数据
    tie_records: list[dict] = []
    normal_records: list[dict] = []

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
        dev = float(inp.get("deviation_pct", 0) or 0)

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

        record = {
            "slug": slug,
            "actual": actual,
            "direction": d.final_direction,
            "win": d.final_direction == actual,
            "s0": d.s0,
            "net": d.net,
            "dev": dev,
            "confidence": d.confidence,
            "factors": {k: v for k, v in factors.items()},
            "n_factors": len([v for v in factors.values() if v != 0]),
            "mode": mode,
            "regime": inp.get("regime", "?"),
        }

        if is_tie:
            tie_records.append(record)
        else:
            normal_records.append(record)

    # === 贴线模式因子分析 ===
    print("## 贴线模式 vs 常规模式\n")
    print(f"贴线模式: {len(tie_records)} 场, 胜率 {sum(1 for r in tie_records if r['win'])/len(tie_records)*100:.1f}%")
    print(f"常规模式: {len(normal_records)} 场, 胜率 {sum(1 for r in normal_records if r['win'])/len(normal_records)*100:.1f}%")

    print(f"\n贴线模式平均活跃因子: {sum(r['n_factors'] for r in tie_records)/len(tie_records):.1f}")
    print(f"常规模式平均活跃因子: {sum(r['n_factors'] for r in normal_records)/len(normal_records):.1f}")

    # === 贴线模式因子出现频率 ===
    print("\n## 贴线模式各因子出现频率与方向一致率\n")
    print("| 因子 | 出现次数 | 出现率 | 方向一致率 |")
    print("|------|---------|--------|----------|")
    factor_names = set()
    for r in tie_records:
        factor_names.update(r["factors"].keys())
    for fname in sorted(factor_names):
        count = sum(1 for r in tie_records if r["factors"].get(fname, 0) != 0)
        if count == 0:
            continue
        rate = count / len(tie_records) * 100
        # 方向一致率
        correct = 0
        for r in tie_records:
            v = r["factors"].get(fname, 0)
            if v == 0:
                continue
            factor_dir = "UP" if v > 0 else "DOWN"
            if factor_dir == r["actual"]:
                correct += 1
        consistency = correct / count * 100 if count > 0 else 0
        print(f"| {fname} | {count} | {rate:.1f}% | {consistency:.1f}% |")

    # === 贴线模式按 regime 分桶 ===
    print("\n## 贴线模式按 regime 分桶\n")
    print("| Regime | 场次 | 胜率 | S0均值 | 活跃因子 |")
    print("|--------|------|------|--------|---------|")
    for regime in ["摆动市", "趋势市"]:
        subset = [r for r in tie_records if r["regime"] == regime]
        if not subset:
            continue
        wins = sum(1 for r in subset if r["win"])
        s0_avg = sum(abs(r["s0"]) for r in subset) / len(subset)
        nf_avg = sum(r["n_factors"] for r in subset) / len(subset)
        print(f"| {regime} | {len(subset)} | {wins/len(subset)*100:.1f}% | {s0_avg:.1f} | {nf_avg:.1f} |")

    # === 贴线模式下 S0 方向与胜率 ===
    print("\n## 贴线模式 S0 方向 → 最终方向 → 胜率\n")
    s0_up = [r for r in tie_records if r["s0"] > 0]
    s0_down = [r for r in tie_records if r["s0"] < 0]
    s0_zero = [r for r in tie_records if r["s0"] == 0]

    for label, subset in [("S0>0", s0_up), ("S0<0", s0_down), ("S0=0", s0_zero)]:
        if not subset:
            continue
        wins = sum(1 for r in subset if r["win"])
        # 最终方向一致率
        final_up = sum(1 for r in subset if r["direction"] == "UP")
        print(f"{label}: {len(subset)} 场, 胜率 {wins/len(subset)*100:.1f}%, "
              f"最终UP={final_up} DOWN={len(subset)-final_up}")

    # === 贴线模式下可用的新因子候选 ===
    print("\n## 贴线增强可行方向\n")
    print("当前贴线专列因子: wall_divergence (50.0%一致率), wall_settlement (0触发)")
    print("缺失因子方向:")
    print("1. 订单簿深度不平衡: buy/sell depth ratio 在贴线时的信号价值")
    print("2. 微观价差: bid-ask spread 变化率")
    print("3. 1m 动量: 贴线时 1m 价格变动方向（当前贴线禁用 1m/5m 因子）")
    print("4. 成交量异常: 贴线时成交量放大 = 庄家突破前兆")

    return 0


if __name__ == "__main__":
    sys.exit(main())