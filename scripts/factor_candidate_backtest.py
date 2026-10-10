#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""候选因子影子回测（round41.8.9 后续）：先采数据 + 回测方向预测力，不进评分。

对第一档候选因子（资金费率/OI变化/多空比/恐惧贪婪/PM价格动量）计算方向信号，
比对 hermes_results 实际结算方向，输出各因子的命中率，供决定「哪些进评分」。
只读不改评分——本脚本不写 factors、不影响 S0。

运行：python3 scripts/factor_candidate_backtest.py
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA = ROOT / "data"
RESULTS_FILE = DATA / "hermes_results.jsonl"
SNAPSHOT_DIR = DATA / "snapshots"


def sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def slug_of_snapshot(filename: str) -> str:
    return re.sub(r"_\d{8}_\d{6}\.json$", "", filename)


def load_results() -> dict[str, dict]:
    results: dict[str, dict] = {}
    for line in RESULTS_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("actual") in ("UP", "DOWN") and r.get("pnl") is not None:
            results[r["slug"]] = r
    return results


def load_snapshots() -> dict[str, Path]:
    m: dict[str, Path] = {}
    for p in sorted(SNAPSHOT_DIR.glob("*.json")):
        m[slug_of_snapshot(p.name)] = p
    return m


def hit(signal: float, actual: str) -> str:
    if signal is None or signal == 0:
        return "neutral"
    predicts_up = signal > 0
    if (predicts_up and actual == "UP") or (not predicts_up and actual == "DOWN"):
        return "correct"
    return "wrong"


def main() -> int:
    results = load_results()
    snaps = load_snapshots()

    # 候选因子统计：name → {correct, wrong, neutral}
    stats: dict[str, dict[str, int]] = defaultdict(lambda: {"correct": 0, "wrong": 0, "neutral": 0})
    joined = 0

    for slug, r in results.items():
        actual = r["actual"]
        if slug not in snaps:
            continue
        try:
            md = json.loads(snaps[slug].read_text())
        except Exception:
            continue
        joined += 1

        cf = md.get("capital_flow") or {}
        senti = md.get("sentiment") or {}
        pm = md.get("polymarket") or {}
        dev = md.get("deviation")  # (current-anchor)/anchor*100，1 小时内价格变化%

        # ---- 候选因子方向信号 ----

        # 1) funding_rate（拥挤反向）：正=多头拥挤→偏空；负=空头拥挤→偏多；0=中性
        fr = cf.get("funding_rate")
        sig_fr = -1 if (fr is not None and fr > 0) else (1 if (fr is not None and fr < 0) else 0)

        # 2) oi_change_24h_pct（OI 变化）：OI↑→新仓进场（跟随价方向）；OI↓→平仓（中性）
        oi = cf.get("oi_change_24h_pct")
        sig_oi_up = 1 if (oi is not None and oi > 0) else (0 if oi is not None else 0)  # OI 单纯增加→偏多？
        # 组合：OI↑ 且 价↑ → 多头建仓(+1)；OI↑ 且 价↓ → 空头建仓(-1)；OI↓ → 0
        if oi is not None and dev is not None and oi > 0:
            sig_oi_combo = sign(dev)
        else:
            sig_oi_combo = 0

        # 3) long_short_ratio（散户拥挤反向）：>1.5 散户追多→偏空；<1.2 散户追空→偏多
        lsr = cf.get("long_short_ratio")
        if lsr is not None:
            sig_lsr = -1 if lsr > 1.5 else (1 if lsr < 1.2 else 0)
        else:
            sig_lsr = 0

        # 4) fear_greed_index（过热反向）：>60 贪婪→偏空；<30 恐惧→偏多
        fgi = senti.get("fear_greed_index")
        if fgi is not None:
            sig_fgi = -1 if fgi > 60 else (1 if fgi < 30 else 0)
        else:
            sig_fgi = 0

        # 5) PM 价格动量（聪明钱流向）：PM up_price 相对 0.5 的偏离方向
        #    （up>0.5 = 市场押多；这是绝对价，动量的变化率在 PM_momentum 单独算）
        pm_up = pm.get("up_price")
        sig_pm = sign(pm_up - 0.5) if pm_up is not None else 0  # 顺 PM（对照基线）

        # 6) PM 挂单失衡（round41.8.11 影子采集，聪明钱挂单信号）：
        #    book_up_mid = (best_bid+best_ask)/2，>0.5 订单簿看多，<0.5 看空
        #    仅 round41.8.11 之后的新快照有该字段；攒 ≥2 周后才够样本。
        book_mid = pm.get("book_up_mid")
        sig_book_mid = sign(book_mid - 0.5) if book_mid is not None else 0
        # 偏离信号：book_up_mid − up_price（订单簿 vs 标记价的偏离方向，聪明钱领先散户）
        if book_mid is not None and pm_up is not None:
            sig_book_dev = sign(book_mid - pm_up)
        else:
            sig_book_dev = 0

        candidates = {
            "funding_rate(拥挤反向)": sig_fr,
            "OI变化(单纯增→多)": sig_oi_up,
            "OI-价格组合(建仓方向)": sig_oi_combo,
            "多空比(散户拥挤反向)": sig_lsr,
            "恐惧贪婪(过热反向)": sig_fgi,
            "PM顺共识(对照基线)": sig_pm,
            "PM挂单book_mid方向(影子)": sig_book_mid,
            "PM挂单偏离book_mid-up_price(影子)": sig_book_dev,
        }
        for name, sig in candidates.items():
            h = hit(sig, actual)
            if h == "correct":
                stats[name]["correct"] += 1
            elif h == "wrong":
                stats[name]["wrong"] += 1
            else:
                stats[name]["neutral"] += 1

    print(f"=== 候选因子影子回测（join {joined}/{len(results)}）===\n")
    print(f"{'候选因子':<26} {'命中':>8} {'触发':>6} {'命中率':>8}")
    print("-" * 52)
    for name in sorted(stats, key=lambda k: -(stats[k]['correct'] + stats[k]['wrong'])):
        st = stats[name]
        n = st["correct"] + st["wrong"]
        rate = st["correct"] / n * 100 if n else 0
        trig = n + st["neutral"]
        flag = "" if rate >= 55 else (" ⚠️" if rate < 45 else "")
        print(f"{name:<26} {st['correct']:>4}/{n:<5} {trig:>6} {rate:>7.1f}%{flag}")
    print("\n（命中率 >55% = 有正向预测力，<45% = 反向，45-55% = 无信号）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
