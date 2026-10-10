#!/usr/bin/env python3
"""
G3 口径回测量化（round41.3 A 方案验证，2026-08-22）。

方法：240 场实盘匹配样本全量重放当前引擎（round41.3 A 方案：证据不全翻向 PM），
取 trace 含"证据不全"的场，比较两口径的实际胜负：
- A 方案（round41.3，当前引擎）：最终方向（G3 硬覆盖翻向 PM 后经 G4/G5）
- round40 口径（已回退）：保留 S₀ 方向（近似：净分方向 d.net_direction；
  G4/G5 后续层影响以两口径方向一致的场剔除后近似为零）

历史：round41.2 版本（round40 口径引擎）曾以同方法论得出"翻向 PM 75% vs 保留 S₀ 25%"，
是 A 方案的决策依据。本版为 A 方案落地后的验证性重跑。
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, "/Users/tangyijun/Desktop/hermes btc/scripts")
from signal_validator import extract_factors
from rule_engine import judge

SNAPSHOT_DIR = Path("/Users/tangyijun/Desktop/hermes btc/data/snapshots")
RESULTS_FILE = Path("/Users/tangyijun/Desktop/hermes btc/data/hermes_results.jsonl")
OUT_MD = Path("/Users/tangyijun/Desktop/hermes btc/data/hermes_g3_round40_replay_20260822.md")


def load_results() -> dict[str, str]:
    results: dict[str, str] = {}
    if RESULTS_FILE.exists():
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

    total = 0
    g3_fields: list[dict] = []
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
        if prelim.final_direction == "DOWN":
            extreme_wall = bool(sell_wall is not None and sell_wall > 3.0)
        elif prelim.final_direction == "UP":
            extreme_wall = bool(buy_wall is not None and buy_wall > 3.0)
        else:
            extreme_wall = False

        d = judge(dict(inp, extreme_wall=extreme_wall,
                       _buy_wall_ratio=buy_wall, _sell_wall_ratio=sell_wall,
                       taker_bs_ratio=taker_bs))
        if not d.final_direction:
            continue
        total += 1

        # G3 证据不全场（A 方案分歧场）：trace 含"证据不全"（宽松匹配，兼容 round40/41.3 字样）
        if any("证据不全" in t for t in d.trace):
            pm_up = inp.get("pm_up")
            pm_dir = "UP" if pm_up > 50 else "DOWN"
            g3_fields.append({
                "slug": slug,
                "actual": actual,
                "new_dir": d.final_direction,        # A 方案（round41.3 当前引擎）
                "old_dir": d.net_direction,          # round40 口径（保留 S₀，近似净分方向）
                "pm_dir": pm_dir,
                "pm_side": max(pm_up, 100 - pm_up),
                "net": d.net,
            })

    lines = ["# G3 口径回测量化（round41.3 A 方案验证）", ""]
    lines.append(f"日期：2026-08-22 | 样本：{total} 场全量重放（当前引擎 round41.3 A 方案）")
    lines.append("")

    if not g3_fields:
        lines.append("无 G3 证据不全分歧场（重放期内 G3 触发场均为强证据/豁免/价格门槛拦截路径）。")
    else:
        n = len(g3_fields)
        new_win = sum(1 for f in g3_fields if f["new_dir"] == f["actual"])
        old_win = sum(1 for f in g3_fields if f["old_dir"] == f["actual"])
        # 新旧方向一致的场（G4/G5 后续层又改回 PM 方向）不计入分歧
        diff_fields = [f for f in g3_fields if f["new_dir"] != f["old_dir"]]
        nd = len(diff_fields)
        nd_new_win = sum(1 for f in diff_fields if f["new_dir"] == f["actual"])
        nd_old_win = sum(1 for f in diff_fields if f["old_dir"] == f["actual"])

        lines.append(f"## G3 触发场（证据不全路径）：{n} 场")
        lines.append("")
        lines.append(f"- A 方案（round41.3 翻向 PM，当前引擎）胜：{new_win}/{n} = {new_win/n*100:.1f}%")
        lines.append(f"- round40 口径（保留 S₀，已回退）胜：{old_win}/{n} = {old_win/n*100:.1f}%")
        lines.append("")
        lines.append(f"## 剔除后续层同向场后（真正方向分歧）：{nd} 场")
        lines.append("")
        if nd:
            lines.append(f"- 新口径胜：{nd_new_win}/{nd} = {nd_new_win/nd*100:.1f}%")
            lines.append(f"- 旧口径胜：{nd_old_win}/{nd} = {nd_old_win/nd*100:.1f}%")
            lines.append(f"- 净差：{nd_new_win - nd_old_win:+d} 场（新口径{'优' if nd_new_win > nd_old_win else ('劣' if nd_new_win < nd_old_win else '平')}）")
            lines.append("")
            lines.append("### 分歧场明细")
            lines.append("")
            lines.append("> round40 方向以净分方向近似（G4/G5 后续修正未计入）；round41.2 版回测的严格口径（12 场真分歧场：翻 PM 75% vs 保留 S₀ 25%）为 A 方案决策依据。")
            lines.append("")
            lines.append("| slug | PM共识 | A方案方向 | round40方向 | 实际 | A方案 | round40 |")
            lines.append("|---|---|---|---|---|---|---|")
            for f in diff_fields:
                lines.append(f"| {f['slug'][:48]} | {f['pm_side']:.0f}% {f['pm_dir']} | {f['new_dir']} | {f['old_dir']} | {f['actual']} | {'✅' if f['new_dir'] == f['actual'] else '❌'} | {'✅' if f['old_dir'] == f['actual'] else '❌'} |")

    out = "\n".join(lines)
    print(out)
    OUT_MD.write_text(out + "\n", encoding="utf-8")
    print(f"\n已写入 {OUT_MD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
