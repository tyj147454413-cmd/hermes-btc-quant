#!/usr/bin/env python3
"""
概率表四维重跑（round41.2 审计 Fix 4，2026-08-22）。

背景：_probability 0-2 档"贴线+逆PM"格 82.1%"高"为数学不可能值
（全量高于全部子组 67.6/60.0/77.8），疑为旧统计遗留。本脚本用与
calibrate_probability.py 相同的样本匹配逻辑（snapshots + results +
当前引擎复算），按 |净分|×贴线×PM×Regime 四维输出真实胜率与样本数，
并与 rule_engine._probability 现行 12 格 diff。

注意：引擎已同步 round40（G3 不硬覆盖）+ round41（宏观趋势族进 S₀），
本重跑结果为新口径下的四维胜率，直接作为回填依据。

用法：python3 scripts/probability_table_recheck.py
输出：stdout 全表 + data/hermes_probability_table_recheck_20260822.md
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from signal_validator import extract_factors
from rule_engine import judge, _probability

SNAPSHOT_DIR = Path("/Users/tangyijun/Desktop/hermes btc/data/snapshots")
RESULTS_FILE = Path("/Users/tangyijun/Desktop/hermes btc/data/hermes_results.jsonl")
OUT_MD = Path("/Users/tangyijun/Desktop/hermes btc/data/hermes_probability_table_recheck_20260822.md")


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


def net_bucket(an: float) -> str:
    if an >= 6:
        return "6+"
    if an >= 3:
        return "3-5"
    return "0-2"


def main() -> int:
    results = load_results()
    slugs = get_latest_snapshots()

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

        records.append({
            "slug": slug,
            "net": d.net,
            "bucket": net_bucket(abs(d.net)),
            "tie": bool(inp.get("tie_line_active")),
            # 引擎口径（rule_engine 244 行）：pm_up>50→UP / <50→DOWN / =50→None(falsy)
            "pm_same": bool(inp.get("pm_up") is not None and d.final_direction == ("UP" if inp["pm_up"] > 50 else ("DOWN" if inp["pm_up"] < 50 else ""))),
            "regime": inp.get("regime") or "?",
            "win": d.final_direction == actual,
            "sort_key": slug,
        })

    # 与引擎 _probability 的 pm_same 口径对齐：pm_same = 最终方向与 PM 主方向同向
    # （引擎 629 行 pm_same = pm_dir and direction == pm_dir，pm_dir 按 pm_up>=50 侧）
    # 上面构造时已按该口径记录。

    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in records:
        groups[(r["bucket"], r["tie"], r["pm_same"], r["regime"])].append(r)

    # 粗粒度：|净分|档全量 / 三维（不含 regime）
    by_bucket: dict[str, list[dict]] = defaultdict(list)
    by_3d: dict[tuple, list[dict]] = defaultdict(list)
    for r in records:
        by_bucket[r["bucket"]].append(r)
        by_3d[(r["bucket"], r["tie"], r["pm_same"])].append(r)

    lines: list[str] = []
    lines.append("# 概率表四维重跑（round41.2 审计 Fix 4）")
    lines.append("")
    lines.append(f"日期：2026-08-22 | 样本：{len(records)} 场（snapshots×results 实盘匹配，当前引擎 round41.2 口径复算）")
    lines.append("")
    lines.append("## 一、四维细粒度（|净分|×贴线×PM×Regime）")
    lines.append("")
    lines.append("| |净分|档 | 贴线 | PM同向 | Regime | 场次 | 胜 | 负 | 胜率 | 现行表值 | 差异 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")

    bucket_order = ["0-2", "3-5", "6+"]
    diffs: list[str] = []
    for b in bucket_order:
        for tie in (False, True):
            for pm in (False, True):
                for reg in ("趋势市", "摆动市"):
                    g = groups.get((b, tie, pm, reg), [])
                    n = len(g)
                    if n == 0:
                        continue
                    w = sum(1 for r in g if r["win"])
                    rate = w / n * 100
                    cur_p, cur_c = _probability(
                        {"0-2": 1.0, "3-5": 4.0, "6+": 7.0}[b] * (1 if records[0]["net"] >= 0 else -1),
                        tie, pm, reg)
                    flag = ""
                    if abs(rate - cur_p) > 5:
                        flag = " ⚠️"
                        diffs.append(f"{b} 贴线={tie} PM同向={pm} {reg}: 重跑 {rate:.1f}% (n={n}) vs 现行 {cur_p}%{flag}")
                    lines.append(f"| {b} | {tie} | {pm} | {reg} | {n} | {w} | {n-w} | {rate:.1f}% | {cur_p}% | {rate-cur_p:+.1f}pp{flag} |")

    lines.append("")
    lines.append("## 二、三维聚合（|净分|×贴线×PM，引擎现行查表粒度）")
    lines.append("")
    lines.append("| |净分|档 | 贴线 | PM同向 | 场次 | 胜率 | 现行表值 | 差异 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for b in bucket_order:
        for tie in (False, True):
            for pm in (False, True):
                g = by_3d.get((b, tie, pm), [])
                n = len(g)
                if n == 0:
                    continue
                w = sum(1 for r in g if r["win"])
                rate = w / n * 100
                cur_p, _ = _probability(
                    {"0-2": 1.0, "3-5": 4.0, "6+": 7.0}[b] * (1 if records[0]["net"] >= 0 else -1),
                    tie, pm, "趋势市")
                lines.append(f"| {b} | {tie} | {pm} | {n} | {rate:.1f}% | {cur_p}% | {rate-cur_p:+.1f}pp |")

    lines.append("")
    lines.append("## 三、|净分|档全量")
    lines.append("")
    lines.append("| |净分|档 | 场次 | 胜率 |")
    lines.append("|---|---|---|---|")
    for b in bucket_order:
        g = by_bucket.get(b, [])
        n = len(g)
        if n == 0:
            continue
        w = sum(1 for r in g if r["win"])
        lines.append(f"| {b} | {n} | {w/n*100:.1f}% |")

    lines.append("")
    lines.append("## 四、矛盾格专项：0-2 贴线+逆PM")
    g = by_3d.get(("0-2", True, False), [])
    n = len(g)
    if n:
        w = sum(1 for r in g if r["win"])
        lines.append("")
        lines.append(f"- 三维真实样本：{n} 场，胜率 {w/n*100:.1f}%（现行表值 82.1%\"高\"——数学不可能值）")
        if n < 5:
            lines.append(f"- 样本 <5：按保守回退策略，取同档已知子组最低胜率（见下）")
        sub_rates = []
        for tie in (False, True):
            for pm in (False, True):
                gg = by_3d.get(("0-2", tie, pm), [])
                if len(gg) >= 5:
                    ww = sum(1 for r in gg if r["win"])
                    sub_rates.append((len(gg), ww/len(gg)*100))
        if sub_rates:
            lines.append("- 同档已知子组（n≥5）：" + "，".join(f"n={n_} {r:.1f}%" for n_, r in sub_rates))
            lines.append(f"- 保守回退建议：min(子组) = {min(r for _, r in sub_rates):.1f}%，置信度按三档定义映射（<70% → 低）")
    else:
        lines.append("")
        lines.append("- 三维 0-2 贴线+逆PM 样本 0 场：按保守回退策略处理")

    lines.append("")
    lines.append("## 五、与现行表差异汇总（|Δ|>5pp）")
    lines.append("")
    if diffs:
        lines.extend(f"- {x}" for x in diffs)
    else:
        lines.append("- 无 >5pp 差异")

    # 时间切分稳定性：三维格子前半 vs 后半（检测近期行情混杂 vs 结构参数）
    lines.append("")
    lines.append("## 六、时间切分稳定性（三维格子，前半 vs 后半）")
    lines.append("")
    lines.append("| |净分|档 | 贴线 | PM同向 | n | 前半胜率 | 后半胜率 | 稳定性 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    recs_sorted = sorted(records, key=lambda r: r["sort_key"])
    half = len(recs_sorted) // 2
    for b in bucket_order:
        for tie in (False, True):
            for pm in (False, True):
                g_all = [r for r in recs_sorted if (r["bucket"], r["tie"], r["pm_same"]) == (b, tie, pm)]
                if len(g_all) < 6:
                    continue
                first, second = g_all[:len(g_all)//2], g_all[len(g_all)//2:]
                r1 = sum(1 for r in first if r["win"]) / len(first) * 100
                r2 = sum(1 for r in second if r["win"]) / len(second) * 100
                stable = "稳定" if abs(r1 - r2) <= 15 else "⚠️ 漂移"
                lines.append(f"| {b} | {tie} | {pm} | {len(g_all)} | {r1:.1f}% | {r2:.1f}% | {stable} |")

    out = "\n".join(lines)
    print(out)
    OUT_MD.write_text(out + "\n", encoding="utf-8")
    print(f"\n已写入 {OUT_MD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
