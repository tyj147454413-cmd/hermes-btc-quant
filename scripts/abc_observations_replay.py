#!/usr/bin/env python3
"""观察项 A/B/C 独立回测（2026-08-15）。

针对 8/14 下午 3 场错判的根因（A/B/C 观察项），在全量快照+结算场次上验证：
  A: 1m bb_pos≥100（冲出布林上轨外）→ 短反弹/看多因子应归零（1pm 案例）
  B: 4H bb_pos≤10 + 4H 衰竭(accel>0) + 方向DOWN → 追空因子降权/减半（3pm 案例）
  C: taker_bs_trend 末6根波动剧烈(max/min>3x 非单调) → Taker 深度档降级（4pm 案例）

对每个命中场次，模拟"落地后"重跑 judge，对比方向是否改变、是否改善/恶化，
统计各观察项的命中 n、改善、恶化、净效果。给出是否值得落地的数据结论。

用法：python3 scripts/abc_observations_replay.py
输出：data/hermes_abc_observations_replay_20260815.md
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
OUT = ROOT / "data" / "hermes_abc_observations_replay_20260815.md"

A_BBPOS_1M = 100.0      # A: 1m 冲出布林上轨
B_BBPOS_4H = 10.0       # B: 4H 极低位
C_TREND_MAXMIN = 3.0    # C: taker 末6根 max/min > 3x
DEEP_TAKER_HIGH = 2.33  # C: taker_bs_ratio 深度买入阈值
DEEP_TAKER_LOW = 0.43   # C: taker_bs_ratio 深度卖出阈值


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


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def run_base(inp: dict) -> "judge":
    return judge(dict(inp), extreme_wall=inp.get("extreme_wall", False))


def main() -> int:
    amap = actual_map()
    rows = []          # 每个命中场记录
    for slug, act in amap.items():
        md = load_latest_snapshot(slug)
        if not md:
            continue
        try:
            factors, inp, notes, mode = extract_factors(md)
        except _SkipValidation:
            continue
        bw = inp.pop("_buy_wall_ratio", None)
        sw = inp.pop("_sell_wall_ratio", None)
        prelim = judge(dict(inp, extreme_wall=False))
        pdir = prelim.final_direction
        if pdir == "DOWN":
            extreme_wall = bool(sw is not None and sw > 3.0)
        elif pdir == "UP":
            extreme_wall = bool(bw is not None and bw > 3.0)
        else:
            extreme_wall = False
        d = judge(dict(inp, extreme_wall=extreme_wall))
        base_dir = d.final_direction
        if not base_dir or d.action == "SKIP":
            continue

        tf1m = (md.get("timeframes") or {}).get("1m") or {}
        tf4h = (md.get("timeframes") or {}).get("4H") or {}
        cap = md.get("capital_flow") or {}
        bb1m = _f(tf1m.get("bb_pos"))
        bb4h = _f(tf4h.get("bb_pos"))
        accel4 = inp.get("macd_4h_accel")
        trend = cap.get("taker_bs_trend")
        taker_ratio = _f(cap.get("taker_bs_ratio"))

        # ---- A: 1m bb_pos≥100（冲出布林上轨）→ 正方向因子归零 ----
        hit_a = False
        new_a_dir = None
        if bb1m is not None and bb1m >= A_BBPOS_1M:
            hit_a = True
            # 落地：正方向因子（order_flow 正值/price_position 正值/short_bounce 正值）归零
            inp2 = dict(inp, extreme_wall=extreme_wall)
            f2 = dict(inp.get("factors", {}))
            for k in ("order_flow", "price_position", "short_bounce_factor"):
                if f2.get(k) and f2[k] > 0:
                    f2[k] = 0.0
            inp2["factors"] = f2
            d2 = judge(inp2)
            new_a_dir = d2.final_direction

        # ---- B: 4H bb_pos≤10 + 4H衰竭(accel>-0.5 含微负) + 方向DOWN → 追空因子降权 ----
        hit_b = False
        new_b_dir = None
        if (bb4h is not None and bb4h <= B_BBPOS_4H
                and isinstance(accel4, (int, float)) and accel4 > -0.5
                and base_dir == "DOWN"):
            hit_b = True
            inp2 = dict(inp, extreme_wall=extreme_wall)
            f2 = dict(inp.get("factors", {}))
            # 追空核心因子：price_position(锚下负) + macd_4h(空) 减半
            for k in ("price_position", "macd_4h"):
                v = f2.get(k)
                if v and v < 0:
                    f2[k] = float(v) * 0.5
            inp2["factors"] = f2
            d2 = judge(inp2)
            new_b_dir = d2.final_direction

        # ---- C: taker 末6根 max/min>3x 且序列剧烈振荡 → Taker 深度档降级 ----
        hit_c = False
        new_c_dir = None
        if isinstance(trend, list) and len(trend) >= 4 and taker_ratio is not None:
            nums = [x for x in (_f(v) for v in trend) if x is not None]
            if len(nums) >= 4:
                mx, mn = max(nums), min(nums)
                # 剧烈振荡：max/min>3x 且序列非单调——任相邻段有方向反转
                # （首末同侧极端但中途探底/冲高 = 振荡，如 [2.7,2.08,5.59,5.31,1.43,5.63]）
                diffs = [nums[i] - nums[i-1] for i in range(1, len(nums))]
                osc = any((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0) for d1, d2 in zip(diffs, diffs[1:]))
                deep = taker_ratio > DEEP_TAKER_HIGH or taker_ratio < DEEP_TAKER_LOW
                if deep and mn > 0 and (mx / mn) > C_TREND_MAXMIN and osc:
                    hit_c = True
                    inp2 = dict(inp, extreme_wall=extreme_wall)
                    f2 = dict(inp.get("factors", {}))
                    # 深度档降级：taker_bs 归零（方向分失效），taker_1h 减半
                    f2["taker_bs"] = 0.0
                    if f2.get("taker_1h"):
                        f2["taker_1h"] = float(f2["taker_1h"]) * 0.5
                    inp2["factors"] = f2
                    d2 = judge(inp2)
                    new_c_dir = d2.final_direction

        rec = {
            "slug": slug.split("august-")[-1], "act": act,
            "base_dir": base_dir, "base_correct": base_dir == act,
            "hit_a": hit_a, "new_a": new_a_dir,
            "hit_b": hit_b, "new_b": new_b_dir,
            "hit_c": hit_c, "new_c": new_c_dir,
            "bb1m": bb1m, "bb4h": bb4h, "accel4": accel4,
            "taker_ratio": taker_ratio, "trend": trend,
        }
        if hit_a or hit_b or hit_c:
            rows.append(rec)

    def eval_hits(rows, key_hit, key_new):
        n = sum(1 for r in rows if r[key_hit])
        if n == 0:
            return None
        improve = sum(1 for r in rows if r[key_hit] and r[key_new] is not None
                      and r[key_new] == r["act"] and r["base_dir"] != r["act"])
        hurt = sum(1 for r in rows if r[key_hit] and r[key_new] is not None
                   and r[key_new] != r["act"] and r["base_dir"] == r["act"])
        no_change = sum(1 for r in rows if r[key_hit] and (r[key_new] is None or r[key_new] == r["base_dir"]))
        return {"n": n, "improve": improve, "hurt": hurt, "no_change": no_change}

    a = eval_hits(rows, "hit_a", "new_a")
    b = eval_hits(rows, "hit_b", "new_b")
    c = eval_hits(rows, "hit_c", "new_c")

    lines = []
    lines.append("# 观察项 A/B/C 独立回测（2026-08-15）\n")
    lines.append("> 样本：全部有结算+快照场次。A=1m bb_pos≥100 短反弹归零；B=4H bb_pos≤10+衰竭 追空降权；")
    lines.append("> C=taker 末6根 max/min>3x 非单调 深度档降级。\n")

    lines.append("## 命中与反事实效果总览\n")
    lines.append("| 观察项 | 命中 n | 落地后改善(原错→对) | 落地后恶化(原对→错) | 方向不变 | 净效果 | 结论 |")
    lines.append("|--------|--------|---------------------|---------------------|----------|--------|------|")
    for name, res in (("A", a), ("B", b), ("C", c)):
        if res is None:
            lines.append(f"| {name} | 0 | - | - | - | - | 无命中，无样本 |")
            continue
        net = res["improve"] - res["hurt"]
        verdict = ("✅ 净改善" if net > 0 and res["hurt"] == 0
                   else ("⚠️ 有改善但也有恶化" if net > 0 else
                         ("❌ 恶化" if net < 0 else "➖ 中性")))
        lines.append(f"| {name} | {res['n']} | {res['improve']} | {res['hurt']} | "
                     f"{res['no_change']} | {net:+d} | {verdict} |")
    lines.append("")

    # 目标场（8/14 下午）
    lines.append("## 8/14 下午目标场逐场明细\n")
    for r in rows:
        if any(h in r["slug"] for h in ("14-2026-1pm", "14-2026-2pm", "14-2026-3pm", "14-2026-4pm")):
            lines.append(f"### {r['slug']}（实际 {r['act']}，引擎判 {r['base_dir']}"
                         f"{'✅' if r['base_correct'] else '❌'}）")
            lines.append(f"- A命中={r['hit_a']} (1m bb_pos={r['bb1m']})"
                         f"{' → 落地后 ' + r['new_a'] if r['hit_a'] else ''}")
            lines.append(f"- B命中={r['hit_b']} (4H bb_pos={r['bb4h']} accel4={r['accel4']})"
                         f"{' → 落地后 ' + r['new_b'] if r['hit_b'] else ''}")
            lines.append(f"- C命中={r['hit_c']} (taker_ratio={r['taker_ratio']} trend={r['trend']})"
                         f"{' → 落地后 ' + r['new_c'] if r['hit_c'] else ''}")
            lines.append("")

    lines.append("## 全部命中场明细\n")
    for r in rows:
        tags = []
        if r["hit_a"]:
            tags.append(f"A(1m bb={r['bb1m']:.0f})→{r['new_a']}")
        if r["hit_b"]:
            tags.append(f"B(4H bb={r['bb4h']:.0f} accel={r['accel4']})→{r['new_b']}")
        if r["hit_c"]:
            tags.append(f"C(taker={r['taker_ratio']} trend_max/min)→{r['new_c']}")
        mark = "✅" if r["base_correct"] else "❌"
        lines.append(f"- {r['slug']}：引擎 {r['base_dir']}({mark}) 实际 {r['act']} | {'; '.join(tags)}")
    lines.append("")

    lines.append("## 结论\n")
    for name, res, desc in (("A", a, "1m bb_pos≥100 时短反弹看多因子归零"),
                            ("B", b, "4H bb_pos≤10 + 4H衰竭 追空因子降权"),
                            ("C", c, "taker 末6根 max/min>3x 非单调 深度档降级")):
        if res is None:
            lines.append(f"- **{name}**（{desc}）：无命中样本，暂不落地。")
        else:
            lines.append(f"- **{name}**（{desc}）：命中 {res['n']}，落地后改善 {res['improve']}/恶化 {res['hurt']}/"
                         f"不变 {res['no_change']}，净 {res['improve']-res['hurt']:+d}。"
                         + ("n<30 样本不足，观察级保留。" if res["n"] < 30 else
                            ("可评估落地。" if res["improve"] > res["hurt"] and res["hurt"] == 0 else "不建议直接落地。")))
    lines.append("")

    OUT.write_text("\n".join(lines) + "\n")
    print(f"已生成 {OUT}")
    for name, res in (("A", a), ("B", b), ("C", c)):
        if res:
            print(f"{name}: n={res['n']} 改善{res['improve']} 恶化{res['hurt']} 不变{res['no_change']}")
        else:
            print(f"{name}: n=0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
