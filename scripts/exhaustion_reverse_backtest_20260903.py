#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
exhaustion_reverse_backtest_20260903.py — exhaustion 因子反用版回测（2026-09-03 用户指令）
==========================================================================================
背景：全因子回测（factor_hitrate_all_20260903）发现休眠因子 exhaustion 原语义
「15m 连续≥5 根同色 + 末根实体缩小 → 判耗尽反转」实测命中率仅 31.0%（n=42），
实为 69% 续势信号。本脚本验证反用版（同条件 → 投续势方向）是否值得入引擎。

必答三问：
  Q1 反用后命中率/EV 达标吗？（判定线同 P0-1.3：n≥30、≥58% 且 CI 下界>50%）
  Q2 反用票与在役因子 recent_dir_15m 是否冗余？（连涨5根必然 15m 方向向上，
     若反用票只是 recent_dir_15m 的子集且无增量命中率，则加了白加、重复计分）
  Q3 参数稳健吗？（根数阈值 4/5 × 实体缩小条件有无，四变体 + 按周切片）

只读数据不改引擎。输出：控制台 + data/exhaustion_reverse_backtest_20260903.md
"""
import json, math, os, re, sys, glob
from collections import defaultdict

HERMES_DIR = os.environ.get("HERMES_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERMES_DIR, "data")
sys.path.insert(0, os.path.join(HERMES_DIR, "scripts"))

import factor_hitrate_study as base

N_MIN = 30
OUT = os.path.join(DATA, "exhaustion_reverse_backtest_20260903.md")


def wilson(p, n, z=1.96):
    if n == 0:
        return (0.0, 1.0)
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0, c - h), min(1, c + h))


def _f(x):
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def body_shrinking(rcs):
    """末根实体 < 前根实体（signal_validator L667-675 原逻辑）"""
    if isinstance(rcs, list) and len(rcs) >= 2:
        try:
            b0 = abs(float(rcs[-1].get("c", 0)) - float(rcs[-1].get("o", 0)))
            b1 = abs(float(rcs[-2].get("c", 0)) - float(rcs[-2].get("o", 0)))
            return 0 <= b0 < b1
        except (TypeError, ValueError):
            return False
    return False


def exhaustion_vote(md, min_run=5, need_shrink=True):
    """反用版：连续≥min_run 同色(+可选实体缩小) → 投续势方向。
    返回 'UP'/'DOWN'/None（None=未出票）。原版投票取反即得。"""
    tf15m = (md.get("timeframes") or {}).get("15m") or {}
    cu = int(_f(tf15m.get("consecutive_up")) or 0)
    cd = int(_f(tf15m.get("consecutive_down")) or 0)
    shrink = body_shrinking(tf15m.get("recent_candles"))
    if need_shrink and not shrink:
        return None
    if cu >= min_run:
        return "UP"    # 原版判多头耗尽(-1) → 反用续涨
    if cd >= min_run:
        return "DOWN"  # 原版判空头耗尽(+1) → 反用续跌
    return None


def rd15_vote(md):
    rd = ((md.get("timeframes") or {}).get("15m") or {}).get("recent_dir")
    return rd if rd in ("UP", "DOWN") else None


def slug_week(slug):
    m = re.search(r"(august|september)-(\d+)-(\d{4})", slug.lower())
    if not m:
        return None
    mon = {"august": 8, "september": 9}[m.group(1)]
    d = int(m.group(2))
    import datetime
    return datetime.date(int(m.group(3)), mon, d).isocalendar()[1]


def stat(rows, vote_fn):
    n = h = 0
    ev_tot, ev_n = 0.0, 0
    for r in rows:
        v = vote_fn(r["md"])
        if v is None:
            continue
        n += 1
        if v == r["actual"]:
            h += 1
        p = _f((r["md"].get("polymarket") or {}).get("book_up_mid"))
        if p is None:
            p = _f((r["md"].get("polymarket") or {}).get("up_price"))
        if p and 0.01 < p < 0.99:
            pe = p if v == "UP" else 1 - p
            ev_tot += ((100.0 / pe - 100.0) if v == r["actual"] else -100.0)
            ev_n += 1
    return n, h, (ev_tot / ev_n if ev_n >= 10 else None)


def table(title, rows, variants, lines):
    lines.append(f"\n### {title}\n")
    lines.append("| 变体 | n | 命中 | 命中率 | 95%CI | EV/$100 | 判定 |")
    lines.append("|---|---:|---:|---:|---:|---:|---|")
    for name, fn in variants:
        n, h, ev = stat(rows, fn)
        if n == 0:
            lines.append(f"| {name} | 0 | — | — | — | — | 未出票 |")
            continue
        hr = h / n
        lo, hi = wilson(hr, n)
        if n < N_MIN:
            v = "样本不足"
        elif hr >= 0.58 and lo > 0.50:
            v = "🟢真信号(可入引擎)"
        elif hr >= 0.53:
            v = "🟡弱"
        else:
            v = "⚪噪音"
        ev_s = f"{ev:+.1f}" if ev is not None else "—"
        lines.append(f"| {name} | {n} | {h} | {hr*100:.1f}% | [{lo*100:.0f}%,{hi*100:.0f}%] | {ev_s} | {v} |")


def main():
    rows, skipped, total = [], 0, 0
    snaps, _ = base.load_last_snapshots()
    results = base.load_results()
    for slug in sorted(snaps):
        actual = results.get(slug)
        if actual not in ("UP", "DOWN"):
            continue
        md = snaps[slug]
        rows.append({"slug": slug, "actual": actual, "md": md, "week": slug_week(slug)})
    print(f"可归因场: {len(rows)}")

    lines = ["# exhaustion 反用版回测（2026-09-03）", "",
             f"- 数据：{len(rows)} 个已结算场 × 每场最后一份快照（引擎口径）",
             "- 反用版语义：15m 连续同色(+实体缩小) → 投**续势**方向（原版投反转方向实测仅 31%）",
             "- 判定线：n≥30；≥58% 且 CI 下界>50% = 真信号"]

    variants = [
        ("原版(≥5根+实体缩小,反用)", lambda md: exhaustion_vote(md, 5, True)),
        ("变体A(≥5根,不看实体缩小)", lambda md: exhaustion_vote(md, 5, False)),
        ("变体B(≥4根+实体缩小)", lambda md: exhaustion_vote(md, 4, True)),
        ("变体C(≥4根,不看实体缩小)", lambda md: exhaustion_vote(md, 4, False)),
    ]
    table("Q1/Q3：反用版四变体", rows, variants, lines)

    # Q2 冗余检查
    n_ex = hit_ex = 0
    same_rd = 0
    rd_only_hit = 0
    n_rd_same = 0
    for r in rows:
        v = exhaustion_vote(r["md"], 5, True)
        if v is None:
            continue
        n_ex += 1
        if v == r["actual"]:
            hit_ex += 1
        rd = rd15_vote(r["md"])
        if rd == v:
            same_rd += 1
            n_rd_same += 1
            if v == r["actual"]:
                rd_only_hit += 1
    lines.append("\n### Q2：与 recent_dir_15m 的冗余检查\n")
    lines.append(f"- exhaustion 反用票（原版条件）共 {n_ex} 场，其中与 recent_dir_15m 同向：{same_rd} 场（{same_rd/n_ex*100:.0f}%）" if n_ex else "- 未出票")
    if n_rd_same:
        lines.append(f"- 重叠场中 recent_dir_15m 单独投票命中率：{rd_only_hit}/{n_rd_same}={rd_only_hit/n_rd_same*100:.1f}%")
        lines.append(f"- exhaustion 反用票在**全部** {n_ex} 场命中率：{hit_ex}/{n_ex}={hit_ex/n_ex*100:.1f}%")
        inc = hit_ex / n_ex - rd_only_hit / n_rd_same
        lines.append(f"- 增量：{'有' if inc > 0.03 else '基本无（|差|≤3pp，属重复计分风险）'}（差 {inc*100:+.1f}pp）")
    lines.append("- 结论要点：连涨5根时 recent_dir_15m 几乎必为同向，反用票若与之高度重叠，"
                 "入引擎只会在已计分的 15m 动量上再 +1，等于变相给 recent_dir_15m 加权，而非新信息。")

    # 按周切片（原版条件反用）
    weeks = defaultdict(list)
    for r in rows:
        if r["week"]:
            weeks[r["week"]].append(r)
    lines.append("\n### 时间切片（原版条件反用，按 ISO 周）\n")
    lines.append("| 周 | n | 命中 | 命中率 |")
    lines.append("|---|---:|---:|---:|")
    for wk in sorted(weeks):
        n, h, _ = stat(weeks[wk], lambda md: exhaustion_vote(md, 5, True))
        if n:
            lines.append(f"| {wk} 周 | {n} | {h} | {h/n*100:.1f}% |")

    text = "\n".join(lines)
    print(text)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(f"\n[落盘] {OUT}")


if __name__ == "__main__":
    main()
