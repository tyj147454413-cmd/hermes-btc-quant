#!/usr/bin/env python3
"""
测试贴线模式下禁用 order_flow 对胜率的影响。

用法：python3 scripts/test_tie_orderflow.py
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

    # 基线
    base_wins = {"tie": 0, "normal": 0, "all": 0}
    base_total = {"tie": 0, "normal": 0, "all": 0}

    # 贴线禁用 order_flow
    test_wins = {"tie": 0, "normal": 0, "all": 0}
    test_total = {"tie": 0, "normal": 0, "all": 0}

    changed = []  # 记录方向变化的场次

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

        base_dir = bd.final_direction
        base_win = base_dir == actual
        base_wins["all"] += base_win
        base_total["all"] += 1
        if is_tie:
            base_wins["tie"] += base_win
            base_total["tie"] += 1
        else:
            base_wins["normal"] += base_win
            base_total["normal"] += 1

        # 测试：贴线模式下禁用 order_flow
        tf = dict(factors)
        if is_tie and tf.get("order_flow", 0) != 0:
            tf["order_flow"] = 0  # 禁用

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

        test_dir = td.final_direction
        test_win = test_dir == actual
        test_wins["all"] += test_win
        test_total["all"] += 1
        if is_tie:
            test_wins["tie"] += test_win
            test_total["tie"] += 1
        else:
            test_wins["normal"] += test_win
            test_total["normal"] += 1

        if base_dir != test_dir:
            changed.append({
                "slug": slug,
                "base_dir": base_dir,
                "test_dir": test_dir,
                "actual": actual,
                "base_win": base_win,
                "test_win": test_win,
                "order_flow": factors.get("order_flow", 0),
            })

    print("## 贴线模式禁用 order_flow 测试\n")
    print("| 分组 | 基线胜率 | 测试胜率 | 变化 |")
    print("|------|---------|---------|------|")
    for key in ["tie", "normal", "all"]:
        br = base_wins[key] / base_total[key] * 100 if base_total[key] else 0
        tr = test_wins[key] / test_total[key] * 100 if test_total[key] else 0
        diff = tr - br
        print(f"| {key} | {br:.1f}% ({base_wins[key]}/{base_total[key]}) | {tr:.1f}% ({test_wins[key]}/{test_total[key]}) | {diff:+.1f}% |")

    print(f"\n方向变化: {len(changed)} 场")
    for c in changed:
        status = "✅ 变对" if c["test_win"] and not c["base_win"] else ("❌ 变错" if not c["test_win"] and c["base_win"] else "➡️ 不变")
        print(f"  {status} {c['slug']}: {c['base_dir']}→{c['test_dir']} (实际 {c['actual']}, order_flow={c['order_flow']:+.0f})")

    return 0


if __name__ == "__main__":
    sys.exit(main())