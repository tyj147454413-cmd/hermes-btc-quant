#!/usr/bin/env python3
"""
测试低效因子变更对胜率的影响。

对每个变更方案，用回测数据跑一遍 judge()，对比原始胜率。
输出每个方案的胜率变化，决定哪些变更值得做。

用法：python3 scripts/test_factor_changes.py
"""

from __future__ import annotations

import copy
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from signal_validator import extract_factors
from rule_engine import judge, _round_half_up, _dir_of, _sign

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


def run_scenario(name: str, modify_fn) -> tuple[int, int, float]:
    """运行一个变更方案，返回 (胜, 负, 胜率%)。modify_fn 修改 factors dict。"""
    results = load_results()
    slugs = get_latest_snapshots()
    wins = 0
    losses = 0

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

        # 应用因子变更
        modify_fn(factors)

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

        if d.final_direction == actual:
            wins += 1
        else:
            losses += 1

    total = wins + losses
    return wins, losses, wins / total * 100 if total > 0 else 0


def baseline() -> tuple[int, int, float]:
    """无变更基线。"""
    return run_scenario("基线（无变更）", lambda f: None)


def remove_factor(key: str):
    """移除指定因子。"""
    def fn(factors):
        factors.pop(key, None)
    return fn


def halve_factor(key: str):
    """降权指定因子 ×0.5。"""
    def fn(factors):
        v = factors.get(key, 0)
        if v != 0:
            factors[key] = _round_half_up(v * 0.5)
    return fn


def main() -> int:
    print("正在运行基线...")
    b_w, b_l, b_rate = baseline()
    print(f"基线: {b_w}胜/{b_l}负, 胜率 {b_rate:.1f}%\n")

    scenarios = [
        ("移除 taker_1h", remove_factor("taker_1h")),
        ("移除 macd_4h", remove_factor("macd_4h")),
        ("移除 seasonality", remove_factor("seasonality")),
        ("taker_bs 降权 ×2→×1", halve_factor("taker_bs")),
        ("移除 taker_1h + macd_4h", lambda f: (f.pop("taker_1h", None), f.pop("macd_4h", None))),
        ("移除 taker_1h + macd_4h + seasonality", lambda f: (f.pop("taker_1h", None), f.pop("macd_4h", None), f.pop("seasonality", None))),
        ("全部四项（移除3 + taker_bs降权）", lambda f: (
            f.pop("taker_1h", None), f.pop("macd_4h", None), f.pop("seasonality", None),
            f.update({"taker_bs": _round_half_up(f.get("taker_bs", 0) * 0.5)}) if f.get("taker_bs", 0) != 0 else None
        )),
    ]

    print("| 变更方案 | 胜 | 负 | 胜率 | vs基线 | 判定 |")
    print("|---------|-----|-----|------|--------|------|")
    for name, fn in scenarios:
        w, l, rate = run_scenario(name, fn)
        diff = rate - b_rate
        verdict = "✅ 提升" if diff > 0.5 else ("⚠️ 持平" if abs(diff) <= 0.5 else "❌ 下降")
        print(f"| {name} | {w} | {l} | {rate:.1f}% | {diff:+.1f}% | {verdict} |")

    return 0


if __name__ == "__main__":
    sys.exit(main())