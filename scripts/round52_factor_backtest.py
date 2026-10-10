#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
round52_factor_backtest.py — round52 因子增删对引擎判定影响回测（8/30）
================================================================================
对比两版 factors 喂 judge 的最终方向：
  NEW (round52) = 当前 signal_validator.extract_factors() 输出（权威）
  OLD (round51.2模拟) = NEW 移除新增3因子（recent_dir_5m/15m + pm_consensus），
       加回删除4因子（taker_bs_15m / order_flow / open_micro / macro_trend），
       按旧口径从快照重算 + 结算期对 taker_bs_15m/order_flow 的 ×0.5 降权。

差异纯由 factors 集合/权重驱动（其余 inp 字段两版完全一致）。
命中率按 data/hermes_results.jsonl 实际结算方向。只读不改。
"""
import json, os, re, sys

HERMES_DIR = os.environ.get("HERMES_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERMES_DIR, "scripts"))

import factor_hitrate_study as base
import signal_validator as sv
from signal_validator import _f, _taker_grade
from rule_engine import judge


def _replay(md, factors_override=None):
    """复刻 _validate_with_md_impl 核心：extract_factors → extreme_wall 两遍 → judge。
    factors_override 非 None 时两遍 judge 均用其覆盖 factors（round51.2 模拟）。
    返回 (decision, factors, inp, mode) 或 None。"""
    try:
        factors, inp, notes, mode = sv.extract_factors(md)
    except Exception:
        return None
    if factors_override is not None:
        factors = factors_override
    buy_wall = inp.get("_buy_wall_ratio")
    sell_wall = inp.get("_sell_wall_ratio")
    taker_bs = inp.get("taker_bs_ratio")
    prelim = judge(dict(inp, factors=factors, extreme_wall=False), max_price=0.60)
    pd = prelim.final_direction
    if pd == "DOWN":
        ew = bool(sell_wall is not None and sell_wall > sv.WALL_RATIO)
    elif pd == "UP":
        ew = bool(buy_wall is not None and buy_wall > sv.WALL_RATIO)
    else:
        ew = False
    d = judge(dict(inp, factors=factors, extreme_wall=ew, _buy_wall_ratio=buy_wall,
                   _sell_wall_ratio=sell_wall, taker_bs_ratio=taker_bs), max_price=0.60)
    return d, factors, inp, mode


def _old_factors(md, factors, mode):
    """round51.2 模拟：NEW → 移除新增3因子 + 加回删除4因子（旧口径）。
    返回修改后的 factors 副本。"""
    f = dict(factors)
    # 1) 移除 round52 新增
    for k in ("recent_dir_5m", "recent_dir_15m", "pm_consensus"):
        f.pop(k, None)

    tf = md.get("timeframes") or {}
    cap = md.get("capital_flow") or {}
    of = cap.get("order_flow") or {}

    # 2) 加回 taker_bs_15m（顶层，×1）：taker_15m_ratio 分级（round51.4 无 ±2 极端档）
    t15 = _f(md.get("taker_15m_ratio"))
    if t15 is not None:
        f["taker_bs_15m"] = _taker_grade(t15) * sv.W_TAKER_BS_15M

    # 3) 加回 order_flow（顶层，×1）：imb5/imb15 双>0.05 → +1 / 双<-0.05 → -1 / 其余 0
    imb5 = _f(of.get("imbalance_5m"))
    imb15 = _f(of.get("imbalance_15m"))
    if imb5 is not None and imb15 is not None:
        w = sv.W_ORDER_FLOW_PARTIAL if of.get("partial") else sv.W_ORDER_FLOW
        if imb5 > 0.05 and imb15 > 0.05:
            f["order_flow"] = 1.0 * w
        elif imb5 < -0.05 and imb15 < -0.05:
            f["order_flow"] = -1.0 * w
        else:
            f["order_flow"] = 0.0

    # 4) 加回 open_micro（开盘/成型期）：opening.dir
    if mode in ("常规-开盘期", "常规-成型期"):
        open_dir = (md.get("opening") or {}).get("dir")
        if open_dir in ("UP", "DOWN"):
            f["open_micro"] = float(1 if open_dir == "UP" else -1)

    # 5) 加回 macro_trend（成型/博弈期）：macro_trend_1h_4h up≥3→+1/down≥3→-1，缺降级 consecutive_up
    if mode in ("常规-成型期", "常规-博弈期"):
        _mt = md.get("macro_trend_1h_4h") or {}
        _mt_up, _mt_dn = _mt.get("up_count"), _mt.get("down_count")
        if isinstance(_mt_up, int) and isinstance(_mt_dn, int) and _mt_up + _mt_dn == 4:
            f["macro_trend"] = float(1 if _mt_up >= 3 else (-1 if _mt_dn >= 3 else 0))
        else:
            _cu = (tf.get("1H") or {}).get("consecutive_up")
            if isinstance(_cu, int) and 0 <= _cu <= 6:
                f["macro_trend"] = float(1 if _cu >= 4 else (-1 if _cu <= 2 else 0))

    # 6) 结算期降权（旧版键表 taker_bs + taker_bs_15m + order_flow，round52 只剩 taker_bs）
    if mode == "常规-结算期":
        for k in ("taker_bs_15m", "order_flow"):
            v = f.get(k)
            if v:
                f[k] = float(int(v * 0.5 + (0.5 if v > 0 else -0.5)))
    return f


def main():
    snaps, cnt = base.load_last_snapshots()
    results = base.load_results()

    rows = []
    skipped = 0
    for slug in sorted(snaps):
        actual = results.get(slug)
        if actual not in ("UP", "DOWN"):
            continue
        md = snaps[slug]
        r = _replay(md)
        if r is None:
            skipped += 1
            continue
        d_new, factors_new, inp, mode = r
        old_factors = _old_factors(md, factors_new, mode)
        r_old = _replay(md, factors_override=old_factors)
        if r_old is None:
            skipped += 1
            continue
        d_old, _, _, _ = r_old
        rows.append({
            "slug": slug, "actual": actual, "mode": mode,
            "dir_new": d_new.final_direction, "net_new": d_new.net, "s0_new": d_new.s0,
            "dir_old": d_old.final_direction, "net_old": d_old.net, "s0_old": d_old.s0,
            "f_new": factors_new, "f_old": old_factors,
        })

    total = len(rows)
    diff = [r for r in rows if r["dir_new"] != r["dir_old"]]
    print(f"可回测场: {total}  跳过: {skipped}  方向差异场: {len(diff)}")

    def hit(rs, key):
        n = sum(1 for r in rs if r[key] in ("UP", "DOWN"))
        h = sum(1 for r in rs if r[key] == r["actual"])
        return n, h

    n_old, h_old = hit(rows, "dir_old")
    n_new, h_new = hit(rows, "dir_new")
    print(f"全量: round51.2 {h_old}/{n_old} = {h_old/n_old*100:.1f}%  "
          f"round52 {h_new}/{n_new} = {h_new/n_new*100:.1f}%  Δ={h_new-h_old}")

    if diff:
        h_d_old = sum(1 for r in diff if r["dir_old"] == r["actual"])
        h_d_new = sum(1 for r in diff if r["dir_new"] == r["actual"])
        print(f"\n差异场({len(diff)}): old 命中 {h_d_old}/{len(diff)} = {h_d_old/len(diff)*100:.1f}%  "
              f"new 命中 {h_d_new}/{len(diff)} = {h_d_new/len(diff)*100:.1f}%")
        print("\n## 差异场明细（slug | actual | old→new | mode | net)  |  新增因子 | 删除因子")
        for r in sorted(diff, key=lambda x: x["slug"]):
            fnew = r["f_new"]; fold = r["f_old"]
            add = {k: fnew[k] for k in ("recent_dir_5m", "recent_dir_15m", "pm_consensus") if fnew.get(k)}
            dele = {k: fold[k] for k in ("taker_bs_15m", "order_flow", "open_micro", "macro_trend") if fold.get(k)}
            print(f"- {r['slug']} | actual={r['actual']} | {r['dir_old']}(net{r['net_old']:+.0f}) "
                  f"→ {r['dir_new']}(net{r['net_new']:+.0f}) | {r['mode']}")
            if add: print(f"    [新增出票] {add}")
            if dele: print(f"    [旧删出票] {dele}")

    # 按模式分组命中率
    print("\n## 按模式分组")
    modes = sorted(set(r["mode"] for r in rows))
    for m in modes:
        rs = [r for r in rows if r["mode"] == m]
        n_old, h_old = hit(rs, "dir_old")
        n_new, h_new = hit(rs, "dir_new")
        d = sum(1 for r in rs if r["dir_new"] != r["dir_old"])
        print(f"- {m}: n={len(rs)}  old {h_old}/{n_old} = {h_old/n_old*100:.0f}%  "
              f"new {h_new}/{n_new} = {h_new/n_new*100:.0f}%  Δ={h_new-h_old}  方向差异{d}")

    # 净分变化分布
    net_chg = [abs(r["net_new"] - r["net_old"]) for r in rows]
    if net_chg:
        avg = sum(net_chg) / len(net_chg)
        gt2 = sum(1 for c in net_chg if c >= 2)
        print(f"\n净分 |Δ| 均值={avg:.2f}  净分变化≥2 的场数={gt2}/{len(net_chg)}")


if __name__ == "__main__":
    main()
