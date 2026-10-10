#!/usr/bin/env python3
"""候选翻转型别（F1U / BB3）全量回测（2026-08-15，F-框架阶段2）。

用 round18 引擎 + 8/15 候选翻转型别观察级标注，重放全部有快照+结算的场次，
统计 F1U / BB3 各自：命中场数、翻转胜率（点估计 + Wilson95 下界 vs 保本线）、
基线（不翻转保持原方向）正确率、误伤场数。

决策规则（方法论铁律）：
- n<30 或 Wilson95 下界 ≤ 保本线 → 保持观察级（仅标注，不改方向）
- n≥30 且 Wilson95 下界 > 保本线 → 建议升翻转级（需人工复核 + prompt.md/rule_engine 同步）

用法：python3 scripts/flip_conditions_replay.py
可选项：
  --relax-bb3-accel   敏感性：把 4H accel∈(-0.5,0]（跌势完全衰竭微负）视为衰竭再判 BB3，
                      验证 8/14 3pm 目标场（accel=-0.01）能否命中（引擎默认口径 accel>0 会漏掉）。
"""
from __future__ import annotations

import argparse
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
OUT = ROOT / "data" / "hermes_flip_conditions_replay_20260815.md"

# 保本线：1H 二元市场方向正确保本 ≈ 0.50；成交价更高时保本线更高，回测取 0.50 保守基准
BREAKEVEN = 0.50
Z = 1.96  # Wilson 95%


