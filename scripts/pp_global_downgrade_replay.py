#!/usr/bin/env python3
"""方案 C 重放验证：价格位置全局 ×3→×2 + 极端位置(<15%/>55%)→×1。

对比三种口径的方向正确率（用实际下单时点快照机算 judge）：
- 基线 round14：|dev|<0.15% 时 price_position ×2，否则 ×3（当前已实现）
- 方案 C：全局 ×2 + current_percentile 极端(<15/>55) → ×1（signal_validator 已实现）
- 反事实对照：纯 ×3（历史原始）

用法：python3 scripts/pp_global_downgrade_replay.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, "scripts")

from signal_validator import extract_factors, _SkipValidation, _f
from rule_engine import judge

ROOT = Path("/Users/tangyijun/Desktop/hermes btc")
RESULTS = ROOT / "data" / "hermes_results.jsonl"
SNAP = ROOT / "data" / "snapshots"
OUT = ROOT / "data" / "hermes_price_pos_global_downgrade_20260813.md"

W3, W2, W1 = 3, 2, 1
PP_EXTREME_LOW, PP_EXTREME_HIGH = 15, 55


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


def pp_variants(inp: dict) -> tuple[int, int, int]:
    """返回 (w_pp, w_pp_extreme, dev) 用于三种口径的重算。

    extract_factors 已实现方案 C（全局 ×2 + 极端 ×1），此处读取 factors 里的
    price_position 当前值（方案 C 已生效），反向推导三种口径应取的值。
    """
    dev = abs(inp.get("deviation_pct") or 0)
    pp_now = inp.get("factors", {}).get("price_position", 0)
    sign = 1 if pp_now > 0 else (-1 if pp_now < 0 else 0)
    pct = inp.get("_pp_pct")  # current_percentile（由 main 注入）
    # 基线 round14：|dev|<0.15% → ×2，否则 ×3
    w_r14 = W2 if dev < 0.15 else W3
    # 方案 C：×2，极端 ×1
    w_c = W1 if (pct is not None and (pct < PP_EXTREME_LOW or pct > PP_EXTREME_HIGH)) else W2
    return sign * w_r14, sign * w_c, sign * W3


def judge_with_pp(inp: dict, pp_val: int):
    imp = dict(inp)
    imp["factors"] = dict(inp["factors"])
    imp["factors"]["price_position"] = pp_val
    return judge(imp).final_direction


def main() -> int:
    amap = actual_map()
    rows = []
    for slug, act in amap.items():
        if not SNAP.glob(f"{slug}_*.json"):
            continue
        # 用最新快照（实际下单时点）
        cands = sorted(SNAP.glob(f"{slug}_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        md = None
        for p in cands:
            try:
                md = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if md.get("slug") == slug:
                break
        if not md:
            continue
        try:
            factors, inp, notes, mode = extract_factors(md)
        except _SkipValidation:
            continue
        pct = _f((md.get("price_position_24h") or {}).get("current_percentile"))
        inp["_pp_pct"] = pct
        w_r14, w_c, w_pure = pp_variants(inp)
        # 基线 round14 方向（当前 extract_factors 输出 = 方案C；需重算 round14）
        d_r14 = judge_with_pp(inp, w_r14)
        d_c = judge_with_pp(inp, w_c)
        d_pure = judge_with_pp(inp, w_pure)
        rows.append({
            "slug": slug, "act": act, "dev": abs(inp.get("deviation_pct") or 0),
            "pct": pct, "d_r14": d_r14, "d_c": d_c, "d_pure": d_pure,
            "pp_r14": w_r14, "pp_c": w_c,
        })

    n = len(rows)
    def acc(key):
        return sum(1 for r in rows if r[key] == r["act"])
    n_r14 = acc("d_r14")
    n_c = acc("d_c")
    n_pure = acc("d_pure")
    changed = [r for r in rows if r["d_r14"] != r["d_c"]]
    improved = [r for r in changed if r["d_c"] == r["act"] and r["d_r14"] != r["act"]]
    worsened = [r for r in changed if r["d_c"] != r["act"] and r["d_r14"] == r["act"]]

    lines = []
    lines.append("# 方案 C 重放验证：价格位置全局 ×3→×2 + 极端位置 ×1（2026-08-13）\n")
    lines.append(f"> 样本：{n} 场（8/10-8/13 有快照 + 有结算结果），用实际下单时点快照机算 judge。\n")
    lines.append("| 口径 | 正确率 | 说明 |")
    lines.append("|------|--------|------|")
    lines.append(f"| 纯 ×3（历史原始） | {n_pure}/{n} = {n_pure/n*100:.1f}% | price_position 恒 ×3 |")
    lines.append(f"| round14（近贴线×2） | {n_r14}/{n} = {n_r14/n*100:.1f}% | \\|dev\\|<0.15% 时 ×2，否则 ×3 |")
    lines.append(f"| **方案C（全局×2+极端×1）** | {n_c}/{n} = {n_c/n*100:.1f}% | 全局 ×2；current_percentile<15/>55 时 ×1 |\n")
    lines.append(f"**方向变化场次：{len(changed)} | 改善 {len(improved)} | 恶化 {len(worsened)}**\n")
    if changed:
        lines.append("| 场次 | 实际 | 偏差% | percentile | round14方向 | 方案C方向 | 效果 |")
        lines.append("|------|------|-------|-----------|-----------|-----------|------|")
        for r in changed:
            eff = ("✅改善" if r["d_c"] == r["act"] and r["d_r14"] != r["act"]
                   else "❌恶化" if r["d_c"] != r["act"] and r["d_r14"] == r["act"] else "持平")
            pct_s = f"{r['pct']:.1f}%" if r["pct"] is not None else "N/A"
            lines.append(f"| {r['slug']} | {r['act']} | {r['dev']:.4f}% | {pct_s} | "
                         f"{r['d_r14']} | {r['d_c']} | {eff} |")
        lines.append("")
    lines.append("## 结论\n")
    lines.append(f"- 方案 C 正确率 {n_c}/{n}（{n_c/n*100:.1f}%）vs round14 {n_r14}/{n}（{n_r14/n*100:.1f}%），净 {'+' if len(improved)>=len(worsened) else ''}{len(improved)-len(worsened)} 场。")
    lines.append("- 极端位置（<15%/>55%）price_position 降为 ×1 仅参考，避免信号失效区误导方向。")
    lines.append("- 注意：样本 54-63 场、变化场次有限；结论需结合实盘观察。")
    OUT.write_text("\n".join(lines) + "\n")
    print(f"已生成 {OUT}")
    print(f"纯×3 {n_pure}/{n}={n_pure/n*100:.1f}% | round14 {n_r14}/{n}={n_r14/n*100:.1f}% | 方案C {n_c}/{n}={n_c/n*100:.1f}% | 净 {len(improved)-len(worsened)} 恶化{len(worsened)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
