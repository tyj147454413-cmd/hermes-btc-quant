#!/usr/bin/env python3
"""Bug#2（S₀=0 时 PM 行归零）与 Bug#4（价格位置权重）回测（2026-08-15）。

方法：用 hermes_results.jsonl 的结算结果（actual）作标签，用实际下单时点快照
（data/snapshots 最新）机算因子，跑 rule_engine.judge 对比变体方向正确率。

回测 1（Bug#2）：筛 S₀=0 的场次
  - 基线：judge 原样（PM 行=0，方向由价格位置决定）
  - 变体A：S₀=0 时强制方向 = PM 侧（pm_dir）
  - 变体B：S₀=0 时 factors 注入 pm 保底分（打破平局，方向=PM 侧，净分±1）
回测 2（Bug#4）：价格位置权重 ×1 / ×2 / ×3 三档（全样本）
  - judge 内 price_position 因子值分别置为 sign(dev)×{1,2,3}

输出：data/hermes_s0_pm_pp_replay_20260815.md

用法：python3 scripts/replay_s0_pm_and_pp.py
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, "scripts")

from signal_validator import extract_factors, _SkipValidation  # noqa: E402
from rule_engine import judge  # noqa: E402

ROOT = Path("/Users/tangyijun/Desktop/hermes btc")
RESULTS = ROOT / "data" / "hermes_results.jsonl"
SNAP = ROOT / "data" / "snapshots"
OUT = ROOT / "data" / "hermes_s0_pm_pp_replay_20260815.md"


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
    return m


def load_snapshot(slug: str) -> dict | None:
    cands = sorted(SNAP.glob(f"{slug}_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in cands:
        try:
            md = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if md.get("slug") == slug:
            return md
    return None


def wilson95(n: int, k: int) -> float | None:
    """Wilson 95% 下界（比例），n=0 返回 None。"""
    if n == 0:
        return None
    p = k / n
    z = 1.96
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (center - half) * 100


def main() -> int:
    amap = actual_map()
    rows = []  # 全部有效样本
    s0_zero = []  # S₀=0 子集
    for slug, act in amap.items():
        md = load_snapshot(slug)
        if not md:
            continue
        try:
            factors, inp, notes, mode = extract_factors(md)
        except _SkipValidation:
            continue

        # ---- 基线 judge ----
        d0 = judge(inp)
        rows.append({"slug": slug, "act": act, "d0": d0.final_direction, "s0": d0.s0,
                     "net": d0.net, "factors": factors, "inp": inp, "mode": mode})

    # ============ 回测 1：S₀=0 子集 ============
    for r in rows:
        if abs(r["s0"]) < 1e-9:
            s0_zero.append(r)

    def judge_variant(inp: dict, *, force_dir: str | None = None, inject_pm: bool = False) -> str | None:
        imp = dict(inp)
        imp["factors"] = dict(inp["factors"])
        pm_up = inp.get("pm_up")
        if inject_pm and isinstance(pm_up, (int, float)):
            pm_dir_v = "UP" if pm_up > 50 else ("DOWN" if pm_up < 50 else None)
            if pm_dir_v:
                imp["factors"]["pm_tiebreak"] = float(1 if pm_dir_v == "UP" else -1)
        return judge(imp).final_direction

    # 变体A：force_dir=PM 侧（但 judge 内部 net_direction 为 0 → 价格位置，需特殊处理）
    # 说明：judge 的 net_direction = _dir_of(_sign(net)) or price_dir，S₀=0 时 PM 行=0 → net=0 → price_dir。
    # 因此"PM 参与"最干净的实现是 inject_pm（变体B）：注入 ±1 打破平局，净分方向=PM 侧，
    # 后续 G2-G5 覆盖照常执行，与引擎架构一致。变体A 仅作方向正确率直比（不跑门槛层）。
    base_correct = sum(1 for r in s0_zero if r["d0"] == r["act"])
    # 直接对比：price_dir vs pm_dir 原始正确率（不跑门槛层，纯看两个信号各自的方向判别力）
    price_correct = 0
    pm_correct = 0
    pm_available = 0
    both_rows = []
    for r in s0_zero:
        dev = float(r["inp"].get("deviation_pct", 0) or 0)
        pm_up = r["inp"].get("pm_up")
        price_dir_v = "UP" if dev > 0 else ("DOWN" if dev < 0 else None)
        pm_dir_v = "UP" if (isinstance(pm_up, (int, float)) and pm_up > 50) else ("DOWN" if (isinstance(pm_up, (int, float)) and pm_up < 50) else None)
        price_correct += int(price_dir_v == r["act"])
        if pm_dir_v:
            pm_available += 1
            pm_correct += int(pm_dir_v == r["act"])
        both_rows.append({"slug": r["slug"], "act": r["act"], "price": price_dir_v, "pm": pm_dir_v,
                          "pm_up": pm_up, "dev": dev, "d0": r["d0"]})
    # 变体B：注入 PM 保底分跑完整 judge
    vb_correct = sum(1 for r in s0_zero if judge_variant(r["inp"], inject_pm=True) == r["act"])
    vb_dir = [judge_variant(r["inp"], inject_pm=True) for r in s0_zero]

    # ============ 回测 2：价格位置权重 ×1/×2/×3 ============
    def judge_pp(inp: dict, w: int) -> str | None:
        imp = dict(inp)
        imp["factors"] = dict(inp["factors"])
        dev = float(inp.get("deviation_pct", 0) or 0)
        sign = 1 if dev > 0 else (-1 if dev < 0 else 0)
        if sign != 0:
            imp["factors"]["price_position"] = float(sign * w)
        return judge(imp).final_direction

    n_pp = len(rows)
    pp1 = sum(1 for r in rows if judge_pp(r["inp"], 1) == r["act"])
    pp2 = sum(1 for r in rows if judge_pp(r["inp"], 2) == r["act"])
    pp3 = sum(1 for r in rows if judge_pp(r["inp"], 3) == r["act"])

    # ============ 报告 ============
    lines = []
    lines.append("# 回测：S₀=0 时 PM 行归零（Bug#2）与价格位置权重（Bug#4）（2026-08-15）\n")
    lines.append(f"> 样本：{len(rows)} 场有快照+结算；S₀=0 子集 {len(s0_zero)} 场。标签=结算 actual，方向=judge 最终方向。\n")

    lines.append("## 回测 1：S₀=0 子集 — PM 参与 vs 归零\n")
    lines.append("### 1a. 原始信号方向判别力直比（不跑门槛层）\n")
    lines.append("| 信号 | 正确 | 样本 | 正确率 | Wilson95 下界 |")
    lines.append("|------|------|------|--------|--------------|")
    n0 = len(s0_zero)
    if n0:
        w_price = wilson95(n0, price_correct)
        lines.append(f"| 价格位置方向（现状，S₀=0 时的方向决定者） | {price_correct} | {n0} | {price_correct/n0*100:.1f}% | {w_price:.1f}% |")
    if pm_available:
        w_pm = wilson95(pm_available, pm_correct)
        lines.append(f"| PM 侧方向（PM 参与时的候选方向） | {pm_correct} | {pm_available} | {pm_correct/pm_available*100:.1f}% | {w_pm:.1f}% |")
    lines.append("")
    lines.append(f"结论：S₀=0 时价格位置 vs PM 侧，正确率高者更值得作为方向决定者。")

    lines.append("\n### 1b. 完整 judge 变体（含门槛层 G2-G5）\n")
    lines.append("| 口径 | 正确 | 样本 | 正确率 | Wilson95 下界 |")
    lines.append("|------|------|------|--------|--------------|")
    if n0:
        w_base = wilson95(n0, base_correct)
        w_vb = wilson95(n0, vb_correct)
        lines.append(f"| 基线（PM 行=0，S₀=0→价格位置决定） | {base_correct} | {n0} | {base_correct/n0*100:.1f}% | {w_base:.1f}% |")
        lines.append(f"| 变体B（S₀=0 注入 PM 保底分→方向=PM 侧） | {vb_correct} | {n0} | {vb_correct/n0*100:.1f}% | {w_vb:.1f}% |")
    lines.append("")
    changed_b = [i for i, r in enumerate(s0_zero) if r["d0"] != vb_dir[i]]
    improved_b = [i for i in changed_b if vb_dir[i] == s0_zero[i]["act"] and s0_zero[i]["d0"] != s0_zero[i]["act"]]
    worsened_b = [i for i in changed_b if vb_dir[i] != s0_zero[i]["act"] and s0_zero[i]["d0"] == s0_zero[i]["act"]]
    lines.append(f"变体B 方向变化场次：{len(changed_b)} | 改善 {len(improved_b)} | 恶化 {len(worsened_b)} | 净 {len(improved_b)-len(worsened_b)}\n")
    if changed_b:
        lines.append("| 场次 | 实际 | 基线 | 变体B | PM | 偏差% | 效果 |")
        lines.append("|------|------|------|-------|-----|-------|------|")
        for i in changed_b:
            r = s0_zero[i]
            eff = ("✅改善" if vb_dir[i] == r["act"] and r["d0"] != r["act"]
                   else "❌恶化" if vb_dir[i] != r["act"] and r["d0"] == r["act"] else "持平")
            pm_up = r["inp"].get("pm_up")
            lines.append(f"| {r['slug']} | {r['act']} | {r['d0']} | {vb_dir[i]} | {pm_up if pm_up is not None else 'N/A'} | {r['inp'].get('deviation_pct', 0):.3f}% | {eff} |")
        lines.append("")

    lines.append("## 回测 2：价格位置权重 ×1/×2/×3（全样本）\n")
    lines.append("| 权重 | 正确 | 样本 | 正确率 | Wilson95 下界 |")
    lines.append("|------|------|------|--------|--------------|")
    for w, k in ((1, pp1), (2, pp2), (3, pp3)):
        wl = wilson95(n_pp, k)
        cur = " ← 现行" if w == 2 else (" ← 历史×3" if w == 3 else "")
        lines.append(f"| ×{w} | {k} | {n_pp} | {k/n_pp*100:.1f}% | {wl:.1f}% |{cur}")
    lines.append("")

    lines.append("## 方法论说明\n")
    lines.append("- Wilson95 下界 vs 保本线：方向正确率保本线按 50%（随机）衡量；n<30 不作决策依据（方法论铁律）。")
    lines.append("- 回测用实际下单时点最新快照机算因子，与 signal_validator 同源；AI 专属因子（taker_1m/taker_trend/fakeout 等）引擎保守默认。")
    lines.append("- 变体B 通过 factors 注入 pm_tiebreak=±1，等价于「打破 S0=0 平局、方向=PM 侧」，后续门槛层照常执行，与引擎架构一致。")

    OUT.write_text("\n".join(lines) + "\n")
    print(f"已生成 {OUT}")
    print(f"回测1: S₀=0 n={n0}, 基线 {base_correct}/{n0}={base_correct/n0*100:.1f}%, 变体B {vb_correct}/{n0}={vb_correct/n0*100:.1f}% (净{len(improved_b)-len(worsened_b)})")
    print(f"回测2: ×1 {pp1}/{n_pp}={pp1/n_pp*100:.1f}%, ×2 {pp2}/{n_pp}={pp2/n_pp*100:.1f}%, ×3 {pp3}/{n_pp}={pp3/n_pp*100:.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
