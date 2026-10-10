#!/usr/bin/env python3
"""
P2 优化分析：摆动市+贴线场景 & price_position 极端位。

用法：python3 scripts/analyze_p2.py
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
        regime = inp.get("regime", "?")
        dev = float(inp.get("deviation_pct", 0) or 0)
        pp = factors.get("price_position", 0)

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

        # pp_extreme: |price_position| >= 3 (极端位，×1 权重)
        pp_extreme = abs(pp) >= 3 and pp != 0

        # 摆动市 + 偏差<0.05% (贴线但被禁用)
        swing_near = regime == "摆动市" and abs(dev) < 0.05

        records.append({
            "slug": slug,
            "actual": actual,
            "direction": d.final_direction,
            "win": d.final_direction == actual,
            "s0": d.s0,
            "net": d.net,
            "is_tie": is_tie,
            "regime": regime,
            "dev": dev,
            "pp": pp,
            "pp_extreme": pp_extreme,
            "swing_near": swing_near,
            "confidence": d.confidence,
            "position_scale": d.position_scale,
        })

    total = len(records)
    total_wins = sum(1 for r in records if r["win"])
    print(f"全量: {total_wins}/{total} = {total_wins/total*100:.1f}%\n")

    # === P2-6: 摆动市+贴线场景 ===
    print("## P2-6: 摆动市+贴线场景\n")
    swing_near = [r for r in records if r["swing_near"]]
    swing_far = [r for r in records if r["regime"] == "摆动市" and not r["swing_near"]]
    trend_near = [r for r in records if r["regime"] == "趋势市" and abs(r["dev"]) < 0.05]
    trend_far = [r for r in records if r["regime"] == "趋势市" and abs(r["dev"]) >= 0.05]

    print("| 场景 | 场次 | 胜率 | 贴线激活 | S0均值 |")
    print("|------|------|------|---------|--------|")
    for label, subset in [
        ("摆动市+偏差<0.05% (贴线禁用)", swing_near),
        ("摆动市+偏差≥0.05%", swing_far),
        ("趋势市+偏差<0.05% (贴线激活)", trend_near),
        ("趋势市+偏差≥0.05%", trend_far),
    ]:
        if not subset:
            continue
        w = sum(1 for r in subset if r["win"])
        tie_pct = sum(1 for r in subset if r["is_tie"]) / len(subset) * 100
        s0_avg = sum(abs(r["s0"]) for r in subset) / len(subset)
        print(f"| {label} | {len(subset)} | {w/len(subset)*100:.1f}% | {tie_pct:.0f}% | {s0_avg:.1f} |")

    # 摆动市+贴线: 方向来源分析
    print(f"\n摆动市+偏差<0.05% 场次: {len(swing_near)}")
    if swing_near:
        # 这些场的因子活跃情况
        from collections import Counter
        pp_counts = Counter()
        for r in swing_near:
            pass  # We don't have factor breakdown here, just the S0

    # === P2-7: price_position 极端位 ===
    print("\n## P2-7: price_position 极端位\n")
    pp_ext = [r for r in records if r["pp_extreme"]]
    pp_non = [r for r in records if not r["pp_extreme"] and r["pp"] != 0]

    print("| 场景 | 场次 | 胜率 | S0均值 | 贴线率 |")
    print("|------|------|------|--------|--------|")
    for label, subset in [
        ("极端位 (|pp|≥3, ×1)", pp_ext),
        ("非极端位 (|pp|<3, ×2)", pp_non),
    ]:
        if not subset:
            continue
        w = sum(1 for r in subset if r["win"])
        tie_pct = sum(1 for r in subset if r["is_tie"]) / len(subset) * 100
        s0_avg = sum(abs(r["s0"]) for r in subset) / len(subset)
        print(f"| {label} | {len(subset)} | {w/len(subset)*100:.1f}% | {s0_avg:.1f} | {tie_pct:.0f}% |")

    # 交叉：极端位 + 贴线
    pp_ext_tie = [r for r in pp_ext if r["is_tie"]]
    pp_ext_normal = [r for r in pp_ext if not r["is_tie"]]
    print(f"\n极端位+贴线: {len(pp_ext_tie)}场, 胜率 {sum(1 for r in pp_ext_tie if r['win'])/len(pp_ext_tie)*100:.1f}%" if pp_ext_tie else "极端位+贴线: 0场")
    print(f"极端位+常规: {len(pp_ext_normal)}场, 胜率 {sum(1 for r in pp_ext_normal if r['win'])/len(pp_ext_normal)*100:.1f}%" if pp_ext_normal else "极端位+常规: 0场")

    # === 结论 ===
    print("\n## 结论\n")

    if swing_near:
        sw = sum(1 for r in swing_near if r["win"]) / len(swing_near) * 100
        print(f"P2-6 摆动市+偏差<0.05%: {len(swing_near)}场, 胜率 {sw:.1f}%")
        if sw < 70:
            print("  → 胜率偏低，建议优化")
        else:
            print("  → 胜率可接受，当前贴线禁用策略正确")

    if pp_ext:
        pe = sum(1 for r in pp_ext if r["win"]) / len(pp_ext) * 100
        pn = sum(1 for r in pp_non if r["win"]) / len(pp_non) * 100
        print(f"P2-7 极端位胜率 {pe:.1f}% vs 非极端位 {pn:.1f}% (差 {pe-pn:+.1f}%)")
        if pe < pn - 3:
            print("  → 极端位胜率明显偏低，建议额外降置信度")
        else:
            print("  → 差距不大，当前权重差异已足够")

    return 0


if __name__ == "__main__":
    sys.exit(main())