#!/usr/bin/env python3
"""盘感/趋势新因子 全样本判别力回测（2026-08-17）。

目的：在落地这些因子进评分项目之前，先看每个新因子对 1H 方向的**单独判别力**，
以及"多因子综合投票"是否超出现有引擎基线。数据说话，判别力不足的因子不启用。

新因子（设计文档 data/hermes_trend_softskill_factors_design_20260817.md）：
  机算类：momentum_health（动量衰竭）、exhaustion（动量耗尽）、s_r_swap（支撑阻力互换）、acceleration（加速放量）
  AI 类：fake_breakout（假突破）、divergence（背离）→ 无法机算，标 N/A
  对比用：trend_backing（大周期背书，已否决，仅作对比参考）

判定标准：某因子信号方向 vs 实际结算，n≥30 且 Wilson95 下界>50% 才有判别力；
下界≤50% 或 n<30 → 不作为方向因子启用。

用法：python3 scripts/softskill_factor_backtest.py
输出：data/hermes_softskill_factor_backtest_20260817.md
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path("/Users/tangyijun/Desktop/hermes btc")
RESULTS = ROOT / "data" / "hermes_results.jsonl"
SNAP = ROOT / "data" / "snapshots"
OUT = ROOT / "data" / "hermes_softskill_factor_backtest_20260817.md"

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


def _f(x):
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _sign(x):
    if x is None:
        return 0
    return 1 if x > 0 else (-1 if x < 0 else 0)


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


def compute_factors(md: dict) -> dict:
    """独立机算各新因子（不受 signal_validator ENABLE_SOFT_SKILL_FACTORS 开关影响）。
    返回 {factor_key: 信号方向(int)}，+1 偏多 / -1 偏空 / 0 未触发。
    """
    tf15m = (md.get("timeframes") or {}).get("15m") or {}
    tf4h = (md.get("timeframes") or {}).get("4H") or {}
    tf1h = (md.get("timeframes") or {}).get("1H") or {}
    current = _f(md.get("current_price"))
    macd4 = _f(tf4h.get("macd_hist"))
    out: dict[str, int] = {}

    # momentum_health（动量衰竭）：多头衰竭→偏空-1，空头衰竭→偏多+1
    accel4 = _f(tf4h.get("macd_acceleration"))
    ms = _sign(macd4)
    if ms > 0 and accel4 is not None and accel4 < 0:
        out["momentum_health"] = -1
    elif ms < 0 and accel4 is not None and accel4 > 0:
        out["momentum_health"] = 1
    else:
        out["momentum_health"] = 0

    # exhaustion（动量耗尽）：连续同色≥5 + 实体缩小
    cu15 = int(_f(tf15m.get("consecutive_up")) or 0)
    cd15 = int(_f(tf15m.get("consecutive_down")) or 0)
    rcs = tf15m.get("recent_candles") or []
    shrink = False
    if isinstance(rcs, list) and len(rcs) >= 2:
        try:
            b0 = abs(float(rcs[-1].get("c", 0)) - float(rcs[-1].get("o", 0)))
            b1 = abs(float(rcs[-2].get("c", 0)) - float(rcs[-2].get("o", 0)))
            shrink = 0 <= b0 < b1
        except (TypeError, ValueError):
            shrink = False
    if cu15 >= 5 and shrink:
        out["exhaustion"] = -1
    elif cd15 >= 5 and shrink:
        out["exhaustion"] = 1
    else:
        out["exhaustion"] = 0

    # s_r_swap（支撑阻力互换）
    kl = md.get("key_levels") or {}
    ss = _f(kl.get("strong_support"))
    sr = _f(kl.get("strong_resistance"))
    rdir15 = tf15m.get("recent_dir")
    if ss is not None and current is not None and current < ss and rdir15 == "DOWN":
        out["s_r_swap"] = -1
    elif sr is not None and current is not None and current > sr and rdir15 == "UP":
        out["s_r_swap"] = 1
    else:
        out["s_r_swap"] = 0

    # acceleration（加速放量）
    vr15 = _f(tf15m.get("vol_ratio"))
    if vr15 is not None and vr15 > 1.5 and rdir15 in ("UP", "DOWN"):
        out["acceleration"] = 1 if rdir15 == "UP" else -1
    else:
        out["acceleration"] = 0

    # trend_backing（对比参考，已否决）：4H方向与价格位置同向
    dev_pct = _f(md.get("deviation_pct"))
    dev_s = _sign(dev_pct)
    if _sign(macd4) != 0 and dev_s == _sign(macd4):
        out["trend_backing"] = _sign(macd4)
    else:
        out["trend_backing"] = 0

    # AI 类（无法机算）→ N/A
    out["fake_breakout"] = None
    out["divergence"] = None
    return out


def main() -> int:
    amap = actual_map()
    factor_keys = ["momentum_health", "exhaustion", "s_r_swap", "acceleration", "trend_backing"]
    stats = {k: {"n": 0, "k": 0} for k in factor_keys}
    rows = []

    for slug, act in amap.items():
        md = load_latest_snapshot(slug)
        if not md:
            continue
        fac = compute_factors(md)
        rows.append({"slug": slug.split("august-")[-1], "act": act, "fac": fac})
        for k in factor_keys:
            v = fac[k]
            if v in (1, -1):
                stats[k]["n"] += 1
                if (v == 1 and act == "UP") or (v == -1 and act == "DOWN"):
                    stats[k]["k"] += 1

    # 多因子综合投票（各机算因子×1 求和，AI因子忽略）
    combo = {"n": 0, "k": 0}
    for r in rows:
        s = sum(r["fac"].get(k, 0) for k in factor_keys)
        if s == 0:
            continue
        combo["n"] += 1
        pred = "UP" if s > 0 else "DOWN"
        if pred == r["act"]:
            combo["k"] += 1

    # 报告
    lines = []
    lines.append("# 盘感/趋势新因子 全样本判别力回测（2026-08-17）\n")
    lines.append(f"> 样本：有快照+结算 {len(rows)} 场。保本线=方向正确50%；Wilson95 下界>50% 且 n≥30 才有判别力。")
    lines.append(f"> 因子信号方向 vs 实际结算：+1 偏多 / -1 偏空 / 0 未触发（不计入）。\n")

    lines.append("## 单因子判别力\n")
    lines.append("| 因子 | 触发 n | 正确 | 点估计 | Wilson95下界 | 判定 |")
    lines.append("|------|------|------|------|------|------|")
    for k in factor_keys:
        st = stats[k]
        n, kk = st["n"], st["k"]
        if n == 0:
            lines.append(f"| {k} | 0 | - | - | - | 未触发 |")
            continue
        wl = wilson_lower(kk, n)
        if n < 30:
            verdict = f"⚠️ n={n}<30 样本不足"
        elif wl > BREAKEVEN and kk / n > 0.55:
            verdict = "✅ 有判别力"
        else:
            verdict = "❌ 判别力不足"
        lines.append(f"| {k} | {n} | {kk} | {kk/n*100:.1f}% | {wl*100:.1f}% | {verdict} |")
    lines.append("")

    lines.append("## 多因子综合投票（各×1 求和）\n")
    if combo["n"] > 0:
        wl = wilson_lower(combo["k"], combo["n"])
        verdict = ("✅ 有判别力" if (wl > BREAKEVEN and combo["k"] / combo["n"] > 0.55) else "❌ 判别力不足")
        lines.append(f"- 触发 {combo['n']} 场，正确 {combo['k']} = {combo['k']/combo['n']*100:.1f}%，下界 {wl*100:.1f}% → {verdict}")
    lines.append("- 现有引擎基线（rule_engine 全口径）：120 场 75.8%（下界 67.4%）——新因子须显著超出才算有增量价值。")
    lines.append("")

    lines.append("## 逐场明细\n")
    for k in factor_keys:
        sub = [r for r in rows if r["fac"][k] in (1, -1)]
        if not sub:
            continue
        lines.append(f"### {k}（触发 {len(sub)} 场）\n")
        for r in sub:
            v = r["fac"][k]
            ok = "✅" if ((v == 1 and r["act"] == "UP") or (v == -1 and r["act"] == "DOWN")) else "❌"
            lines.append(f"- {r['slug']}：因子{'偏多' if v==1 else '偏空'} 实际{r['act']} {ok}")
        lines.append("")

    OUT.write_text("\n".join(lines) + "\n")
    print(f"已生成 {OUT}\n")
    print(f"样本 {len(rows)} 场。单因子：")
    for k in factor_keys:
        st = stats[k]
        n, kk = st["n"], st["k"]
        if n:
            print(f"  {k}: n={n} 正确率 {kk/n*100:.1f}% 下界 {wilson_lower(kk,n)*100:.1f}%")
    if combo["n"]:
        print(f"  综合投票: n={combo['n']} 正确率 {combo['k']/combo['n']*100:.1f}% 下界 {wilson_lower(combo['k'],combo['n'])*100:.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
