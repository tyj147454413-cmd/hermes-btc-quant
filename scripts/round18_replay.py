#!/usr/bin/env python3
"""round18 反手预警分级全量回测（2026-08-14）。

用 round18 引擎（已内置 F1 翻转级/观察级）重放全部有快照+结算的场次，
统计 round18 相对 round16 基线的方向正确率与翻转改善/恶化。

用法：python3 scripts/round18_replay.py
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
OUT = ROOT / "data" / "hermes_round18_replay_20260814.md"


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
    flipped = []
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
        is_flip = any("翻转级" in r for r in d.risks)
        rows.append({
            "slug": slug.split("august-")[-1], "act": act, "dir": direction,
            "correct": direction == act, "flip": is_flip, "risks": d.risks,
            "conf": d.confidence, "scale": d.position_scale,
        })
        if is_flip:
            flipped.append(rows[-1])

    n = len(rows)
    correct = sum(1 for r in rows if r["correct"])
    lines = []
    lines.append("# round18 反手预警分级全量回测（2026-08-14）\n")
    lines.append(f"> 样本：{n} 场（含快照+结算）。round18 引擎 = round16 基线 + F1 翻转级/观察级。\n")
    lines.append(f"## 总览\n")
    lines.append(f"- 总方向正确：{correct}/{n} = {correct/n*100:.1f}%\n")
    lines.append(f"- F1 翻转触发：{len(flipped)} 场\n")
    for r in flipped:
        mark = "✅翻对" if r["correct"] else "❌翻错"
        lines.append(f"  - {r['slug']}：预测→实际 {r['act']}，翻转后 {r['dir']} {mark}，scale={r['scale']}")
    lines.append(f"\n- 翻转场净效果：改善 {sum(1 for r in flipped if r['correct'])} / 恶化 {sum(1 for r in flipped if not r['correct'])}\n")
    lines.append("## 翻转场完整信息\n")
    for r in flipped:
        lines.append(f"### {r['slug']}")
        lines.append(f"- 实际 {r['act']}，翻转后 {r['dir']}，{'对' if r['correct'] else '错'}")
        lines.append(f"- scale={r['scale']} conf={r['conf']}")
        for rk in r["risks"]:
            if "翻转" in rk or "预警" in rk:
                lines.append(f"- risk: {rk}")

    OUT.write_text("\n".join(lines) + "\n")
    print(f"已生成 {OUT}")
    print(f"样本 {n}，正确 {correct}（{correct/n*100:.1f}%），F1 翻转 {len(flipped)} 场")
    return 0


if __name__ == "__main__":
    sys.exit(main())
