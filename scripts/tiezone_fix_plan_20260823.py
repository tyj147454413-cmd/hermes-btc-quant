#!/usr/bin/env python3
"""
贴线区专项研究·第三步：胜率修复方案对比（8/23）。

用户约束：固定 $10 注额（无半仓概念）、只接受两结果（涨/跌）、目标=提高胜率。
本脚本回答唯一关键问题：
  在投票器判定的"危险场次"（顺引擎票数 ≤1）里，
  换任何一种方向判法（因子多数/PM/位置/Taker/动量/上一小时），
  能不能赢过"保留引擎方向"？

同时检验第二条提胜率路径：贴线区多快照样本里，"晚一点决策"（用更晚快照）
是否比"早决策"胜率更高 —— 即"强制复核/延迟下单"的数据依据。

输出：data/hermes_tiezone_fix_plan_20260823.md
只读分析，不改规则。
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from replay_common import get_latest_snapshots, load_results_actual, load_results_full, wilson, replay_judge_full  # noqa: E402
from tiezone_factor_scan_20260823 import build_rows, TIE_MAX  # noqa: E402
from datetime import datetime

OUT_MD = Path(__file__).resolve().parent.parent / "data" / "hermes_tiezone_fix_plan_20260823.md"
SNAP_DIR = Path(__file__).resolve().parent.parent / "data" / "snapshots"


def _f(v):
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def factor_dirs(F):
    out = []
    t = F.get("taker_now")
    if t is not None and t != 1:
        out.append("UP" if t > 1 else "DOWN")
    mh = F.get("mh_5m")
    if mh in ("UP", "DOWN"):
        out.append(mh)
    ls = F.get("ls_slope")
    if ls is not None and ls != 0:
        out.append("UP" if ls > 0 else "DOWN")
    ka = F.get("kl_asym")
    if ka is not None and abs(ka) > 1e-9:
        out.append("UP" if ka > 0 else "DOWN")
    return out


def majority_dir(r):
    ds = factor_dirs(r["F"])
    up, dn = ds.count("UP"), ds.count("DOWN")
    if up > dn:
        return "UP"
    if dn > up:
        return "DOWN"
    return None


def make_rules():
    def pm_dir(r):
        p = r["F"].get("pm_up")
        return None if p is None else ("UP" if p >= 0.5 else "DOWN")

    def taker_dir(r):
        t = r["F"].get("taker_now")
        return None if t is None or t == 1 else ("UP" if t > 1 else "DOWN")

    def pos_dir(r):
        return "UP" if r["dev"] > 0 else "DOWN"

    def rd15(r):
        return r["F"].get("rd_15m") or None

    def prev_dir(r):
        return r["F"].get("prev_dir_hit") or None

    return [
        ("R0 保留引擎方向（现状）", lambda r: r["final"]),
        ("R1 危险场跟四因子多数", lambda r: majority_dir(r) or r["final"]),
        ("R2 危险场跟PM方向", lambda r: pm_dir(r) or r["final"]),
        ("R3 危险场跟Taker即时", lambda r: taker_dir(r) or r["final"]),
        ("R4 危险场跟recent_dir 15m", lambda r: rd15(r) or r["final"]),
        ("R5 危险场跟价格位置sign(dev)", lambda r: pos_dir(r)),
        ("R6 危险场跟上一小时方向", lambda r: prev_dir(r) or r["final"]),
        ("R7 因子多数,平票跟PM", lambda r: majority_dir(r) or pm_dir(r) or r["final"]),
    ]


def main() -> int:
    rows = build_rows()
    for r in rows:
        ds = factor_dirs(r["F"])
        r["votes"] = sum(1 for d in ds if d == r["final"])
    full = load_results_full()
    med_prices = sorted(float(full[s]["buy_price"]) for s in full if full[s].get("buy_price"))
    med_price = med_prices[len(med_prices) // 2] if med_prices else 0.55

    def pnl_if(slug, direction, actual):
        """固定$10注额、按记录买价的单场盈亏。"""
        rec = full.get(slug) or {}
        price = float(rec.get("buy_price") or med_price)
        return (10.0 / price - 10.0) if direction == actual else -10.0

    base_wins = sum(1 for r in rows if r["win"])
    base_pnl = sum(pnl_if(r["slug"], r["final"], r["actual"]) for r in rows)

    L = [
        "# 贴线区胜率修复方案对比（8/23·第三步）\n",
        f"- 样本：|dev|<0.15% 共 {len(rows)} 场；现状命中 {base_wins}/{len(rows)}={base_wins/len(rows)*100:.1f}%，"
        f"模拟P&L {base_pnl:+.2f}U（固定$10、按各场记录买价）；",
        "- 用户约束：固定注额无半仓 → 只比较【方向判法】与【是否跳过】两类手段；\n",
    ]

    # ================= 一、危险场次换判法 =================
    low = [r for r in rows if r["votes"] <= 1]
    lw = sum(1 for r in low if r["win"])
    llo, lhi = wilson(lw, len(low))
    L.append("## 一、核心实验：危险场次（≤1票）里换判法，谁能赢过引擎？\n")
    L.append(f"- 危险场次共 {len(low)} 场，保留引擎命中 {lw}/{len(low)}={lw/len(low)*100:.1f}% [{llo:.0f},{lhi:.0f}]；\n")
    L.append("| 判法 | 危险场命中 | 危险场胜率 | 全区合成命中 | 全区胜率 | Δvs现状 | 合成ΔP&L |")
    L.append("|---|---|---|---|---|---|---|")
    results = []
    for name, fn in make_rules():
        low_hits = sum(1 for r in low if fn(r) == r["actual"])
        comp_hits = sum(1 for r in rows if (r["final"] if r["votes"] >= 2 else fn(r)) == r["actual"])
        comp_pnl = sum(pnl_if(r["slug"], (r["final"] if r["votes"] >= 2 else fn(r)), r["actual"]) for r in rows)
        results.append((name, low_hits, comp_hits, comp_pnl))
        L.append(f"| {name} | {low_hits}/{len(low)} | {low_hits/len(low)*100:.1f}% "
                 f"| {comp_hits}/{len(rows)} | {comp_hits/len(rows)*100:.1f}% "
                 f"| {comp_hits-base_wins:+d}场 | {comp_pnl-base_pnl:+.2f}U |")
    best = max(results[1:], key=lambda x: x[2])
    L.append("")
    L.append(f"- 结论：危险场上最强的替代判法是「{best[0]}」，全区 {best[2]}/{len(rows)}={best[2]/len(rows)*100:.1f}%"
             f"（现状 {base_wins/len(rows)*100:.1f}%）。"
             f"若所有替代判法都不显著超过现状，则说明**方向层没有免费午餐**。\n")

    # ================= 二、双弱区 =================
    L.append("## 二、最差格子：双弱区（≤1票 且 PM对我方<0.55）\n")
    dbl = [r for r in rows if r["votes"] <= 1 and (r["F"].get("pm_conf") is not None and r["F"]["pm_conf"] < 0.55)]
    dw = sum(1 for r in dbl if r["win"])
    L.append(f"- 共 {len(dbl)} 场，现状命中 {dw}/{len(dbl)}={dw/len(dbl)*100:.1f}%（≈抛硬币）；\n")
    L.append("| 处置 | 命中变化 | 模拟ΔP&L |")
    L.append("|---|---|---|")
    for name, fn in make_rules()[1:]:
        hits = sum(1 for r in dbl if fn(r) == r["actual"])
        L.append(f"| 换判法·{name} | {hits-dw:+d} | "
                 f"{sum(pnl_if(r['slug'], fn(r), r['actual']) for r in dbl)-sum(pnl_if(r['slug'], r['final'], r['actual']) for r in dbl):+.2f}U |")
    skip_pnl = -sum(pnl_if(r["slug"], r["final"], r["actual"]) for r in dbl)
    L.append(f"| **整格跳过(SKIP)** | 实现胜率剔除该格 | **{skip_pnl:+.2f}U** |")
    rest = [r for r in rows if r not in dbl]
    rw = sum(1 for r in rest if r["win"])
    L.append(f"\n- 若双弱区直接 SKIP：其余 {len(rest)} 场实现胜率 = {rw}/{len(rest)}=**{rw/len(rest)*100:.1f}%**"
             f"（现状全区 {base_wins/len(rows)*100:.1f}%），且省 {skip_pnl:+.2f}U。\n")

    # ================= 三、延迟决策检验 =================
    L.append("## 三、第二条路径：贴线区\"多等一会再决定\"值多少？\n")
    actuals = load_results_actual()
    snaps_by_slug = defaultdict(list)
    for p in sorted(SNAP_DIR.glob("*.json")):
        snaps_by_slug[p.name.split("_202")[0]].append(p)

    pairs = []
    for slug, paths in snaps_by_slug.items():
        act = actuals.get(slug)
        if not act or len(paths) < 2:
            continue
        paths = sorted(paths, key=lambda x: x.stat().st_mtime)
        try:
            first_md = json.loads(paths[0].read_text(encoding="utf-8"))
            last_md = json.loads(paths[-1].read_text(encoding="utf-8"))
        except Exception:
            continue
        rep_e, rep_l = replay_judge_full(first_md), replay_judge_full(last_md)
        if rep_e is None or rep_l is None:
            continue
        de, inp_e = rep_e[2], rep_e[2]
        dev_e = _f(inp_e.get("deviation_pct"))
        dev_l = _f(rep_l[2].get("deviation_pct"))
        if dev_e is None or dev_l is None or abs(dev_l) >= TIE_MAX:
            continue

        def quick_votes(inp):
            cf = (first_md if inp is inp_e else last_md).get("capital_flow") or {}
            tf5 = ((first_md if inp is inp_e else last_md).get("timeframes") or {}).get("5m") or {}
            kl = (first_md if inp is inp_e else last_md).get("key_levels") or {}
            cur = _f((first_md if inp is inp_e else last_md).get("current_price"))
            fin = rep_e[0].final_direction if inp is inp_e else rep_l[0].final_direction
            v = 0
            t = _f(cf.get("taker_bs_ratio"))
            if t is not None and t != 1 and (("UP" if t > 1 else "DOWN") == fin):
                v += 1
            mh = _f(tf5.get("macd_hist"))
            if mh is not None and mh != 0 and (("UP" if mh > 0 else "DOWN") == fin):
                v += 1
            lst = cf.get("long_short_ratio_trend") or []
            lv = [_f(x) for x in lst if _f(x) is not None]
            if len(lv) >= 2 and lv[-1] != lv[0]:
                if (("UP" if lv[-1] > lv[0] else "DOWN") == fin):
                    v += 1
            ws, wr = _f(kl.get("weak_support")), _f(kl.get("weak_resistance"))
            if cur and ws and wr:
                ka = (wr - cur) / cur * 100 - (cur - ws) / cur * 100
                if abs(ka) > 1e-9 and (("UP" if ka > 0 else "DOWN") == fin):
                    v += 1
            return v, fin

        ve, fe = quick_votes(inp_e)
        vl, fl = quick_votes(rep_l[2])
        try:
            te = datetime.fromisoformat(first_md["collect_time_utc"])
            tl = datetime.fromisoformat(last_md["collect_time_utc"])
            gap = (tl - te).total_seconds() / 60.0
        except Exception:
            gap = None
        pairs.append({"slug": slug, "act": act,
                      "early": (fe, ve), "late": (fl, vl),
                      "gap": gap,
                      "early_ok": fe == act, "late_ok": fl == act})

    if pairs:
        ew = sum(1 for p in pairs if p["early_ok"])
        lw2 = sum(1 for p in pairs if p["late_ok"])
        gaps = sorted(p["gap"] for p in pairs if p["gap"] is not None)
        med_gap = gaps[len(gaps) // 2] if gaps else 0
        le, he = wilson(ew, len(pairs))
        ll, hl = wilson(lw2, len(pairs))
        L.append(f"- 贴线区有 ≥2 份快照的对子 {len(pairs)} 组（首末中位间隔 ≈{med_gap:.0f} 分钟）：")
        L.append(f"  早决策命中 {ew}/{len(pairs)}={ew/len(pairs)*100:.1f}% [{le:.0f},{he:.0f}]"
                 f" → 晚决策命中 {lw2}/{len(pairs)}={lw2/len(pairs)*100:.1f}% [{ll:.0f},{hl:.0f}]")
        chg = [p for p in pairs if p["early"][0] != p["late"][0]]
        chg_right = sum(1 for p in chg if p["late_ok"])
        chg_wrong_before = sum(1 for p in chg if not p["early_ok"])
        L.append(f"- 其中方向发生改变 {len(chg)} 组：改后判对 {chg_right} 组（此前早决策在这些组判对 "
                 f"{len(chg)-chg_wrong_before} 组）→ 改对净收益 {chg_right-(len(chg)-chg_wrong_before):+d} 场")
        ve_avg = sum(p["early"][1] for p in pairs) / len(pairs)
        vl_avg = sum(p["late"][1] for p in pairs) / len(pairs)
        L.append(f"- 平均票数：早 {ve_avg:.2f} → 晚 {vl_avg:.2f}（等待让信息更清晰）")
        L.append("\n> 若\"晚决策\"显著优于\"早决策\"，对应落地形态 = 贴线区信号发出前强制二次采集复核（与待回测清单\"贴线抖动场 refresh 敏感度\"条目合流）。\n")

    # ================= 四、方案组合账本 =================
    L.append("## 四、可执行方案组合账本（全部固定$10口径）\n")
    L.append("| 方案 | 动作 | 全区命中 | 实现胜率 | 模拟P&L | 改规则? |")
    L.append("|---|---|---|---|---|---|")

    def ledger(name, action_fn, note):
        hits = 0
        pnl = 0.0
        traded = 0
        for r in rows:
            act = action_fn(r)
            if act == "SKIP":
                continue
            hits += 1 if act == r["actual"] else 0
            pnl += pnl_if(r["slug"], act, r["actual"])
            traded += 1
        rate = hits / traded * 100 if traded else 0
        L.append(f"| {name} | {note} | {hits}/{traded} | {rate:.1f}% | {pnl:+.2f}U | {'是' if 'SKIP' in note or '换' in note else '否'} |")

    ledger("S0 现状", lambda r: r["final"], "全部照旧")
    ledger("S1 投票器+双弱区SKIP", lambda r: "SKIP" if (r["votes"] <= 1 and (r["F"].get("pm_conf") is not None and r["F"]["pm_conf"] < 0.55)) else r["final"],
           "仅最差30场左右跳过")
    ledger("S2 投票器+≤1票全SKIP", lambda r: "SKIP" if r["votes"] <= 1 else r["final"], "危险场全跳")
    best_name, _lh, _ch, _cp = best
    rule_map = dict((n.split(" ")[0], f) for n, f in make_rules())
    brule = rule_map[best_name.split(" ")[0]]
    ledger(f"S3 危险场换判法({best_name.split(' ')[0]})", lambda r: (brule(r) if r["votes"] <= 1 else r["final"]), f"{best_name}")
    OUT_MD.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"危险场 {len(low)} 场 现状{lw/len(low)*100:.1f}%; 最强替代={best[0]} 合成{best[2]/len(rows)*100:.1f}%")
    print(f"双弱区 {len(dbl)} 场 现状{dw/len(dbl)*100:.1f}%; SKIP省{skip_pnl:+.2f}U, 其余实现胜率{rw/len(rest)*100:.1f}%")
    if pairs:
        print(f"多快照 {len(pairs)} 对: 早{ew/len(pairs)*100:.1f}% → 晚{lw2/len(pairs)*100:.1f}%")
    print(f"报告写入 {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
