#!/usr/bin/env python3
"""分析提前分析时刻对方向判定正确度的影响（:15 首采 vs :28 次采双快照实证）。

核心问题：automation 从 :28 提前到 :05-:08 后，因子在开盘初期是否稳定/可用，
方向判定会不会翻、正确率会不会掉。

方法：
- 对 data/snapshots 下所有双快照合约，按 collect_time_utc 排序取首采/次采两份
- 分别用 signal_validator.extract_factors + rule_engine.judge 机算方向
- 统计：方向一致性、各自正确率、因子差异、缺失因子、分钟分布

用法：python3 scripts/analyze_timing_accuracy.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, "scripts")

from signal_validator import extract_factors, load_market_data, _SkipValidation
from rule_engine import judge

ROOT = Path("/Users/tangyijun/Desktop/hermes btc")
SNAP = ROOT / "data" / "snapshots"
RESULTS = ROOT / "data" / "hermes_results.jsonl"

ET = timezone(timedelta(hours=-4))
CN = timezone(timedelta(hours=8))


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


def collect_time(md: dict) -> datetime | None:
    ts = md.get("collect_time_utc") or md.get("timestamp")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def judge_snapshot(md: dict):
    """机算单份快照 → (direction, factors, inp, notes, mode)"""
    try:
        factors, inp, notes, mode = extract_factors(md)
    except _SkipValidation as e:
        return None, None, None, [str(e)], "SKIP"
    try:
        j = judge(inp)
        return j.final_direction, factors, inp, notes, mode
    except Exception as e:
        return None, None, None, [str(e)], f"judge_err:{e}"


def main() -> int:
    amap = actual_map()
    rows = []
    # slug -> [snapshot files]
    slugs: dict[str, list[Path]] = defaultdict(list)
    for p in SNAP.glob("*.json"):
        m = re.match(r"(.+)_\d{8}_\d{6}\.json$", p.name)
        if m:
            slugs[m.group(1)].append(p)

    for slug, files in slugs.items():
        if len(files) < 2:
            continue
        mds = []
        for p in files:
            try:
                md = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            ct = collect_time(md)
            if ct is None:
                continue
            mds.append((ct, md))
        if len(mds) < 2:
            continue
        mds.sort(key=lambda x: x[0])
        first_md = mds[0][1]
        last_md = mds[-1][1]
        first_ct = mds[0][0].astimezone(ET)
        last_ct = mds[-1][0].astimezone(ET)

        d1, f1, inp1, n1, mode1 = judge_snapshot(first_md)
        d2, f2, inp2, n2, mode2 = judge_snapshot(last_md)
        act = amap.get(slug)

        # 记录首采时间（判断是否真的在 :15 前后）
        rows.append({
            "slug": slug,
            "first_min": first_ct.minute,
            "last_min": last_ct.minute,
            "d1": d1, "d2": d2,
            "act": act,
            "mode1": mode1, "mode2": mode2,
            "f1": f1, "f2": f2,
            "notes1": n1, "notes2": n2,
            "pm_up1": (inp1 or {}).get("pm_up"),
            "pm_up2": (inp2 or {}).get("pm_up"),
            "dev1": (inp1 or {}).get("deviation_pct"),
            "dev2": (inp2 or {}).get("deviation_pct"),
        })

    n = len(rows)
    if n == 0:
        print("无可用样本")
        return 1

    # 有效（两时点都判出方向）的样本
    valid = [r for r in rows if r["d1"] and r["d2"]]
    print(f"总合约 {n} | 双时点均可判定 {len(valid)}")

    # ---- 方向一致性 ----
    same = [r for r in valid if r["d1"] == r["d2"]]
    flip = [r for r in valid if r["d1"] != r["d2"]]
    print(f"\n[方向一致性] 提前分析（首采）vs 现状（次采）")
    print(f"  方向相同: {len(same)}/{len(valid)} = {len(same)/len(valid)*100:.1f}%")
    print(f"  方向翻转: {len(flip)}/{len(valid)} = {len(flip)/len(valid)*100:.1f}%")

    # ---- 正确率（有 actual 的子集）----
    lab = [r for r in valid if r["act"] in ("UP", "DOWN")]
    h1 = sum(1 for r in lab if r["d1"] == r["act"])
    h2 = sum(1 for r in lab if r["d2"] == r["act"])
    print(f"\n[正确率]（有结算结果 {len(lab)} 场）")
    print(f"  首采(:15)方向正确: {h1}/{len(lab)} = {h1/len(lab)*100:.1f}%")
    print(f"  次采(:28)方向正确: {h2}/{len(lab)} = {h2/len(lab)*100:.1f}%")
    if flip and lab:
        flip_lab = [r for r in flip if r["act"] in ("UP", "DOWN")]
        if flip_lab:
            f1h = sum(1 for r in flip_lab if r["d1"] == r["act"])
            f2h = sum(1 for r in flip_lab if r["d2"] == r["act"])
            print(f"  翻转场次中首采正确: {f1h}/{len(flip_lab)} | 次采正确: {f2h}/{len(flip_lab)}")

    # ---- 因子差异统计（翻转场次）----
    print(f"\n[翻转场次明细]（{len(flip)} 场）")
    for r in flip[:20]:
        act_s = r["act"] or "?"
        mark = ""
        if r["act"] in ("UP", "DOWN"):
            mark = "首采✓" if r["d1"] == r["act"] else ("次采✓" if r["d2"] == r["act"] else "双错")
        print(f"  {r['slug']} | 首采:{r['d1']} 次采:{r['d2']} 实际:{act_s} {mark} | "
              f"首采min:{r['first_min']} 次采min:{r['last_min']} | pm_up {r['pm_up1']}→{r['pm_up2']} | dev {r['dev1']:.4f}→{r['dev2']:.4f}")

    # ---- 首采时刻分布 ----
    print(f"\n[首采时刻分钟分布]")
    mins = defaultdict(int)
    for r in rows:
        mins[r["first_min"]] += 1
    for m in sorted(mins):
        print(f"  :{m:>2} -> {mins[m]} 合约")

    # ---- 关键因子稳定性（全样本，两时点因子绝对差均值）----
    print(f"\n[因子稳定性]（首采 vs 次采，绝对值差均值，越小越稳定）")
    key_factors = ["price_position", "taker_bs", "taker_1h", "order_flow", "macd_15m", "macd_4h",
                   "kline", "seasonality", "short_bounce_factor", "open_micro", "wall_divergence", "wall_settlement"]
    diffs: dict[str, list[float]] = defaultdict(list)
    missing1: dict[str, int] = defaultdict(int)
    missing2: dict[str, int] = defaultdict(int)
    for r in valid:
        f1, f2 = r["f1"] or {}, r["f2"] or {}
        for k in key_factors:
            v1, v2 = f1.get(k), f2.get(k)
            if v1 is None:
                missing1[k] += 1
            if v2 is None:
                missing2[k] += 1
            if v1 is not None and v2 is not None:
                diffs[k].append(abs(v1 - v2))
    for k in key_factors:
        d = diffs.get(k)
        if d:
            print(f"  {k:<22} 均差 {sum(d)/len(d):.2f} | 变化率 {len([x for x in d if x>0])}/{len(d)} | 首采缺失 {missing1[k]} 次采缺失 {missing2[k]}")
        else:
            print(f"  {k:<22} 无双样本 | 首采缺失 {missing1[k]} 次采缺失 {missing2[k]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
