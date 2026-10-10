#!/usr/bin/env python3
"""
G4 翻向机制参数扫描（8/23 研究配套）

问题：G4/H9 的三个可调部件——触发阈值 τ、冲突裁决权、是否波动归一——历史最优在哪？

方法（独立决策规则扫描，非精确管线反事实；现行系统命中率为参照线）：
- 样本：全部可重放且有结算 actual 的场次；
- 每场特征：带符号偏差 dev、vol_5m、minutes_to_settle、净分方向 net_dir、
  G4 前方向 pre_g4（含 G2/G3 改判近似）、价格位置方向 price_dir=sign(dev)；
- 策略族：
  S1 阈值扫描：|dev|≥τ 跟价格位置，否则用 pre_g4。τ ∈ {0.05,0.10,0.15,0.20,0.25,0.30,∞}
  S2 z 扫描：z≥τz 才跟位置（波动×剩余时间归一）。τz ∈ {0.25,0.35,0.50,0.75}
  S3 两个极端：永远跟位置（纯价格）/ 永远不跟位置（pre_g4）
- 另统计：冲突子集（pre_g4≠price_dir 且被策略翻向）翻对率、翻错损失场数。

只读分析，不改规则。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from replay_common import get_latest_snapshots, load_results_actual, wilson, replay_judge_full  # noqa: E402

OUT_MD = Path(__file__).resolve().parent.parent / "data" / "hermes_g4_sweep_20260823.md"


def main() -> int:
    actuals = load_results_actual()
    snaps = get_latest_snapshots()
    rows = []
    for slug, p in sorted(snaps.items()):
        act = actuals.get(slug)
        if not act:
            continue
        try:
            md = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        rep = replay_judge_full(md)
        if rep is None:
            continue
        d, _f, inp = rep
        dev = inp.get("deviation_pct")
        vol = inp.get("volatility_5m")
        mins = inp.get("minutes_to_settle")
        pm_up = inp.get("pm_up")
        if dev is None:
            continue
        dev = float(dev)
        if abs(dev) < 1e-9:
            continue
        price_dir = "UP" if dev > 0 else "DOWN"
        net = float(d.s0 or 0) + float(d.pm_row or 0)
        net_dir = "UP" if net > 0 else ("DOWN" if net < 0 else "")
        risks = list(d.risks)
        pre = net_dir
        if any("改判顺共识" in x for x in risks) or any("反向保护" in x for x in risks):
            pre = ("UP" if (pm_up or 50) >= 50 else "DOWN") if pm_up is not None else net_dir
        z = None
        if vol and mins:
            z = abs(dev) / (float(vol) * ((max(float(mins), 1) / 5) ** 0.5))
        rows.append({"slug": slug, "dev": dev, "abs_dev": abs(dev), "z": z,
                     "price_dir": price_dir, "net_dir": net_dir, "pre": pre,
                     "final": d.final_direction, "actual": act})
    n = len(rows)
    cur_ok = sum(1 for r in rows if r["final"] == r["actual"])

    def acc(keyfn):
        ok = sum(1 for r in rows if keyfn(r) == r["actual"])
        return ok, len(rows)

    L = ["# G4 翻向机制参数扫描（8/23 研究）\n",
         f"- 样本 {n} 场；现行系统（完整管线）命中 {cur_ok}/{n}={cur_ok/n*100:.1f}%（参照线）",
         "- 注意：以下为独立决策规则扫描（从 G4 前方向出发），非精确管线反事实。\n"]

    L.append("## 一、极端对照：价格位置权重 0% vs 100%\n")
    never_ok, _ = acc(lambda r: r["pre"])
    always_ok, _ = acc(lambda r: r["price_dir"])
    l1, h1 = wilson(never_ok, n)
    l2, h2 = wilson(always_ok, n)
    L.append(f"- 永不跟位置（G4 前方向）：{never_ok}/{n}={never_ok/n*100:.1f}% [{l1:.0f},{h1:.0f}]")
    L.append(f"- 永远跟位置（无脑 sign(dev)）：{always_ok}/{n}={always_ok/n*100:.1f}% [{l2:.0f},{h2:.0f}]\n")

    L.append("## 二、S1 绝对阈值扫描（|dev|≥τ 才跟位置，否则用 G4 前方向）\n")
    L.append("| τ | 命中 | 胜率 | Wilson95% | 与τ=0.15差 |")
    L.append("|---|---|---|---|---|")
    base15 = None
    for tau in (0.05, 0.08, 0.10, 0.12, 0.15, 0.18, 0.20, 0.25, 0.30, 99.0):
        ok, _ = acc(lambda r, t=tau: r["price_dir"] if r["abs_dev"] >= t else r["pre"])
        lo, hi = wilson(ok, n)
        if tau == 0.15:
            base15 = ok
        name = "∞(永不)" if tau == 99.0 else f"{tau:.2f}"
        delta = "" if base15 is None else f"{ok-base15:+d}"
        L.append(f"| {name} | {ok}/{n} | {ok/n*100:.1f}% | [{lo:.0f},{hi:.0f}] | {delta} |")
    L.append("")

    L.append("## 三、S2 波动归一 z 阈值扫描（z≥τz 才跟位置）\n")
    L.append("| τz | 命中 | 胜率 |")
    L.append("|---|---|---|")
    for tz in (0.20, 0.25, 0.35, 0.50, 0.75):
        ok, _ = acc(lambda r, t=tz: r["price_dir"] if (r["z"] or 9) >= t else r["pre"])
        L.append(f"| ≥{tz:.2f} | {ok}/{n} | {ok/n*100:.1f}% |")
    L.append("")

    L.append("## 四、冲突子集解剖（G4 前方向逆价格位置，且 |dev|≥0.15%）\n")
    conf = [r for r in rows if r["abs_dev"] >= 0.15 and r["pre"] and r["pre"] != r["price_dir"]]
    flip_ok = sum(1 for r in conf if r["price_dir"] == r["actual"])
    keep_ok = sum(1 for r in conf if r["pre"] == r["actual"])
    if conf:
        lf, hf = wilson(flip_ok, len(conf))
        L.append(f"- 冲突场 {len(conf)} 场：翻向价格位置 {flip_ok}/{len(conf)}={flip_ok/len(conf)*100:.1f}% "
                 f"[{lf:.0f},{hf}] vs 保留原方向 {keep_ok}/{len(conf)}={keep_ok/len(conf)*100:.1f}%")
        L.append(f"- 翻错代价：{len(conf)-flip_ok} 场（若这两类场直接 SKIP 可省这些亏损，但违背 round13 全量交易原则，仅作参考）\n")
        L.append("| slug | dev% | z | 冲突前方向 | 位置方向 | 实际 | 翻向? |")
        L.append("|---|---|---|---|---|---|---|")
        for r in sorted(conf, key=lambda x: x["slug"]):
            L.append(f"| {r['slug'].replace('bitcoin-up-or-down-','')} | {r['dev']:+.2f} | {r['z']:.2f} | {r['pre']} | {r['price_dir']} | {r['actual']} | {'✅' if r['price_dir']==r['actual'] else '❌'} |")
    OUT_MD.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"样本 {n}；现行参照 {cur_ok}/{n}={cur_ok/n*100:.1f}%")
    print(f"永不跟位置 {never_ok}/{n}={never_ok/n*100:.1f}% | 永远跟位置 {always_ok}/{n}={always_ok/n*100:.1f}%")
    for tau in (0.10, 0.15, 0.20, 0.25):
        ok, _ = acc(lambda r, t=tau: r["price_dir"] if r["abs_dev"] >= t else r["pre"])
        print(f"S1 τ={tau:.2f}: {ok}/{n}={ok/n*100:.1f}%")
    print(f"报告写入 {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
