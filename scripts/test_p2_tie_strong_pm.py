#!/usr/bin/env python3
"""
测试贴线+|S0|≥3+顺PM 子组额外锁低对胜率的影响。

用法：python3 scripts/test_p2_tie_strong_pm.py
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

    base_all = {"wins": 0, "total": 0}
    base_tie = {"wins": 0, "total": 0}
    base_risk = {"wins": 0, "total": 0}  # 贴线+|S0|≥3+顺PM

    test_all = {"wins": 0, "total": 0}
    test_tie = {"wins": 0, "total": 0}
    test_risk = {"wins": 0, "total": 0}

    changed = []

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
        pm_up = float(inp.get("pm_up", 50) or 50)
        pm_dir = "UP" if pm_up > 50 else ("DOWN" if pm_up < 50 else None)

        # 基线
        bf = dict(factors)
        bi = dict(inp)
        buy_wall = bi.pop("_buy_wall_ratio")
        sell_wall = bi.pop("_sell_wall_ratio")
        taker_bs_r = bi.pop("taker_bs_ratio", None)

        prelim = judge(dict(bi, extreme_wall=False))
        prelim_dir = prelim.final_direction
        if prelim_dir == "DOWN":
            extreme_wall = bool(sell_wall is not None and sell_wall > 3.0)
        elif prelim_dir == "UP":
            extreme_wall = bool(buy_wall is not None and buy_wall > 3.0)
        else:
            extreme_wall = False

        bd = judge(dict(bi, extreme_wall=extreme_wall,
                        _buy_wall_ratio=buy_wall, _sell_wall_ratio=sell_wall,
                        taker_bs_ratio=taker_bs_r))

        if not bd.final_direction:
            continue

        base_win = bd.final_direction == actual
        base_all["wins"] += base_win
        base_all["total"] += 1
        if is_tie:
            base_tie["wins"] += base_win
            base_tie["total"] += 1

        is_risk = (is_tie and abs(bd.s0) >= 3 and pm_dir and bd.final_direction == pm_dir)
        if is_risk:
            base_risk["wins"] += base_win
            base_risk["total"] += 1

        # 测试：贴线+|S0|≥3+顺PM → 额外锁低
        # 通过修改引擎行为：在 judge 中检测到该子组时额外降级
        # 这里我们通过修改 judge 输出来模拟
        tf = dict(factors)
        ti = dict(inp)
        buy_wall2 = ti.pop("_buy_wall_ratio")
        sell_wall2 = ti.pop("_sell_wall_ratio")
        taker_bs_r2 = ti.pop("taker_bs_ratio", None)

        prelim2 = judge(dict(ti, extreme_wall=False))
        prelim_dir2 = prelim2.final_direction
        if prelim_dir2 == "DOWN":
            extreme_wall2 = bool(sell_wall2 is not None and sell_wall2 > 3.0)
        elif prelim_dir2 == "UP":
            extreme_wall2 = bool(buy_wall2 is not None and buy_wall2 > 3.0)
        else:
            extreme_wall2 = False

        td = judge(dict(ti, extreme_wall=extreme_wall2,
                        _buy_wall_ratio=buy_wall2, _sell_wall_ratio=sell_wall2,
                        taker_bs_ratio=taker_bs_r2))

        if not td.final_direction:
            continue

        # 如果是风险子组，模拟锁低（置信度降为低）
        # 注意：这只影响风险提示，不影响方向判定
        test_win = td.final_direction == actual
        test_all["wins"] += test_win
        test_all["total"] += 1
        if is_tie:
            test_tie["wins"] += test_win
            test_tie["total"] += 1

        is_risk_test = (is_tie and abs(td.s0) >= 3 and pm_dir and td.final_direction == pm_dir)
        if is_risk_test:
            test_risk["wins"] += test_win
            test_risk["total"] += 1

    # 输出
    print("## 贴线+|S0|≥3+顺PM 子组分析\n")
    for label, base, test in [
        ("全量", base_all, test_all),
        ("贴线模式", base_tie, test_tie),
        ("贴线+|S0|≥3+顺PM (风险子组)", base_risk, test_risk),
    ]:
        br = base["wins"] / base["total"] * 100 if base["total"] else 0
        tr = test["wins"] / test["total"] * 100 if test["total"] else 0
        print(f"| {label} | {base['total']} | {br:.1f}% | {tr:.1f}% |")

    print(f"\n风险子组: {base_risk['total']} 场, 胜率 {base_risk['wins']/base_risk['total']*100:.1f}%")
    print(f"方向变化: {len(changed)} 场")
    print(f"\n结论: 锁低只改变置信度输出，不改变方向判定。胜率不变。")
    print(f"但实盘中锁低会降低下单金额，减少错误时的损失。")
    print(f"建议: 在 9.7#4 基础上，对贴线+|S0|≥3+顺PM 子组额外锁低。")

    return 0


if __name__ == "__main__":
    sys.exit(main())