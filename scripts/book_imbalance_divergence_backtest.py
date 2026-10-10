#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
book_imbalance_divergence_backtest.py — 盘口背离预警回测（9/3，先回测再进打分）
=================================================================================
背景：用户归纳「庄家三招」共核 = 盘口失衡(快)与聪明钱/引擎判定(慢)方向背离。
预警器已挂 dashboard 抓两种「盘口陷阱」：
  ① 快慢打架    ：pm_imbalance(盘口量失衡) 方向 ≠ pm_consensus(盘口价共识) 方向
  ② 盘口反着引擎：pm_imbalance 方向 ≠ 引擎最终方向(final_direction)

本脚本回答：这些背离出现时，按引擎方向下注是不是真的更差？
  触发组命中率 vs 不触发组，差异显著才值得进引擎评分；否则是噪音。
铁律：只统计诊断，不改 rule_engine / signal_validator 任何权重。
口径：与 round52_factor_backtest 一致 —— sv.extract_factors + judge（权威引擎打分行）。
"""
import json, os, sys, math, glob

HERMES_DIR = os.environ.get("HERMES_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERMES_DIR, "scripts"))

import factor_hitrate_study as base
import signal_validator as sv
from rule_engine import judge

SNAP_DIR = os.path.join(HERMES_DIR, "data", "snapshots")


def load_book_snapshots():
    """slug -> md：每小时优先选「含 polymarket.book_imbalance」的最近快照；
    无任何含盘口字段的快照时，回退到最后一份（该场 pm_imbalance/pm_consensus 多为 0，归对照）。
    与 round52 口径差异：更多小时能带入真实盘口读数。"""
    best = {}  # slug -> (book_has, ts, md)
    for f in glob.glob(os.path.join(SNAP_DIR, "*.json")):
        base = os.path.basename(f)
        slug = base.rsplit("_", 2)[0]
        try:
            md = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        ts = base.rsplit("_", 2)[1] + base.rsplit("_", 2)[2].replace(".json", "")
        pm = md.get("polymarket") or {}
        book_has = ("book_imbalance" in pm) and ("book_up_mid" in pm)
        cur = best.get(slug)
        if cur is None or (book_has, ts) > (cur[0], cur[1]):
            best[slug] = (book_has, ts, md)
    return {k: v[2] for k, v in best.items()}


def _replay(md):
    """复刻权威引擎入口：extract_factors → extreme_wall 两遍 → judge。返回 (decision, factors, inp, mode)。"""
    try:
        factors, inp, notes, mode = sv.extract_factors(md)
    except Exception:
        return None
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


def _sgn(x, th=0.0):
    if x is None:
        return 0
    if x > th:
        return 1
    if x < -th:
        return -1
    return 0


def wilson(p, n, z=1.96):
    if n == 0:
        return (0.0, 1.0)
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0, center - half), min(1, center + half))


def main():
    snaps = load_book_snapshots()
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
        d, factors, inp, mode = r
        side = d.final_direction
        if side not in ("UP", "DOWN"):
            continue
        imb = factors.get("pm_imbalance", 0.0) or 0.0
        cons = factors.get("pm_consensus", 0.0) or 0.0
        up_price = (md.get("polymarket") or {}).get("up_price")
        try:
            up_price = float(up_price)
        except (TypeError, ValueError):
            up_price = None
        pm = md.get("polymarket") or {}
        def _num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        raw_imb = _num(pm.get("book_imbalance"))
        raw_mid = _num(pm.get("book_up_mid"))
        rows.append({
            "slug": slug, "actual": actual, "side": side, "mode": mode,
            "imb": float(imb), "cons": float(cons), "up_price": up_price,
            "raw_imb": raw_imb, "raw_mid": raw_mid,
        })

    total = len(rows)
    print("engine backtest 可回测场:", total, " 跳过:", skipped)
    if not rows:
        print("无数据")
        return

    # 逐场打标
    def sgns(x):
        return _sgn(x)
    for r in rows:
        sI = sgns(r["imb"]); sC = sgns(r["cons"])
        sS = 1 if r["side"] == "UP" else (-1 if r["side"] == "DOWN" else 0)
        r["sI"] = sI; r["sC"] = sC; r["sS"] = sS
        r["fight"] = (sI != 0 and sC != 0 and sI != sC)          # ① 快慢打架
        r["reverse"] = (sI != 0 and sS != 0 and sI != sS)        # ② 盘口反着引擎
        r["any"] = r["fight"] or r["reverse"]                    # 任一种触发
        # 原始连续口径（不去阈值，直接比方向，找背离"本质窗口"）
        ri = sgns(r["raw_imb"])
        rm = sgns((r["raw_mid"] or 0.5) - 0.5)
        r["sIraw"] = ri; r["sMraw"] = rm
        r["fight_raw"] = (ri != 0 and rm != 0 and ri != rm)
        r["reverse_raw"] = (ri != 0 and sS != 0 and ri != sS)
        r["any_raw"] = r["fight_raw"] or r["reverse_raw"]

    def stats(sub):
        n = len(sub)
        h = sum(1 for r in sub if r["side"] == r["actual"])
        rate = h / n if n else 0.0
        lo, hi = wilson(rate, n)
        # 朴素 EV：按快照隐含价入场，$100 本金
        ev_sum, n_ev = 0.0, 0
        for r in sub:
            p = r["up_price"]
            if p and 0.01 < p < 0.99:
                pe = p if r["side"] == "UP" else 1 - p
                ev_sum += ((100.0 / pe - 100.0) if r["side"] == r["actual"] else -100.0)
                n_ev += 1
        ev = round(ev_sum / n_ev, 1) if n_ev >= 10 else None
        return {"n": n, "h": h, "rate": rate, "ci": (lo, hi), "ev": ev}

    def row(label, sub):
        s = stats(sub)
        ascii_ev = f"{s['ev']:+.1f}" if s["ev"] is not None else "—"
        return (f"| {label} | {s['n']} | {s['h']} | {s['rate']*100:.1f}% | "
                f"[{s['ci'][0]*100:.0f}%,{s['ci'][1]*100:.0f}%] | {ascii_ev} |")

    base_all = stats(rows)
    fight_on = [r for r in rows if r["fight"]]
    fight_off = [r for r in rows if not r["fight"]]
    rev_on = [r for r in rows if r["reverse"]]
    rev_off = [r for r in rows if not r["reverse"]]
    any_on = [r for r in rows if r["any"]]
    any_off = [r for r in rows if not r["any"]]

    lines = []
    lines.append("# 盘口背离预警回测（先回测再进打分）")
    lines.append("")
    lines.append(f"生成: 引擎回测可归因样本 = **{total} 小时**")
    lines.append("")
    lines.append("口径：引擎方向=`sv.extract_factors→judge`（权威打分行）；命中=引擎方向==结算方向；")
    lines.append("朴素EV=按快照隐含价(up_price/1-up_price)入场，$100本金口径，近似真实执行。触发=背离条件成立。")
    lines.append("")
    lines.append("| 分组 | n | 命中 | 命中率 | 95%CI | 朴素EV/$100 |")
    lines.append("|---|---|---|---|---|---|")
    lines.append(row("全部(基线)", rows))
    lines.append("")
    lines.append("### ① 快慢打架（pm_imbalance 方向 ≠ pm_consensus 方向）")
    lines.append("| 分组 | n | 命中 | 命中率 | 95%CI | 朴素EV/$100 |")
    lines.append("|---|---|---|---|---|---|")
    lines.append(row("触发(打架)", fight_on))
    lines.append(row("不触发", fight_off))
    lines.append("")
    lines.append("### ② 盘口反着引擎（pm_imbalance 方向 ≠ 引擎方向）")
    lines.append("| 分组 | n | 命中 | 命中率 | 95%CI | 朴素EV/$100 |")
    lines.append("|---|---|---|---|---|---|")
    lines.append(row("触发(反引擎)", rev_on))
    lines.append(row("不触发", rev_off))
    lines.append("")
    lines.append("### ③ 任一种背离触发（∪，对应 dashboard 预警实际生效）")
    lines.append("| 分组 | n | 命中 | 命中率 | 95%CI | 朴素EV/$100 |")
    lines.append("|---|---|---|---|---|---|")
    lines.append(row("触发(任一种)", any_on))
    lines.append(row("不触发", any_off))
    lines.append("")
    # 原始连续口径：不去 pm_imbalance/pm_consensus 的阈值，直接比盘口方向（找背离本质）
    fr_on = [r for r in rows if r["fight_raw"]]
    fr_off = [r for r in rows if not r["fight_raw"]]
    rr_on = [r for r in rows if r["reverse_raw"]]
    rr_off = [r for r in rows if not r["reverse_raw"]]
    ar_on = [r for r in rows if r["any_raw"]]
    ar_off = [r for r in rows if not r["any_raw"]]
    lines.append("### ④ 原始口径 · 快慢打架（sign(book_imbalance) ≠ sign(book_up_mid-0.5)）")
    lines.append("| 分组 | n | 命中 | 命中率 | 95%CI | 朴素EV/$100 |")
    lines.append("|---|---|---|---|---|---|")
    lines.append(row("触发(打架·原始)", fr_on))
    lines.append(row("不触发", fr_off))
    lines.append("")
    lines.append("### ⑤ 原始口径 · 盘口反着引擎（sign(book_imbalance) ≠ 引擎方向）")
    lines.append("| 分组 | n | 命中 | 命中率 | 95%CI | 朴素EV/$100 |")
    lines.append("|---|---|---|---|---|---|")
    lines.append(row("触发(反引擎·原始)", rr_on))
    lines.append(row("不触发", rr_off))
    lines.append("")
    lines.append("### ⑥ 原始口径 · 任一种背离（∪）")
    lines.append("| 分组 | n | 命中 | 命中率 | 95%CI | 朴素EV/$100 |")
    lines.append("|---|---|---|---|---|---|")
    lines.append(row("触发(任一种·原始)", ar_on))
    lines.append(row("不触发", ar_off))
    lines.append("")
    # 判读
    def verdict(on, off, name):
        if on and off and len(on) >= 10 and len(off) >= 10 and on is not None:
            d = on["rate"] - off["rate"]
            big = "显著" if (on["n"] >= 15 and d <= -0.06) else ("略差" if d < 0 else "不差/更好")
            note = ("→ 背离时引擎更容易错，支持进引擎(降档/缩仓)" if big == "显著"
                    else "→ 差异不足，暂不构成进引擎依据(继续观察)");
            lines.append(f"### 判读 · {name}")
            lines.append(f"触发命中比不触发 {d*100:+.1f}pp（{on['n']}/{off['n']} 样本），判定：**{big}**。{note}")
        else:
            lines.append(f"### 判读 · {name}")
            lines.append("样本不足(<10)或区间无意义，不作结论。")
    verdict(fight_on and stats(fight_on), stats(fight_off), "快慢打架")
    verdict(rev_on and stats(rev_on), stats(rev_off), "盘口反着引擎")
    verdict(any_on and stats(any_on), stats(any_off), "任一种背离")
    verdict(fr_on and stats(fr_on), stats(fr_off), "原始口径·快慢打架")
    verdict(rr_on and stats(rr_on), stats(rr_off), "原始口径·盘口反着引擎")
    verdict(ar_on and stats(ar_on), stats(ar_off), "原始口径·任一种背离")
    lines.append("")
    lines.append("---")
    lines.append("**诚实声明**：样本内统计，未做多重比较校正；朴素EV与真实成交(taker@ask)有偏差；结论仅供进引擎设计参考，本轮不落地任何权重变更。")
    text = "\n".join(lines)

    out_path = os.path.join(HERMES_DIR, "docs", "book_imbalance_divergence_backtest_20260903.md")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fp:
        fp.write(text)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(text[:2600])
    print(f"\n完整报告已写入: {out_path}")


if __name__ == "__main__":
    main()