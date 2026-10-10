#!/usr/bin/env python3
"""候选翻转条件在全部场次上的全集验证（2026-08-14）。

在 21 场触发场里挖出的候选翻转条件可能过拟合。本脚本在全部有快照+结算的
场次（无论是否触发反手预警）上评估候选条件：命中条件则翻转方向，统计改善/恶化。
条件不依赖"反手预警 3/4 维触发"前置，因为错误场 9pm 就未触发预警。

候选条件（基于错误场特征：预测DOWN+贴线+顺共识+衰竭+净分≤-3+|hist4|浅）：
- F1: 预测DOWN + 贴线模式 + 顺共识60-80% + 4H衰竭 + |hist4|<50 + 净分≤-3
- F2: F1 去掉 |hist4| 约束
- F3: F2 去掉 净分 约束（纯特征）
- F4: F1 加 贴线维持/激活（排除失效预警）
- F5: 预测DOWN + 贴线 + 顺共识 + 衰竭 + |hist4|<50（不加净分）
- F6: 预测DOWN + 贴线 + 顺共识60-70% + 衰竭 + 净分≤-3 + |hist4|<50

用法：python3 scripts/countertrend_flip_candidates.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "scripts")

from signal_validator import extract_factors, _SkipValidation, _f  # noqa: E402
from rule_engine import judge  # noqa: E402

ROOT = Path("/Users/tangyijun/Desktop/hermes btc")
RESULTS = ROOT / "data" / "hermes_results.jsonl"
SNAP = ROOT / "data" / "snapshots"
OUT = ROOT / "data" / "hermes_countertrend_flip_candidates_20260814.md"


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


def load_latest_snapshot(slug: str) -> dict | None:
    if not SNAP.glob(f"{slug}_*.json"):
        return None
    cands = sorted(SNAP.glob(f"{slug}_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in cands:
        try:
            md = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if md.get("slug") == slug:
            return md
    return None


def main() -> int:
    amap = actual_map()
    amap["bitcoin-up-or-down-august-14-2026-1am-et"] = "UP"
    amap.setdefault("bitcoin-up-or-down-august-13-2026-9pm-et", "UP")

    rows = []
    for slug, act in amap.items():
        md = load_latest_snapshot(slug)
        if not md:
            continue
        try:
            factors, inp, notes, mode = extract_factors(md)
        except _SkipValidation:
            continue
        d = judge(inp)
        direction = d.final_direction
        if not direction:
            continue
        hist = md.get("historical") or {}
        up_rate = _f(hist.get("up_rate"))
        pm = md.get("polymarket") or {}
        pm_up = _f(pm.get("up_price"))
        tf4h = (md.get("timeframes") or {}).get("4H") or {}
        accel4 = _f(tf4h.get("macd_acceleration"))
        hist4 = _f(tf4h.get("macd_hist"))
        dtrend = md.get("deviation_trend") or {}
        pm_side = None
        pm_dir = None
        if isinstance(pm_up, (int, float)):
            pm_side = max(pm_up * 100, (1 - pm_up) * 100) if pm_up > 0 else None
            pm_dir = "UP" if pm_up > 0.5 else ("DOWN" if pm_up < 0.5 else None)

        tie_mode = "贴线" in mode
        tie_stable = tie_mode and not dtrend.get("expanding")
        weak = tie_stable or d.confidence == "低"
        pm_cons = bool(pm_side is not None and 60 <= pm_side < 80 and pm_dir == direction)
        exhaust = bool(
            isinstance(accel4, (int, float))
            and ((direction == "DOWN" and accel4 > 0) or (direction == "UP" and accel4 < 0))
        )
        rows.append({
            "slug": slug, "act": act, "base": direction, "correct": direction == act,
            "net": d.net, "mode": mode, "tie": tie_mode, "weak": weak,
            "pm_cons": pm_cons, "pm_side": pm_side, "pm_dir": pm_dir,
            "exhaust": exhaust, "hist4": hist4, "is_down": direction == "DOWN",
            "up_rate": up_rate,
        })

    n = len(rows)
    all_wrong = [r for r in rows if not r["correct"]]

    def ev(cond):
        hits = [r for r in rows if cond(r)]
        imp = [r for r in hits if not r["correct"]]
        wor = [r for r in hits if r["correct"]]
        return hits, imp, wor

    candidates = {
        "F1: DOWN+贴线+顺共识+衰竭+|hist4|<50+净分≤-3": (
            lambda r: r["is_down"] and r["tie"] and r["pm_cons"] and r["exhaust"]
            and r["hist4"] is not None and abs(r["hist4"]) < 50 and r["net"] <= -3
        ),
        "F2: F1去掉|hist4|": (
            lambda r: r["is_down"] and r["tie"] and r["pm_cons"] and r["exhaust"] and r["net"] <= -3
        ),
        "F3: F2去掉净分": (
            lambda r: r["is_down"] and r["tie"] and r["pm_cons"] and r["exhaust"]
        ),
        "F4: F1限定贴线稳定(排除失效预警)": (
            lambda r: r["is_down"] and r["weak"] and r["pm_cons"] and r["exhaust"]
            and r["hist4"] is not None and abs(r["hist4"]) < 50 and r["net"] <= -3
        ),
        "F5: DOWN+贴线+顺共识+衰竭+|hist4|<50": (
            lambda r: r["is_down"] and r["tie"] and r["pm_cons"] and r["exhaust"]
            and r["hist4"] is not None and abs(r["hist4"]) < 50
        ),
        "F6: DOWN+贴线+顺共识60-70%+衰竭+净分≤-3+|hist4|<50": (
            lambda r: r["is_down"] and r["tie"] and r["pm_cons"] and r["exhaust"]
            and r["pm_side"] is not None and 60 <= r["pm_side"] < 70
            and r["hist4"] is not None and abs(r["hist4"]) < 50 and r["net"] <= -3
        ),
        "F7: 全量 DOWN+贴线+顺共识+衰竭+净分≤-3": (
            lambda r: r["is_down"] and r["tie"] and r["pm_cons"] and r["exhaust"]
            and r["net"] <= -3 and r["hist4"] is not None and abs(r["hist4"]) < 100
        ),
    }

    lines = []
    lines.append("# 候选翻转条件全集验证（2026-08-14）\n")
    lines.append(f"> 样本：{n} 场（含快照+结算）。基线错误 {len(all_wrong)} 场。"
                 f"条件命中则翻转方向。\n")
    lines.append("## 各候选条件效果\n")
    lines.append("| 条件 | 命中 | 改善 | 恶化 | 净效果 | 命中但基线错误的场(应翻对) | 命中但基线正确的场(误伤) |")
    lines.append("|------|------|------|------|--------|---------------------------|---------------------------|")
    for name, cond in candidates.items():
        hits, imp, wor = ev(cond)
        lines.append(
            f"| {name} | {len(hits)} | {len(imp)} | {len(wor)} | {len(imp)-len(wor):+d} | "
            f"{','.join(r['slug'].split('august-')[-1] for r in imp) or '-'} | "
            f"{','.join(r['slug'].split('august-')[-1] for r in wor) or '-'} |"
        )
    lines.append("")
    lines.append(f"## 全部错误场（基线方向错，共 {len(all_wrong)} 场）\n")
    for r in sorted(all_wrong, key=lambda x: x["slug"]):
        lines.append(
            f"- {r['slug'].split('august-')[-1]}：预测{r['base']}实际{r['act']} | 净分{r['net']:+.1f} | "
            f"模式={r['mode']} | 贴线={r['tie']} | 顺共识={r['pm_cons']}（PM{r['pm_dir']} {r['pm_side']}%） | "
            f"衰竭={r['exhaust']} | hist4={r['hist4']} | up_rate={r['up_rate']}"
        )
    lines.append("")
    lines.append("## 基线正确但可能被 F1 翻转误伤的场（若 F1 上马需盯防）\n")
    hits1, imp1, wor1 = ev(candidates["F1: DOWN+贴线+顺共识+衰竭+|hist4|<50+净分≤-3"])
    for r in wor1:
        lines.append(f"- {r['slug'].split('august-')[-1]}：预测{r['base']}实际{r['act']} 净分{r['net']:+.1f}")

    OUT.write_text("\n".join(lines) + "\n")
    print(f"已生成 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
