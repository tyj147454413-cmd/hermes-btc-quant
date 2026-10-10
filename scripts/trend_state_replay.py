#!/usr/bin/env python3
"""图表分析"3-5小时趋势状态"因子回测（2026-08-17）。

目标：回答用户"把 3-5 小时趋势（顺趋势/单跳/变向）作为 1H 评分因子是否有价值"。
用现有数据（data/snapshots + data/hermes_results.jsonl）重放全部有快照+结算的场次，
按 prev_hour.direction_history（近3根已完成1H方向）分类三状态，对每个状态统计：
  - 跟随方向（押延续）正确率        → 若显著>50%，说明"顺趋势跟随"有价值
  - 反向方向（押 prev 反方向）正确率 → 若显著>50%，说明"单跳/变向反转"有价值
  - 当前引擎（rule_engine.judge）方向命中率 → 基线，判断新因子是否超出现有引擎

判定标准（方法论铁律）：n<30 不作决策依据；用 Wilson95 下界 vs 保本线 50%。
若某状态正确率显著 >55%（下界>50%）且 n≥30，才考虑作为评分因子。

用法：python3 scripts/trend_state_replay.py
输出：data/hermes_trend_state_replay_20260817.md
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
OUT = ROOT / "data" / "hermes_trend_state_replay_20260817.md"

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


def classify_state(dh: list[str]) -> str:
    """三状态分类（direction_history 恒为近3根已完成 1H 方向）。
    顺趋势(延续): flips==0 → [UUU]/[DDD]
    单跳(交替)  : flips==2 → [UDU]/[DUD]
    变向(反转)  : flips==1 → [UUD]/[DDU]/[UDD]/[DUU]
    """
    if len(dh) < 2:
        return "未知"
    flips = sum(1 for a, b in zip(dh, dh[1:]) if a != b)
    if flips == 0:
        return "顺趋势(延续)"
    if flips == 2:
        return "单跳(交替)"
    return "变向(反转)"


def engine_direction(md: dict) -> tuple[str | None, str]:
    """用现有 rule_engine 重放，返回 (final_direction, mode)。SKIP/无方向返回 None。"""
    try:
        factors, inp, notes, mode = extract_factors(md)
    except _SkipValidation:
        return None, "skip"
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
    if d.action == "SKIP" or not d.final_direction:
        return None, mode
    return d.final_direction, mode


def main() -> int:
    amap = actual_map()
    rows = []
    unscanned = 0
    engine_stats = {"n": 0, "correct": 0, "skip": 0}

    for slug, act in amap.items():
        md = load_latest_snapshot(slug)
        if not md:
            unscanned += 1
            continue
        dh = md.get("prev_hour", {}).get("direction_history") or []
        if len(dh) < 2:
            continue
        state = classify_state(dh)
        follow_dir = dh[-1]                      # 跟随方向 = 最近一根已完成 1H
        reverse_dir = "DOWN" if follow_dir == "UP" else "UP"  # 反向方向

        edir, mode = engine_direction(md)
        if edir:
            engine_stats["n"] += 1
            engine_stats["correct"] += (edir == act)

        rows.append({
            "slug": slug.split("august-")[-1],
            "state": state,
            "dh": "/".join(dh),
            "act": act,
            "follow_dir": follow_dir,
            "follow_ok": (follow_dir == act),
            "reverse_ok": (reverse_dir == act),
            "engine_dir": edir or "SKIP",
            "engine_ok": (edir == act) if edir else None,
            "mode": mode,
        })

    lines = []
    lines.append("# 图表分析「3-5小时趋势状态」因子回测（2026-08-17）")
    lines.append("")
    lines.append(f"> 样本：结算记录 {len(amap)} 场，有快照可分类 {len(rows)} 场，无快照 {unscanned} 场。")
    lines.append("> 保本线 = 方向正确 50%；统计用点估计 + Wilson95 下界；n<30 不作充分依据。")
    lines.append("> 三状态口径：用 prev_hour.direction_history（近3根已完成1H）。")
    lines.append("> 跟随 = 押最近一根 1H 方向（顺趋势延续）；反向 = 押 prev 反方向（单跳/变向）。")
    lines.append("")

    # 总体 + 分状态表
    lines.append("## 三状态判别力统计\n")
    lines.append("| 状态 | n | 跟随正确率 | 跟随下界 | 反向正确率 | 反向下界 | 引擎命中 | 引擎下界 |")
    lines.append("|------|---|-----------|---------|-----------|---------|---------|---------|")
    states = ["顺趋势(延续)", "单跳(交替)", "变向(反转)"]

    def _fmt(state):
        sub = [r for r in rows if r["state"] == state]
        n = len(sub)
        if n == 0:
            return f"| {state} | 0 | - | - | - | - | - | - |"
        kf = sum(1 for r in sub if r["follow_ok"])
        kr = sum(1 for r in sub if r["reverse_ok"])
        wlf = wilson_lower(kf, n)
        wlr = wilson_lower(kr, n)
        eng = [r for r in sub if r["engine_dir"] != "SKIP"]
        ne, ke = len(eng), sum(1 for r in eng if r["engine_ok"])
        wle = wilson_lower(ke, ne) if ne else 0.0
        return (f"| {state} | {n} | {kf}/{n}={kf/n*100:.1f}% | {wlf*100:.1f}% | "
                f"{kr}/{n}={kr/n*100:.1f}% | {wlr*100:.1f}% | "
                f"{ke}/{ne}={ke/ne*100:.1f}% | {wle*100:.1f}% |")

    for st in states:
        lines.append(_fmt(st))
    # 合计
    n_all = len(rows)
    kf_all = sum(1 for r in rows if r["follow_ok"])
    kr_all = sum(1 for r in rows if r["reverse_ok"])
    wlf_all = wilson_lower(kf_all, n_all)
    wlr_all = wilson_lower(kr_all, n_all)
    ne_all, ke_all = engine_stats["n"], engine_stats["correct"]
    wle_all = wilson_lower(ke_all, ne_all) if ne_all else 0.0
    lines.append(f"| **合计** | {n_all} | {kf_all}/{n_all}={kf_all/n_all*100:.1f}% | {wlf_all*100:.1f}% | "
                 f"{kr_all}/{n_all}={kr_all/n_all*100:.1f}% | {wlr_all*100:.1f}% | "
                 f"{ke_all}/{ne_all}={ke_all/ne_all*100:.1f}% | {wle_all*100:.1f}% |")
    lines.append("")

    # 结论判定
    lines.append("## 结论判定\n")
    for st in states:
        sub = [r for r in rows if r["state"] == st]
        n = len(sub)
        if n == 0:
            continue
        kf = sum(1 for r in sub if r["follow_ok"])
        kr = sum(1 for r in sub if r["reverse_ok"])
        wlf = wilson_lower(kf, n)
        wlr = wilson_lower(kr, n)
        verdicts = []
        if n < 30:
            verdicts.append(f"⚠️ 样本不足（n={n}<30），只观察不决策")
        else:
            if wlf > BREAKEVEN and kf / n > 0.55:
                verdicts.append(f"✅ 跟随方向有效（{kf/n*100:.1f}%，下界{wlf*100:.1f}%）→ 顺趋势跟随可考虑作为因子")
            else:
                verdicts.append(f"❌ 跟随方向无效（{kf/n*100:.1f}%，下界{wlf*100:.1f}%≤50%）→ 顺趋势延续被证伪")
            if wlr > BREAKEVEN and kr / n > 0.55:
                verdicts.append(f"✅ 反向方向有效（{kr/n*100:.1f}%，下界{wlr*100:.1f}%）→ 单跳/变向反转可考虑作为因子")
            else:
                verdicts.append(f"❌ 反向方向无效（{kr/n*100:.1f}%，下界{wlr*100:.1f}%≤50%）→ 押 prev 反转被证伪")
        lines.append(f"- **{st}**（n={n}）：{'; '.join(verdicts)}")
    lines.append("")

    # 引擎基线
    lines.append("## 引擎基线\n")
    lines.append(f"- 现有引擎（rule_engine 全口径重放）命中：{ke_all}/{ne_all} = {ke_all/ne_all*100:.1f}%（下界 {wle_all*100:.1f}%）")
    lines.append(f"- 判断新因子是否有增量价值：新因子正确率（或其组合）须显著高于引擎基线，否则不落地。")
    lines.append("")

    # 逐场明细
    lines.append("## 逐场明细\n")
    for st in states:
        sub = [r for r in rows if r["state"] == st]
        if not sub:
            continue
        lines.append(f"### {st}（{len(sub)} 场）\n")
        for r in sub:
            f_ok = "✅" if r["follow_ok"] else "❌"
            r_ok = "✅" if r["reverse_ok"] else "❌"
            e_ok = ("✅" if r["engine_ok"] else ("-" if r["engine_dir"] == "SKIP" else "❌"))
            lines.append(f"- {r['slug']}：hist=[{r['dh']}] 实际{r['act']} | 跟随{r['follow_dir']}{f_ok} / 反向{r_ok} / 引擎{r['engine_dir']}{e_ok} | {r['mode']}")
        lines.append("")

    OUT.write_text("\n".join(lines) + "\n")
    print(f"已生成 {OUT}")
    print(f"\n样本：总 {len(rows)} 场（有快照+结算），无快照 {unscanned} 场")
    for st in states:
        sub = [r for r in rows if r["state"] == st]
        n = len(sub)
        if n == 0:
            print(f"{st}: n=0")
            continue
        kf = sum(1 for r in sub if r["follow_ok"])
        kr = sum(1 for r in sub if r["reverse_ok"])
        print(f"{st}: n={n} 跟随 {kf}/{n}={kf/n*100:.1f}%（下界{wilson_lower(kf,n)*100:.1f}%）"
              f" 反向 {kr}/{n}={kr/n*100:.1f}%（下界{wilson_lower(kr,n)*100:.1f}%）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
