#!/usr/bin/env python3
"""现有 5 个翻转口子（G2/G3/G4/G5.4/9.7#7-F1）专项回测（2026-08-15）。

目标：回答用户"哪些翻转是对的、怎么优化"。重放全部有快照+结算的场次，从
rule_engine.judge() 对比 net_direction（初始）vs final_direction（最终），识别每次
翻转，再结合 risks 判断触发口子，逐个统计：
  - 触发次数 n
  - 翻转后方向正确率（点估计 + Wilson95 下界 vs 保本线 50%）
  - 基线（不翻转保持初始方向）正确率
  - 改善场数（初始错→翻对）/ 恶化场数（初始对→翻错/误伤）

用法：python3 scripts/flip_gates_replay.py
输出：data/hermes_flip_gates_replay_20260815.md
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, "scripts")

from rule_engine import judge  # noqa: E402
from signal_validator import extract_factors, _SkipValidation  # noqa: E402

ROOT = Path("/Users/tangyijun/Desktop/hermes btc")
RESULTS = ROOT / "data" / "hermes_results.jsonl"
SNAP = ROOT / "data" / "snapshots"
OUT = ROOT / "data" / "hermes_flip_gates_replay_20260815.md"

BREAKEVEN = 0.50
Z = 1.96


def wilson_lower(k: int, n: int, z: float = Z) -> float:
    if n <= 0:
        return 0.0
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - half)


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


# risks 中口子触发标记（按 risks 文本识别；优先级从特殊到一般）
def detect_gate(risks: list[str]) -> str | None:
    joined = " ".join(risks)
    if "反手预警翻转级" in joined:
        return "F1"
    if "G5.4" in joined and ("覆盖" in joined or "吸筹" in joined or "派发" in joined):
        return "G5.4"
    # G5.4 吸筹/派发覆盖 risk 文本不带 "G5.4" 前缀（"跌势衰竭+极端卖墙...方向覆盖为UP"）
    if "吸筹区覆盖" in joined or "派发区覆盖" in joined or "方向覆盖为" in joined:
        return "G5.4"
    if "G4" in joined:
        return "G4"
    if "G3" in joined:
        return "G3"
    if "G2" in joined:
        return "G2"
    return None


def main() -> int:
    amap = actual_map()
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
        # 复刻 signal_validator 的 extreme_wall 两遍注入
        buy_wall = inp.pop("_buy_wall_ratio", None)
        sell_wall = inp.pop("_sell_wall_ratio", None)
        prelim = judge(dict(inp, extreme_wall=False))
        pdir = prelim.final_direction
        if pdir == "DOWN":
            extreme_wall = bool(sell_wall is not None and sell_wall > 3.0)
        elif pdir == "UP":
            extreme_wall = bool(buy_wall is not None and buy_wall > 3.0)
        else:
            extreme_wall = False
        d = judge(dict(inp, extreme_wall=extreme_wall))
        orig = d.net_direction          # 初始方向（S0+PM 行后）
        final = d.final_direction       # 最终方向（含所有口子覆盖）
        if not orig or not final or d.action == "SKIP":
            continue
        flipped = (orig != final)
        gate = detect_gate(d.risks) if flipped else None
        if not flipped:
            continue
        # 仅统计"翻转类"口子（方向真的变了）。G2/G3/G4/G5.4/F1 都是 direction 赋值。
        correct_flip = (final == act)
        correct_orig = (orig == act)
        rows.append({
            "slug": slug.split("august-")[-1],
            "gate": gate or "未知",
            "act": act,
            "orig": orig,
            "final": final,
            "correct_flip": correct_flip,
            "correct_orig": correct_orig,
            "mode": mode,
            "risks": [r for r in d.risks if any(g in r for g in ("G2", "G3", "G4", "G5.4", "反手预警"))],
        })

    lines = []
    lines.append("# 现有 5 个翻转口子专项回测（2026-08-15）")
    lines.append("")
    lines.append(f"> 样本：全部有结算记录场次（{len(amap)}），有快照可重放 {scanned}，其中发生方向翻转 {len(rows)} 场。")
    lines.append("> 保本线 = 方向正确 50%；翻转胜率用点估计 + Wilson95 下界；n<30 不作充分依据。")
    lines.append("")

    # 分口子统计
    order = ["F1", "G5.4", "G4", "G3", "G2", "未知"]
    lines.append("## 各口子翻转统计\n")
    lines.append("| 口子 | 触发 n | 翻转正确率 | Wilson95下界 | 基线(不翻) | 改善(原错→翻对) | 恶化(原对→翻错) | 结论 |")
    lines.append("|------|--------|-----------|-------------|-----------|----------------|----------------|------|")
    total_flip_correct = 0
    total_flip_n = 0
    for gate in order:
        sub = [r for r in rows if r["gate"] == gate]
        n = len(sub)
        if n == 0:
            lines.append(f"| {gate} | 0 | - | - | - | - | - | 无样本 |")
            continue
        k = sum(1 for r in sub if r["correct_flip"])
        k_o = sum(1 for r in sub if r["correct_orig"])
        improve = sum(1 for r in sub if r["correct_flip"] and not r["correct_orig"])
        hurt = sum(1 for r in sub if not r["correct_flip"] and r["correct_orig"])
        wl = wilson_lower(k, n)
        total_flip_correct += k
        total_flip_n += n
        verdict = ("✅ 可靠（n≥30 且下界>50%）" if (n >= 30 and wl > BREAKEVEN)
                   else ("⚠️ 样本不足（n<30）" if n < 30 else "❌ 不可靠（下界≤50%）"))
        lines.append(f"| {gate} | {n} | {k}/{n}={k/n*100:.1f}% | {wl*100:.1f}% | "
                     f"{k_o}/{n}={k_o/n*100:.1f}% | {improve} | {hurt} | {verdict} |")
    if total_flip_n:
        wl_all = wilson_lower(total_flip_correct, total_flip_n)
        lines.append(f"| **合计** | {total_flip_n} | {total_flip_correct}/{total_flip_n}="
                     f"{total_flip_correct/total_flip_n*100:.1f}% | {wl_all*100:.1f}% | - | - | - | - |")
    lines.append("")

    # 逐场明细
    lines.append("## 逐场翻转明细\n")
    for gate in order:
        sub = [r for r in rows if r["gate"] == gate]
        if not sub:
            continue
        lines.append(f"### {gate}（{len(sub)} 场）\n")
        for r in sub:
            mark = "✅翻对" if r["correct_flip"] else "❌翻错"
            orig_mark = "原对" if r["correct_orig"] else "原错"
            lines.append(f"- {r['slug']}：{r['orig']}→{r['final']}（实际{r['act']}）{mark}/{orig_mark} | {r['mode']}")
            for rk in r["risks"]:
                lines.append(f"  - risk: {rk}")
        lines.append("")

    # 结论建议
    lines.append("## 结论与建议\n")
    for gate in order:
        sub = [r for r in rows if r["gate"] == gate]
        n = len(sub)
        if n == 0:
            continue
        k = sum(1 for r in sub if r["correct_flip"])
        wl = wilson_lower(k, n)
        k_o = sum(1 for r in sub if r["correct_orig"])
        improve = sum(1 for r in sub if r["correct_flip"] and not r["correct_orig"])
        hurt = sum(1 for r in sub if not r["correct_flip"] and r["correct_orig"])
        net = improve - hurt
        if n >= 30 and wl > BREAKEVEN:
            rec = f"可靠，保留/强化翻转（净改善 {net:+d} 场）"
        elif n < 30:
            rec = f"样本不足（n={n}<30），继续观察收集样本，暂不作为独立决策依据"
        else:
            rec = f"翻转整体不佳（下界{wl*100:.1f}%≤50%），应审查该口子门槛或收窄触发条件"
        lines.append(f"- **{gate}**（n={n}）：翻转正确 {k/n*100:.1f}%（下界 {wl*100:.1f}%），基线 {k_o/n*100:.1f}%，"
                     f"改善 {improve}/恶化 {hurt}（净 {net:+d}）→ {rec}")
    lines.append("")

    OUT.write_text("\n".join(lines) + "\n")
    print(f"已生成 {OUT}")
    for gate in order:
        sub = [r for r in rows if r["gate"] == gate]
        n = len(sub)
        if n == 0:
            continue
        k = sum(1 for r in sub if r["correct_flip"])
        print(f"{gate}: n={n} 翻对 {k} ({k/n*100:.1f}%) 下界 {wilson_lower(k,n)*100:.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