def wilson_lower(k: int, n: int, z: float = Z) -> float:
    """Wilson 得分区间下界。n=0 返回 0（无样本不作依据）。"""
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
    # 与 round18 同口径的已知结算修正（缺失/迟延场次）
    m["bitcoin-up-or-down-august-14-2026-1am-et"] = "UP"
    m.setdefault("bitcoin-up-or-down-august-13-2026-9pm-et", "UP")
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--relax-bb3-accel", action="store_true",
                    help="敏感性：4H accel∈(-0.5,0] 视为衰竭（跌势完全衰竭微负口径）")
    args = ap.parse_args()

    amap = actual_map()
    rows = []
    scanned = 0  # 有快照且成功重放（含 G1 缺失跳过）的场次数
    relax_extra = []  # 放宽口径额外命中的场次（仅敏感性展示）
    for slug, act in amap.items():
        md = load_latest_snapshot(slug)
        if not md:
            continue
        scanned += 1
        try:
            factors, inp, notes, mode = extract_factors(md)
        except _SkipValidation:
            continue
        d = judge(inp)
        orig_dir = d.final_direction
        if not orig_dir:
            continue
        hits = []
        for tag in ("[F1U]", "[BB3]"):
            if any(tag in r for r in d.risks):
                hits.append(tag[1:-1])
        if not hits:
            # 敏感性：放宽 4H 衰竭口径（accel∈(-0.5,0] → 0.01，即"跌势完全衰竭"含微负）后，
            # 记录两类额外命中：① BB3 观察级 ② F1 翻转级（8/14 3pm accel=-0.01 属此类）
            if args.relax_bb3_accel and orig_dir == "DOWN":
                accel = inp.get("macd_4h_accel")
                if isinstance(accel, (int, float)) and -0.5 < accel <= 0:
                    inp2 = dict(inp)
                    inp2["macd_4h_accel"] = 0.01
                    d2 = judge(inp2)
                    if d2.final_direction != orig_dir:
                        # F1 翻转级被激活（方向改变）：记录为敏感性发现，不进候选型别 rows
                        relax_extra.append((slug, act, orig_dir, d2.final_direction, accel, "F1翻转级"))
                    elif any("[BB3]" in r for r in d2.risks):
                        hits.append("BB3")
                        relax_extra.append((slug, act, orig_dir, "UP", accel, "BB3观察级"))
            if not hits:
                continue
        flip_to = "DOWN" if orig_dir == "UP" else "UP"  # 候选型别翻转目标（F1U: DOWN，BB3: UP）
        for tag in hits:
            rows.append({
                "slug": slug.split("august-")[-1],
                "act": act,
                "orig": orig_dir,
                "flip_to": flip_to,
                "tag": tag,
                "correct_flip": flip_to == act,   # 翻转后方向是否正确
                "correct_orig": orig_dir == act,  # 基线（不翻转）是否正确
                "conf": d.confidence,
                "scale": d.position_scale,
                "risks": [r for r in d.risks if "候选翻转型" in r],
            })

    lines = []
    lines.append("# 候选翻转型别（F1U / BB3）全量回测（2026-08-15，F-框架阶段2）\n")
    lines.append(f"> 样本口径：全部有结算记录的场次（{len(amap)} 场），其中有快照可重放 {scanned} 场。")
    lines.append("> 保本线 = 方向正确 50%；翻转胜率用点估计 + Wilson95 下界；n<30 不作升翻转级依据。\n")

    # 分组统计
    for tag in ("F1U", "BB3"):
        sub = [r for r in rows if r["tag"] == tag]
        n = len(sub)
        k_flip = sum(1 for r in sub if r["correct_flip"])
        k_orig = sum(1 for r in sub if r["correct_orig"])
        wl = wilson_lower(k_flip, n)
        flipped_correct = sum(1 for r in sub if r["correct_flip"] and not r["correct_orig"])
        hurt = sum(1 for r in sub if not r["correct_flip"] and r["correct_orig"])
        lines.append(f"## 型别 {tag}\n")
        if n == 0:
            lines.append("- 无命中场次，样本不足。\n")
            continue
        p_flip = k_flip / n * 100
        p_orig = k_orig / n * 100
        lines.append(f"- 命中：**{n} 场**")
        lines.append(f"- 翻转后方向正确：{k_flip}/{n} = **{p_flip:.1f}%**（Wilson95 下界 **{wl*100:.1f}%**）")
        lines.append(f"- 基线（不翻转保持原方向）正确：{k_orig}/{n} = **{p_orig:.1f}%**")
        lines.append(f"- 翻转改善（原错→翻对）：{flipped_correct} 场；翻转恶化（原对→翻错/误伤）：{hurt} 场")
        verdict = "✅ 达标：n≥30 且 Wilson 下界显著>50% → 可评估升翻转级" if (
            n >= 30 and wl > BREAKEVEN
        ) else ("⚠️ 样本不足：n<30 → 保持观察级" if n < 30 else "❌ 未达标：Wilson 下界≤保本线 → 保持观察级")
        lines.append(f"- 结论：{verdict}\n")
        for r in sub:
            mark = "✅翻对" if r["correct_flip"] else "❌翻错"
            orig_mark = "原对" if r["correct_orig"] else "原错"
            lines.append(f"  - {r['slug']}：预测 {r['orig']}→翻转 {r['flip_to']}（实际 {r['act']}）{mark}/{orig_mark} "
                         f"conf={r['conf']} scale={r['scale']}")

    if args.relax_bb3_accel:
        lines.append("\n## 敏感性：放宽 4H 衰竭口径（accel∈(-0.5,0] 视为衰竭）额外命中\n")
        if relax_extra:
            for slug, act, orig, flip, accel, kind in relax_extra:
                lines.append(f"- {slug.split('august-')[-1]}：预测 {orig}→{'翻转' if flip != orig else 'BB3标注'} {flip}"
                             f"（实际 {act}，accel={accel}，{kind}）")
        else:
            lines.append("- 无额外命中。")
        lines.append("> 注：引擎默认 4H 衰竭口径 accel>0。8/14 3pm 目标场 accel=-0.01 属\"跌势完全衰竭\"微负，")
        lines.append("> 按放宽口径（accel>-0.5）将命中 F1 翻转级且翻对（实际 UP），但此为阈值口径调整，")
        lines.append("> 需重跑全量回测确认无误伤后才可落地；本报告引擎口径不据此改判。")

    lines.append("\n## 结论（引擎口径，不改判）\n")
    for tag in ("F1U", "BB3"):
        sub = [r for r in rows if r["tag"] == tag]
        n = len(sub)
        if n == 0:
            lines.append(f"- **{tag}**：n=0 <30 → **保持观察级**（观察级标注已上线，继续收集样本）")
        else:
            k = sum(1 for r in sub if r["correct_flip"])
            wl = wilson_lower(k, n)
            lines.append(f"- **{tag}**：n={n}，翻转胜率 {k}/{n}，Wilson95 下界 {wl*100:.1f}%"
                         + (" → **保持观察级**（n<30）" if n < 30 else
                            (" → **可评估升翻转级**" if wl > BREAKEVEN else " → **保持观察级**（Wilson 下界≤保本线）")))
    lines.append("- **护栏2 教训**：BB3 初版缺 |4H hist|<50 护栏，8/11 1am（hist4=-159.91 深空势）误伤 → 已加护栏后 n=0")
    lines.append("- **F1 accel 边界发现**：8/14 3pm（accel=-0.01 微负）是 F1 翻转级漏网，放宽衰竭口径可翻对，")
    lines.append("  但属阈值调整，需单独回测验证无误伤后落地（不并入本次候选型别）\n")

    lines.append("\n## 命中场 risk 明细\n")
    for r in rows:
        lines.append(f"### {r['slug']}（{r['tag']}）")
        for rk in r["risks"]:
            lines.append(f"- risk: {rk}")

    OUT.write_text("\n".join(lines) + "\n")
    print(f"已生成 {OUT}")
    for tag in ("F1U", "BB3"):
        sub = [r for r in rows if r["tag"] == tag]
        n = len(sub)
        k = sum(1 for r in sub if r["correct_flip"])
        if n:
            print(f"{tag}：命中 {n}，翻转胜率 {k}/{n}={k/n*100:.1f}%（Wilson下界 {wilson_lower(k, n)*100:.1f}%）")
        else:
            print(f"{tag}：命中 0，无样本")
    return 0


if __name__ == "__main__":
    sys.exit(main())
