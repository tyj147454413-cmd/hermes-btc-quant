#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_engine_factors.py — 引擎口径因子验证（P0-1.3 第①步）
=============================================================
用 signal_validator.extract_factors() 机算每场快照的 factors（带模式门/权重/降权），
统计两类因子的真实命中率：
  A. 该砍候选（研究口径证伪，但引擎还在计分）：open_micro / order_flow / taker_bs /
     taker_bs_15m / kline_1h —— 验证它们在引擎口径下是否真噪音
  B. 添加候选（研究口径证实最强，但引擎未用）：recent_dir_5m / recent_dir_15m /
     MACD_5m / MACD加速度_15m / 历史时段UP率 / PM共识跟随(绝对价 book_up_mid)

只统计，不改任何代码。因子方向票 = sign(机算 value)，命中 = 方向票 == actual。
EV 按快照 polymarket.book_up_mid（缺退 up_price）隐含价，每 $100 本金。
"""
import json, math, os, sys, glob
from collections import defaultdict

HERMES_DIR = os.environ.get("HERMES_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERMES_DIR, "data")
SNAP_DIR = os.path.join(DATA, "snapshots")
sys.path.insert(0, os.path.join(HERMES_DIR, "scripts"))

import factor_hitrate_study as base
import signal_validator as sv

N_MIN = 30


def wilson(p, n, z=1.96):
    if n == 0:
        return (0.0, 1.0)
    den = 1 + z*z/n
    c = (p + z*z/(2*n)) / den
    h = z*math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / den
    return (max(0, c-h), min(1, c+h))


def _f(x):
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def main():
    snaps, _ = base.load_last_snapshots()
    results = base.load_results()

    # 每场：机算 factors + mode
    rows = []
    skipped = 0
    for slug in sorted(snaps):
        actual = results.get(slug)
        if actual not in ("UP", "DOWN"):
            continue
        md = snaps[slug]
        try:
            factors, inp, notes, mode = sv.extract_factors(md)
        except sv._SkipValidation as e:
            skipped += 1
            continue
        except Exception as e:
            skipped += 1
            continue
        rows.append({"slug": slug, "actual": actual, "factors": factors, "mode": mode, "md": md})

    print(f"可归因场: {len(rows)}  extract_factors 跳过: {skipped}")

    # 引擎因子的方向票统计
    def stat(field_getter, rows_subset=None):
        rs = rows_subset if rows_subset is not None else rows
        n = h = 0
        ev_tot = 0.0
        ev_n = 0
        for r in rs:
            v = field_getter(r)
            if v is None or v == 0:
                continue
            d = "UP" if v > 0 else "DOWN"
            n += 1
            if d == r["actual"]:
                h += 1
            p = _f((r["md"].get("polymarket") or {}).get("book_up_mid"))
            if p is None:
                p = _f((r["md"].get("polymarket") or {}).get("up_price"))
            if p and 0.01 < p < 0.99:
                pe = p if d == "UP" else 1 - p
                ev_tot += ((100.0/pe - 100.0) if d == r["actual"] else -100.0)
                ev_n += 1
        return n, h, (ev_tot/ev_n if ev_n >= 10 else None)

    def report(title, getters):
        lines = [f"\n## {title}\n"]
        lines.append("| 因子 | n | 命中 | 命中率 | 95%CI | EV/$100 | 判定 |")
        lines.append("|---|---:|---:|---:|---:|---:|---|")
        for name, g in getters:
            n, h, ev = stat(g)
            if n == 0:
                lines.append(f"| {name} | 0 | 0 | — | — | — | 未出票 |")
                continue
            hr = h/n
            lo, hi = wilson(hr, n)
            weak = n < N_MIN
            if weak:
                verdict = "样本不足"
            elif hr >= 0.58 and lo > 0.50:
                verdict = "🟢真信号"
            elif hr <= 0.42 and hi < 0.50:
                verdict = "🔴反向"
            else:
                verdict = "⚪噪音"
            ev_s = f"{ev:+.1f}" if ev is not None else "—"
            lines.append(f"| {name} | {n} | {h} | {hr*100:.1f}% | [{lo*100:.0f}%,{hi*100:.0f}%] | {ev_s} | {verdict} |")
        return "\n".join(lines)

    # ===== A. 该砍候选（引擎机算 value）=====
    def eng_factor(key):
        return lambda r: r["factors"].get(key, 0)

    print(report("A. 该砍候选（引擎机算口径）", [
        ("open_micro（开盘方向）", eng_factor("open_micro")),
        ("order_flow（订单流）", eng_factor("order_flow")),
        ("taker_bs（5m Taker）", eng_factor("taker_bs")),
        ("taker_bs_15m（15m Taker）", eng_factor("taker_bs_15m")),
        ("kline_1h（1H K线序列）", eng_factor("kline_1h")),
        ("kline_15m（15m K线序列，对照）", eng_factor("kline_15m")),
        ("price_position（dev符号，对照）", eng_factor("price_position")),
        ("pm_momentum（PM价动量，对照）", eng_factor("pm_momentum")),
        ("momentum_burst（放量顺势，对照）", eng_factor("momentum_burst")),
        ("pm_imbalance（PM挂单失衡，对照）", eng_factor("pm_imbalance")),
        ("macro_trend（宏观趋势，对照）", eng_factor("macro_trend")),
        ("short_bounce_factor（短反弹，对照）", eng_factor("short_bounce_factor")),
        ("taker_1m（贴线1m，对照）", eng_factor("taker_1m")),
        ("wall_divergence（墙背离，对照）", eng_factor("wall_divergence")),
        ("wall_settlement（结算墙，对照）", eng_factor("wall_settlement")),
    ]))

    # ===== B. 添加候选（引擎原始字段，研究证实最强）=====
    def rd(tfname):
        return lambda r: (1 if (r["md"].get("timeframes") or {}).get(tfname, {}).get("recent_dir") == "UP"
                          else -1 if (r["md"].get("timeframes") or {}).get(tfname, {}).get("recent_dir") == "DOWN"
                          else None)

    def macd5():
        def g(r):
            mh = (r["md"].get("timeframes") or {}).get("5m", {}).get("macd_hist")
            mh = _f(mh)
            return 1 if (mh is not None and mh > 0.5) else (-1 if (mh is not None and mh < -0.5) else None)
        return g

    def macd_accel15():
        def g(r):
            t = (r["md"].get("timeframes") or {}).get("15m", {})
            a, b = _f(t.get("macd_hist")), _f(t.get("macd_hist_prev"))
            if a is None or b is None:
                return None
            acc = a - b
            return 1 if acc > 0.25 else (-1 if acc < -0.25 else None)
        return g

    def hist_up_rate():
        def g(r):
            ur = (r["md"].get("historical") or {}).get("up_rate")
            ur = _f(ur)
            return 1 if (ur is not None and ur > 0.55) else (-1 if (ur is not None and ur < 0.45) else None)
        return g

    def pm_consensus():
        def g(r):
            p = _f((r["md"].get("polymarket") or {}).get("book_up_mid"))
            if p is None:
                p = _f((r["md"].get("polymarket") or {}).get("up_price"))
            return 1 if (p is not None and p > 0.60) else (-1 if (p is not None and p < 0.40) else None)
        return g

    print(report("B. 添加候选（引擎原始字段，研究证实最强）", [
        ("recent_dir_5m（5m 动量）", rd("5m")),
        ("recent_dir_15m（15m 动量）", rd("15m")),
        ("recent_dir_1H（1H 动量，对照）", rd("1H")),
        ("MACD_5m（5m MACD hist）", macd5()),
        ("MACD加速度_15m", macd_accel15()),
        ("历史时段UP率", hist_up_rate()),
        ("PM共识跟随（绝对价 book_up_mid）", pm_consensus()),
    ]))

    # ===== C. 交叉：该砍候选是否已被更强因子冗余覆盖 =====
    # 简单看 open_micro 与 recent_dir_5m 的共现（同为开盘段方向类）
    both = 0
    open_only_wrong = 0
    for r in rows:
        om = r["factors"].get("open_micro", 0)
        rd5 = rd("5m")(r)
        if om != 0 and rd5 is not None:
            both += 1
            if (1 if om > 0 else -1) != rd5:
                open_only_wrong += 1
    print(f"\n## C. 冗余检查\n- open_micro 与 recent_dir_5m 同时出票: {both} 场，方向不一致: {open_only_wrong} 场")


if __name__ == "__main__":
    main()