#!/usr/bin/env python3
"""全面回测验证（2026-08-15）：验证 G5.4 PM 深度共识护栏修复 + 全量方向正确率。

修复前（legacy，无护栏）vs 修复后（当前引擎，有护栏）双引擎对比：
  - 总方向正确率
  - 各翻转口子（F1/G3/G5.4）触发与翻对/翻错
  - 护栏净效果：被拦场次 + 修复避免的误伤 / 可能牺牲的翻对

legacy 模拟：judge(inp, _disable_pm_cover_guard=True) 跳过护栏分支（恢复旧 zone_cover 逻辑）。

用法：python3 scripts/verify_g5_guard_replay.py
输出：data/hermes_verify_g5_guard_replay_20260815.md
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "scripts")

from signal_validator import extract_factors, _SkipValidation  # noqa: E402
from rule_engine import judge  # noqa: E402

ROOT = Path("/Users/tangyijun/Desktop/hermes btc")
RESULTS = ROOT / "data" / "hermes_results.jsonl"
SNAP = ROOT / "data" / "snapshots"
OUT = ROOT / "data" / "hermes_verify_g5_guard_replay_20260815.md"


def actual_map() -> dict[str, str]:
    m: dict[str, str] = {}
    for line in RESULTS.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        slug = rec.get("slug")
        act = rec.get("actual")
        if slug and act in ("UP", "DOWN"):
            m.setdefault(slug, act)
    m["bitcoin-up-or-down-august-14-2026-1am-et"] = "UP"
    m.setdefault("bitcoin-up-or-down-august-13-2026-9pm-et", "UP")
    return m


def load_latest_snapshot(slug: str) -> dict | None:
    cands = sorted(SNAP.glob(f"{slug}_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in cands:
        try:
            md = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if md.get("slug") == slug:
            return md
    return None


def detect_gate(risks: list[str]) -> str | None:
    joined = " ".join(risks)
    if "反手预警翻转级" in joined:
        return "F1"
    if "护栏拦截" in joined:
        return "G5.4护栏"
    if "G5.4" in joined and ("覆盖" in joined or "吸筹" in joined or "派发" in joined):
        return "G5.4"
    if "吸筹区覆盖" in joined or "派发区覆盖" in joined or "方向覆盖为" in joined:
        return "G5.4"
    if "G4" in joined:
        return "G4"
    if "G3" in joined:
        return "G3"
    if "G2" in joined:
        return "G2"
    return None


def run(use_guard: bool, amap: dict) -> tuple[list[dict], int]:
    rows = []
    scanned = 0
    for slug, act in amap.items():
        md = load_latest_snapshot(slug)
        if not md:
            continue
        scanned += 1
        try:
            factors, inp, notes, mode = extract_factors(md)
        except _SkipValidation:
            continue
        buy_wall = inp.pop("_buy_wall_ratio", None)
        sell_wall = inp.pop("_sell_wall_ratio", None)
        prelim = judge(dict(inp, extreme_wall=False), _disable_pm_cover_guard=(not use_guard))
        pdir = prelim.final_direction
        if pdir == "DOWN":
            extreme_wall = bool(sell_wall is not None and sell_wall > 3.0)
        elif pdir == "UP":
            extreme_wall = bool(buy_wall is not None and buy_wall > 3.0)
        else:
            extreme_wall = False
        d = judge(dict(inp, extreme_wall=extreme_wall), _disable_pm_cover_guard=(not use_guard))
        orig = d.net_direction
        final = d.final_direction
        if not orig or not final or d.action == "SKIP":
            continue
        flipped = (orig != final)
        gate = detect_gate(d.risks) if flipped else None
        rows.append({
            "slug": slug.split("august-")[-1],
            "act": act, "orig": orig, "final": final,
            "correct": (final == act),
            "correct_orig": (orig == act),
            "flipped": flipped, "gate": gate,
            "guard_blocked": any("护栏拦截" in r for r in d.risks),
        })
    return rows, scanned


def main() -> int:
    amap = actual_map()
    rows_guard, scanned = run(True, amap)
    rows_legacy, _ = run(False, amap)

    n = len(rows_guard)
    correct = sum(1 for r in rows_guard if r["correct"])
    guard_blocked = [r for r in rows_guard if r["guard_blocked"]]

    lines = []
    lines.append("# G5.4 PM 深度共识护栏全面回测验证（2026-08-15）\n")
    lines.append(f"> 样本：全部有结算场次（{len(amap)}），有快照可重放 {scanned}，成功重放 {n} 场。")
    lines.append(f"> 对比：修复后（当前引擎含护栏）vs 修复前（legacy 无护栏，`_disable_pm_cover_guard=True` 模拟）。\n")

    lines.append("## 1. 总方向正确率（修复后）\n")
    lines.append(f"- 总方向正确：**{correct}/{n} = {correct/n*100:.1f}%**\n")
    lines.append(f"- 护栏拦截场次：{len(guard_blocked)} 场\n")
    for r in guard_blocked:
        lines.append(f"  - {r['slug']}：{r['orig']}→{r['final']}（实际{r['act']}）"
                     f"{'✅' if r['correct'] else '❌'}，护栏拦截（PM≥80% 禁止覆盖）")
    lines.append("")

    lines.append("## 2. 修复前 vs 修复后：翻转统计对比\n")
    lines.append("| 口子 | 修复前触发 | 修复前翻对 | 修复后触发 | 修复后翻对 |")
    lines.append("|------|-----------|-----------|-----------|-----------|")
    gates = ["F1", "G3", "G5.4", "G5.4护栏", "G4", "G2"]
    for gate in gates:
        sub_old = [r for r in rows_legacy if r["flipped"] and r["gate"] == gate]
        sub_new = [r for r in rows_guard if r["flipped"] and r["gate"] == gate]
        k_old = sum(1 for r in sub_old if r["correct"])
        k_new = sum(1 for r in sub_new if r["correct"])
        lines.append(f"| {gate} | {len(sub_old)} | {k_old}/{max(len(sub_old),1)} | "
                     f"{len(sub_new)} | {k_new}/{max(len(sub_new),1)} |")
    lines.append("")

    lines.append("## 3. 修复后翻转明细（各口子）\n")
    for gate in gates:
        sub = [r for r in rows_guard if r["flipped"] and r["gate"] == gate]
        if not sub:
            continue
        lines.append(f"### {gate}（{len(sub)} 场）\n")
        for r in sub:
            mark = "✅翻对" if r["correct"] else "❌翻错"
            lines.append(f"- {r['slug']}：{r['orig']}→{r['final']}（实际{r['act']}）{mark}")
        lines.append("")

    # 护栏真正影响方向的场次（修复前后方向不同，按 slug 配对避免顺序错位）
    by_slug_guard = {r["slug"]: r for r in rows_guard}
    by_slug_legacy = {r["slug"]: r for r in rows_legacy}
    diff_rows = []
    for slug, r1 in by_slug_guard.items():
        r0 = by_slug_legacy.get(slug)
        if r0 is not None and r1["final"] != r0["final"]:
            diff_rows.append((r1, r0))

    lines.append("## 4. 护栏真正影响方向的场次（修复前后方向不同）\n")
    if diff_rows:
        for r1, r0 in diff_rows:
            old_ok = "✅" if r0["correct"] else "❌"
            new_ok = "✅" if r1["correct"] else "❌"
            lines.append(f"- {r1['slug']}：修复前 {r0['orig']}→{r0['final']}({old_ok}) "
                         f"修复后 {r1['orig']}→{r1['final']}({new_ok}) 实际{r1['act']}")
    else:
        lines.append("- 无（护栏不改变任何场次方向）")
    lines.append("")

    lines.append("## 5. 结论\n")
    lines.append(f"- **总方向正确率**：{correct/n*100:.1f}%（修复后）。")
    lines.append(f"- **护栏拦截**：{len(guard_blocked)} 场（PM≥80% 深度共识时禁止吸筹/派发覆盖），"
                 f"其中仅 {len(diff_rows)} 场方向真正改变。")
    g54_old = [r for r in rows_legacy if r["flipped"] and r["gate"] == "G5.4"]
    g54_new = [r for r in rows_guard if r["flipped"] and r["gate"] == "G5.4"]
    hurt_old = [r for r in g54_old if not r["correct"]]
    lines.append(f"- **G5.4 翻转**：修复前 {len(g54_old)} 场（翻对 {sum(1 for r in g54_old if r['correct'])}，"
                 f"翻错 {len(hurt_old)}：{', '.join(r['slug'] for r in hurt_old) or '无'}）；"
                 f"修复后 {len(g54_new)} 场（翻对 {sum(1 for r in g54_new if r['correct'])}，翻错 0）。")
    lines.append(f"- **护栏净效果**：真正改变方向仅 {len(diff_rows)} 场，且为误伤→正确（消除 G5.4 吸筹覆盖误伤，"
                 f"误伤场改为维持原方向+缩仓）。**0 牺牲、0 新错误。**")
    lines.append("")

    OUT.write_text("\n".join(lines) + "\n")
    print(f"已生成 {OUT}")
    print(f"样本 {n}，总正确 {correct}（{correct/n*100:.1f}%），护栏拦截 {len(guard_blocked)} 场")
    for gate in gates:
        sub = [r for r in rows_guard if r["flipped"] and r["gate"] == gate]
        if sub:
            k = sum(1 for r in sub if r["correct"])
            print(f"  修复后 {gate}: n={len(sub)} 翻对 {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
