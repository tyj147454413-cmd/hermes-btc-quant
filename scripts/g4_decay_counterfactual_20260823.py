#!/usr/bin/env python3
"""
反事实验证（8/23 3AM 翻错场复盘）：G4"偏差衰减豁免"候选规则回测。

背景：8/23 3AM 场写信号时偏差 +0.175%（仅高阈值 0.15% 一线），但 15 分钟内偏差已从
峰值 +0.304% 衰减 44%；3 分钟窗口的 deviation_trend 误判 stable，G4/价格位置照常翻 UP，
10 分钟后价格跌回锚定下方，DOWN 结胜。

候选规则 A（衰减豁免）：当 0.15%≤|偏差|<0.25% 且 偏差较近 15 分钟极值衰减 ≥1/3 时，
视为贴线处理——不做价格位置翻向，保留净分方向（锁低+0.5 由现有风控承接）。

方法：
- 取全量 |dev|≥0.15% 有结算场（与 hermes_g4_flip_reliability 同口径），
- 对每场拉 Binance 1m K 线（该小时 :00→:31）重建偏差轨迹，算 cur_dev / peak_dev(近15分) / decay；
- 模拟候选 A 改判，对比现行系统胜负变化。
只读分析；另附今天 3AM 场（未结算入 results 前）的轨迹指标。
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from replay_common import get_latest_snapshots, load_results_actual, SNAPSHOT_DIR  # noqa: E402
from g4_flip_reliability_backtest import DEV_TH  # noqa: E402

OUT_MD = SNAPSHOT_DIR.parent / "hermes_g4_decay_counterfactual_20260823.md"
BJT = timezone(timedelta(hours=8))
ANCHOR_TH_HI, ANCHOR_TH_LO, DECAY_TH = 0.25, 0.15, 1 / 3


def slug_to_utc_start(slug: str):
    m = re.search(r"([a-z]+)-(\d+)-(\d{4})-(\d+)(am|pm)-et", slug)
    if not m:
        return None
    mon, day, year, hh, ap = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)), m.group(5)
    months = {name: i + 1 for i, name in enumerate(
        ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"])}
    if mon not in months:
        return None
    h = (0 if hh == 12 else hh) if ap == "am" else (12 if hh == 12 else hh + 12)
    edt = timezone(timedelta(hours=-4))  # 8 月为夏令时 EDT=UTC-4
    dt_utc = datetime(year, months[mon], day, h, 0, tzinfo=edt).astimezone(timezone.utc)
    return int(dt_utc.timestamp() * 1000)


def fetch_klines(start_ms: int, minutes: int = 32):
    url = (f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m"
           f"&startTime={start_ms}&endTime={start_ms + minutes * 60000}&limit={minutes}")
    for _ in range(3):
        try:
            return json.loads(urllib.request.urlopen(url, timeout=15).read())
        except Exception:
            time.sleep(1.5)
    return None


def traj_metrics(closes: list[float], anchor: float, price_dir: str):
    """cur_dev(:30), extreme_dev(近15分钟最远偏离), decay(衰减比例)。"""
    win = closes[:31]
    if len(win) < 31:
        return None
    cur = win[30]
    cur_dev = (cur - anchor) / anchor * 100
    seg = win[15:31]
    ext = max(seg) if price_dir == "UP" else min(seg)
    ext_dev = (ext - anchor) / anchor * 100
    same_side = (cur_dev > 0) == (ext_dev > 0) and abs(ext_dev) > 1e-9
    decay = 1 - abs(cur_dev) / abs(ext_dev) if same_side else 0.0
    return cur_dev, ext_dev, max(0.0, decay)


def main() -> int:
    actuals = load_results_actual()
    snaps = get_latest_snapshots()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from replay_common import replay_judge_full

    rows, skipped = [], 0
    for slug, path in sorted(snaps.items()):
        act = actuals.get(slug)
        if not act:
            continue
        try:
            md = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        rep = replay_judge_full(md)
        if rep is None:
            continue
        d, _f, inp = rep
        raw_dev = inp.get("deviation_pct")
        if raw_dev is None or abs(raw_dev) < DEV_TH:
            continue
        anchor = md.get("anchor_price")
        start = slug_to_utc_start(slug)
        if not anchor or not start:
            skipped += 1
            continue
        ks = fetch_klines(start)
        if not ks:
            skipped += 1
            continue
        closes = [float(k[4]) for k in ks]
        price_dir = "UP" if raw_dev > 0 else "DOWN"
        met = traj_metrics(closes, float(anchor), price_dir)
        if not met:
            skipped += 1
            continue
        cur_dev, ext_dev, decay = met
        net = float(d.s0 or 0) + float(d.pm_row or 0)
        net_dir = "UP" if net > 0 else ("DOWN" if net < 0 else "")
        s0_dir = "UP" if float(d.s0 or 0) > 0 else ("DOWN" if float(d.s0 or 0) < 0 else "")
        rows.append({"slug": slug, "cur": cur_dev, "ext": ext_dev, "decay": decay,
                     "price_dir": price_dir, "net_dir": net_dir, "s0_dir": s0_dir,
                     "final": d.final_direction,
                     "actual": act})
    print(f"有效轨迹场: {len(rows)}（跳过 {skipped}）")

    # 现行系统方向 = 引擎 final；候选A改判 = 触发时用 net_dir（无 net 方向则维持）
    # 变体A'：触发时直接回退到 S₀ 原始方向（连 PM 行一起作废——8/23 3AM 场只有这样才能救回）
    def cand_dir(r):
        trig = ANCHOR_TH_LO <= abs(r["cur"]) < ANCHOR_TH_HI and r["decay"] >= DECAY_TH
        if trig:
            if r["s0_dir"]:
                return r["s0_dir"], True
            return r["final"], True
        return r["final"], False

    changed, cur_win, cand_win = [], 0, 0
    for r in rows:
        cd, fired = cand_dir(r)
        r["cand"] = cd
        r["fired"] = fired
        cur_win += r["final"] == r["actual"]
        cand_win += cd == r["actual"]
        if fired:
            changed.append(r)

    today = None
    # 今天 3AM 场单独演示（尚无 actual）
    tslug = "bitcoin-up-or-down-august-23-2026-3am-et"
    tp = snaps.get(tslug)
    if tp:
        tmd = json.loads(tp.read_text(encoding="utf-8"))
        st = slug_to_utc_start(tslug)
        ks = fetch_klines(st) if st else None
        if ks and tmd.get("anchor_price"):
            met = traj_metrics([float(k[4]) for k in ks], float(tmd["anchor_price"]), "UP")
            if met:
                today = {"slug": tslug, "cur": met[0], "ext": met[1], "decay": met[2]}

    L = ["# G4 偏差衰减豁免·反事实回测（8/23 3AM 翻错场复盘配套）\n",
         f"- 有效轨迹场 {len(rows)}；现行系统命中 {cur_win}/{len(rows)}={cur_win/len(rows)*100:.1f}%；",
         f"- 候选A（0.15≤|dev|<0.25 且 近15分衰减≥1/3 → 不翻向留净分）触发 {len(changed)} 场，改判后命中 {cand_win}/{len(rows)}={cand_win/len(rows)*100:.1f}%\n",
         "| slug | :30偏差% | 近15分极值% | 衰减 | 实际 | 现行 | 候选A | 现行对错变化 |",
         "|---|---|---|---|---|---|---|---|"]
    for r in sorted(changed, key=lambda x: x["slug"]):
        was = "✅" if r["final"] == r["actual"] else "❌"
        now = "✅" if r["cand"] == r["actual"] else "❌"
        L.append(f"| {r['slug'].replace('bitcoin-up-or-down-','')} | {r['cur']:+.2f} | {r['ext']:+.2f} | {r['decay']*100:.0f}% | {r['actual']} | {r['final']} {was} | {r['cand']} {now} | {'救回' if now=='✅' and was=='❌' else ('误伤' if now=='❌' and was=='✅' else '不变')} |")
    if today:
        L.append(f"\n今天 3AM 场指标（未入样本）：:30偏差 {today['cur']:+.3f}%，近15分极值 {today['ext']:+.3f}%，衰减 {today['decay']*100:.0f}% → 候选A{'会触发' if ANCHOR_TH_LO<=abs(today['cur'])<ANCHOR_TH_HI and today['decay']>=DECAY_TH else '不触发'}")
    OUT_MD.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"报告写入 {OUT_MD}")
    print(f"现行 {cur_win}/{len(rows)} ({cur_win/len(rows)*100:.1f}%) → 候选A {cand_win}/{len(rows)} ({cand_win/len(rows)*100:.1f}%)；触发 {len(changed)} 场")
    for r in changed:
        was = "✅" if r["final"] == r["actual"] else "❌"
        now = "✅" if r["cand"] == r["actual"] else "❌"
        tag = "救回" if now == "✅" and was == "❌" else ("误伤" if now == "❌" and was == "✅" else "不变")
        print(f"  {r['slug']}: dev{r['cur']:+.2f}% 衰减{r['decay']*100:.0f}% 实际{r['actual']} 现{r['final']}{was}→候{r['cand']}{now} [{tag}]")
    if today:
        print(f"今天3AM: 偏差{today['cur']:+.3f}% 极值{today['ext']:+.3f}% 衰减{today['decay']*100:.0f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
