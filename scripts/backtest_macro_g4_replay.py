#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest_macro_g4_replay.py — 宏观趋势维度注入（round44.2 同步链补丁 8/30）对引擎判定影响回测
================================================================================================
对全部历史快照（每场最后一张）重放 judge 完整判定（复刻 _validate_with_md_impl 核心路径），
对比两版 inp：
  A. 修复后：extract_factors 注入真实 macro_trend_1h_4h（{up_count, down_count}）
  B. 修复前：inp["macro_trend_1h_4h"] = {}（模拟恒缺失 → G4 双向核对宏观维度恒不计数）

差异仅来自 judge G4 双向核对（24h位置 + 宏观趋势 + 4H MACD 3 维证据计数）。
输出：差异场数 / 明细 / 命中率影响（按 data/hermes_results.jsonl 实际结算方向）。

只读快照与结果，不改任何代码。
"""
import json, os, re, sys

HERMES_DIR = os.environ.get("HERMES_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERMES_DIR, "scripts"))

import factor_hitrate_study as base
import signal_validator as sv
from rule_engine import judge


def replay(md, inject_macro: bool):
    """复刻 _validate_with_md_impl 核心：extract_factors → extreme_wall 两遍 → judge。
    返回 (Decision, factors, inp, mode) 或 None（extract_factors 跳过/异常）。"""
    try:
        factors, inp, notes, mode = sv.extract_factors(md)
    except Exception:
        return None
    if not inject_macro:
        inp["macro_trend_1h_4h"] = {}  # 模拟修复前恒缺失（G4 宏观维度不计数）
    # 与 signal_validator._validate_with_md_impl 一致：两遍 judge 均用 dict(inp,...) 浅拷贝，
    # 原始 inp 保持完整（含 _buy_wall_ratio/_sell_wall_ratio/taker_bs_ratio 键）。
    buy_wall = inp.get("_buy_wall_ratio")
    sell_wall = inp.get("_sell_wall_ratio")
    taker_bs = inp.get("taker_bs_ratio")
    prelim = judge(dict(inp, extreme_wall=False), max_price=0.60)
    pd = prelim.final_direction
    if pd == "DOWN":
        ew = bool(sell_wall is not None and sell_wall > sv.WALL_RATIO)
    elif pd == "UP":
        ew = bool(buy_wall is not None and buy_wall > sv.WALL_RATIO)
    else:
        ew = False
    d = judge(dict(inp, extreme_wall=ew, _buy_wall_ratio=buy_wall,
                   _sell_wall_ratio=sell_wall, taker_bs_ratio=taker_bs), max_price=0.60)
    return d, factors, inp, mode


def _reconstruct_macro(md):
    """从快照 klines_1h 重构 macro_trend_1h_4h（与 hermes_collect._apply_slim round44 口径一致：
    klines_1h[-5:-1] 取 4 根已收盘 1H K线，阳/阴计数）。返回 dict 或 None（无 klines_1h）。"""
    k1h = md.get("klines_1h")
    if not isinstance(k1h, list) or len(k1h) < 2:
        return None
    last4 = k1h[-5:-1] if len(k1h) >= 5 else k1h[-4:]
    dirs = ["UP" if k.get("c", 0) >= k.get("o", 0) else "DOWN" for k in last4 if isinstance(k, dict)]
    if not dirs:
        return None
    return {
        "dirs": dirs,
        "up_count": sum(1 for d in dirs if d == "UP"),
        "down_count": sum(1 for d in dirs if d == "DOWN"),
        "first_ts": last4[0].get("t") if isinstance(last4[0], dict) else None,
        "last_ts": last4[-1].get("t") if isinstance(last4[-1], dict) else None,
    }


def _g4_dims(d) -> int | None:
    """从 judge trace 提取 G4 双向核对支持维度（无该行返回 None=G4 未触发）。"""
    for t in d.trace:
        m = re.search(r"G4 双向核对: 新方向=\S+ 支持维度=(\d+)/3", t)
        if m:
            return int(m.group(1))
    return None


def _g4_rolled(d) -> bool:
    return any("G4/H9 回退" in t or "G4/H9价格位置回退" in t for t in d.trace)


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
        # 原生 macro：快照自带；否则尝试用 klines_1h 重构（数据齐全敏感性）
        native = md.get("macro_trend_1h_4h") or {}
        recon = _reconstruct_macro(md) if not (native or {}).get("up_count") is not None else native
        a = replay(md, inject_macro=True)
        b = replay(md, inject_macro=False)
        if a is None or b is None:
            skipped += 1
            continue
        d_a, factors_a, inp_a, mode_a = a
        d_b, factors_b, inp_b, mode_b = b
        rows.append({
            "slug": slug, "actual": actual, "mode": mode_a,
            "dir_new": d_a.final_direction, "net_new": d_a.net,
            "dir_old": d_b.final_direction, "net_old": d_b.net,
            "g4_new": _g4_dims(d_a), "g4_old": _g4_dims(d_b),
            "g4_roll_new": _g4_rolled(d_a), "g4_roll_old": _g4_rolled(d_b),
            "native": bool((native or {}).get("up_count") is not None),
            "recon": recon,
        })

    total = len(rows)
    diff = [r for r in rows if r["dir_new"] != r["dir_old"]]
    print(f"可回测场: {total}  跳过: {skipped}  判定差异场(原生注入): {len(diff)}")

    # ---- 全量命中率（原生注入）----
    def hit(rows_sub, key):
        n = sum(1 for r in rows_sub if r[key] in ("UP", "DOWN"))
        h = sum(1 for r in rows_sub if r[key] == r["actual"])
        return n, h

    n_old, h_old = hit(rows, "dir_old")
    n_new, h_new = hit(rows, "dir_new")
    print(f"全量: 修复前 {h_old}/{n_old} = {h_old/n_old*100:.1f}%  修复后 {h_new}/{n_new} = {h_new/n_new*100:.1f}%")
    if diff:
        h_d_old = sum(1 for r in diff if r["dir_old"] == r["actual"])
        h_d_new = sum(1 for r in diff if r["dir_new"] == r["actual"])
        print(f"差异场({len(diff)}): 修复前命中 {h_d_old}/{len(diff)} = {h_d_old/len(diff)*100:.1f}%  "
              f"修复后命中 {h_d_new}/{len(diff)} = {h_d_new/len(diff)*100:.1f}%")
        print("\n## 差异场明细（slug | actual | 修复前→修复后 | mode | 宏观趋势）")
        for r in sorted(diff, key=lambda x: x["slug"]):
            mac = r["recon"] or {}
            mac_s = f"up={mac.get('up_count')}/down={mac.get('down_count')}" if isinstance(mac, dict) else "无"
            print(f"- {r['slug']} | actual={r['actual']} | "
                  f"{r['dir_old']}(net{r['net_old']:+.0f}) → {r['dir_new']}(net{r['net_new']:+.0f}) | "
                  f"{r['mode']} | {mac_s}")

    # ---- G4 触发与 macro 维度覆盖 ----
    n_native = sum(1 for r in rows if r["native"])
    n_recon = sum(1 for r in rows if r["recon"])
    g4_old = [r for r in rows if r["g4_old"] is not None]
    g4_new = [r for r in rows if r["g4_new"] is not None]
    print(f"\n快照原生含 macro: {n_native}/{total}  |  可重构 macro(klines_1h): {n_recon}/{total}")
    print(f"G4 双向核对触发(修复前): {len(g4_old)}  (修复后): {len(g4_new)}")

    # G4 触发场中 macro 维度是否改变支持维度
    g4_same = sum(1 for r in g4_old if r["g4_old"] == r["g4_new"])
    g4_diff_dims = [r for r in g4_old if r["g4_old"] != r["g4_new"]]
    print(f"G4 触发场中支持维度不变: {g4_same}/{len(g4_old)}  |  维度变化: {len(g4_diff_dims)}")
    for r in g4_diff_dims[:20]:
        mac = r["recon"] or {}
        mac_s = f"up={mac.get('up_count')}/down={mac.get('down_count')}" if isinstance(mac, dict) else "无"
        print(f"  - {r['slug']} actual={r['actual']} G4维度 {r['g4_old']}→{r['g4_new']} | "
              f"{r['dir_old']}→{r['dir_new']} | {r['mode']} | macro={mac_s}")

    # ---- 数据齐全敏感性：重构 macro 下有多少场 G4 会翻转判定 ----
    # 对 G4 触发且 native=0 的场，若重构 macro 使 support_dims 跨过 ≥2，则修复后会改变判定
    sens = []
    for r in rows:
        if r["g4_old"] is None:
            continue  # G4 未触发
        mac = r["recon"]
        if not isinstance(mac, dict):
            continue
        # 模拟：如果注入重构 macro，support_dims 会不会从 <2 升到 ≥2
        # 旧维度 g4_old 是注入空 macro 的基线；注入后需重跑才准，这里用近似：
        # 直接重跑一遍 inject=True 且强制用重构 macro
        pass
    # 精确法：对重构 macro 的场重跑，对比（空macro vs 重构macro）最终方向
    sens_rows = []
    for r in rows:
        if not r["recon"]:
            continue
        md = snaps[r["slug"]]
        md2 = dict(md)
        md2["macro_trend_1h_4h"] = r["recon"]
        c = replay(md2, inject_macro=True)   # 重构 macro 注入
        if c is None:
            continue
        d_c = c[0]
        sens_rows.append({
            "slug": r["slug"], "actual": r["actual"],
            "dir_base": r["dir_old"], "dir_full": d_c.final_direction,
            "g4_base": r["g4_old"], "g4_full": _g4_dims(d_c),
            "macro": r["recon"],
        })
    sens_diff = [x for x in sens_rows if x["dir_base"] != x["dir_full"]]
    print(f"\n## 数据齐全敏感性（重构 macro，仅含 klines_1h 场 {len(sens_rows)} 场）")
    print(f"方向变化: {len(sens_diff)} 场")
    if sens_diff:
        for x in sorted(sens_diff, key=lambda z: z["slug"]):
            mac = x["macro"] or {}
            mac_s = f"up={mac.get('up_count')}/down={mac.get('down_count')}" if isinstance(mac, dict) else "无"
            old_hit = x["dir_base"] == x["actual"]
            new_hit = x["dir_full"] == x["actual"]
            print(f"  - {x['slug']} actual={x['actual']} {x['dir_base']}({old_hit and '对' or '错'}) "
                  f"→ {x['dir_full']}({new_hit and '对' or '错'}) | G4 {x['g4_base']}→{x['g4_full']} | macro={mac_s}")
        n_h = sum(1 for x in sens_diff if x["dir_base"] == x["actual"])
        print(f"  基线命中 {n_h}/{len(sens_diff)}，全注入后命中 "
              f"{sum(1 for x in sens_diff if x['dir_full'] == x['actual'])}/{len(sens_diff)}")


if __name__ == "__main__":
    main()
