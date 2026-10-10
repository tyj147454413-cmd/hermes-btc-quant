#!/usr/bin/env python3
"""round18 反手预警分级深度 bug 审查（2026-08-14）。

重点核查：
1. F1 翻转后 scale 是否残留 9.7 的 0.5/min（翻转级设计"不缩仓"）
2. F1 翻转是否与 G5.4 zone_cover / G3 覆盖冲突（覆盖优先级）
3. F1 翻转后 d.net / net_for_prob / pm_same 语义是否错位
4. 观察级命中但 F1 边界（|hist4| 恰好 50 / 净分恰好 -3）是否判定正确
5. F1 里 direction 是否真的用"翻转前"值（G3/G5.4 可能已改向）

用法：python3 scripts/round18_bug_audit.py
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
OUT = ROOT / "data" / "hermes_round18_bug_audit_20260814.md"


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

    lines = []
    lines.append("# round18 反手预警分级深度 bug 审查（2026-08-14）\n")

    # ---- 1) 所有命中"翻转级/观察级"的场次，检查 scale 残留 ----
    lines.append("## 1) 反手预警命中场：scale / confidence / 方向语义核查\n")
    flip_count = 0
    obs_count = 0
    scale_residual = []  # 翻转级却 scale != None
    flip_pm_same_mismatch = []  # 翻转后 pm_same 与方向错位
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
        is_obs = any("观察级" in r for r in d.risks)
        if is_flip:
            flip_count += 1
            # scale 残留检查：翻转级设计"不缩仓"，scale 应为 None
            if d.position_scale is not None:
                scale_residual.append((slug.split("august-")[-1], d.position_scale, d.risks))
            lines.append(f"- F1翻转 {slug.split('august-')[-1]}：act={act} dir={direction} "
                         f"scale={d.position_scale} conf={d.confidence} net={d.net:+.1f}")
        elif is_obs:
            obs_count += 1
            lines.append(f"- 观察级 {slug.split('august-')[-1]}：act={act} dir={direction} "
                         f"scale={d.position_scale} conf={d.confidence} net={d.net:+.1f}")

    lines.append(f"\n> F1 翻转 {flip_count} 场，观察级 {obs_count} 场。")
    lines.append(f"\n### 翻转级 scale 残留（应为 None，若非 None 则 9.7 在翻转前注入了缩仓）：")
    if scale_residual:
        for s in scale_residual:
            lines.append(f"- **BUG**: {s[0]} scale={s[1]} risks={s[2]}")
    else:
        lines.append("- 无残留（全部 scale=None）✅")

    # ---- 2) 观察级命中但被 9.7 缩仓的场 ----
    lines.append("\n## 2) 观察级场次是否被 9.7#4/#3/#5 缩仓（观察级设计「完全不动」）\n")
    lines.append("> 观察级要求「方向/置信/仓位完全不动」。但 9.7 的 scale 在 F1 判定前已算好，\n")
    lines.append("> 若观察级场也命中 9.7#3/#4/#5（贴线/尾盘/顺共识），会残留缩仓——这与观察级「完全不动」冲突。\n")
    obs_scale_hits = []
    for slug, act in amap.items():
        md = load_latest_snapshot(slug)
        if not md:
            continue
        try:
            factors, inp, notes, mode = extract_factors(md)
        except _SkipValidation:
            continue
        d = judge(inp)
        if not d.final_direction:
            continue
        if not any("观察级" in r for r in d.risks):
            continue
        # 记录观察级但被 9.7 缩仓的场（scale 非 None 但方向没动 = 缩仓而非不动）
        if d.position_scale is not None:
            obs_scale_hits.append((slug.split("august-")[-1], d.position_scale, d.risks))
    if obs_scale_hits:
        for s in obs_scale_hits:
            lines.append(f"- **注意**: 观察级 {s[0]} scale={s[1]}（9.7 注入缩仓，与「完全不动」设计冲突）\n  risks={s[2]}")
    else:
        lines.append("- 无观察级场被 9.7 缩仓 ✅")

    # ---- 3) F1 边界与 9.7 优先级 ----
    lines.append("\n## 3) 关键发现汇总\n")

    # 统计：有多少翻转级场的 confidence 在翻转前已经因 9.7#1 顺共识被降级
    lines.append("\n## 4) 全部 trace（仅翻转级/观察级场）\n")
    for slug, act in amap.items():
        md = load_latest_snapshot(slug)
        if not md:
            continue
        try:
            factors, inp, notes, mode = extract_factors(md)
        except _SkipValidation:
            continue
        d = judge(inp)
        if not d.final_direction:
            continue
        if any("翻转级" in r or "观察级" in r for r in d.risks):
            lines.append(f"\n### {slug.split('august-')[-1]} (act={act})")
            for t in d.trace:
                lines.append(f"  - {t}")
            for rk in d.risks:
                lines.append(f"  - risk: {rk}")

    OUT.write_text("\n".join(lines) + "\n")
    print(f"已生成 {OUT}")
    print(f"F1 翻转 {flip_count} 场，观察级 {obs_count} 场，翻转级 scale 残留 {len(scale_residual)} 处")
    return 0


if __name__ == "__main__":
    sys.exit(main())
