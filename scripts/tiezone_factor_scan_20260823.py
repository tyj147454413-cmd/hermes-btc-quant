#!/usr/bin/env python3
"""
贴线区专项研究：非位置因子信息量扫描（8/23 新课题配套）。

背景（data/hermes_g4_sweep_20260823.md §五·§六.3）：
- 263 场结算样本中 72 场输单，其中 57 场（79%）发生在 |dev|<0.15% 贴线区；
- 该区价格位置无信息量（31-32% 输率平坦），课题 = 找非位置类因子的信息量。

口径（与 G4 扫描完全一致）：
- 样本 = 全部可重放且有结算 actual 的场次 ∩ |dev|<0.15%；
- 方向 = 引擎完整管线两遍复算 final_direction；胜负 = final == actual；
- 快照 = 每 slug 取 mtime 最新一份。

两类分析：
- 分析A（独立方向性）：因子自身指向 vs 实际命中率。显著 >50% 才有"翻向"候选价值；
- 分析B（信任度调制）：因子顺/逆引擎最终方向分组胜率差。
  · 顺向组胜率高 → 加仓/升置信调制器；
  · 逆向组内比较"跟引擎 vs 跟因子"谁准 → 翻向候选；否则只配缩仓。

统计纪律：
- 双比例 z 检验与 Fisher 精确检验取较大者（保守）；Wilson95% 区间；
- BH-FDR(q≤0.10) 家族内多重校正；
- 按采集时间对半切样本，效应符号前后一致性检查（防过拟合）。

反事实（仅贴线区内）：
- 缩仓×0.5 / SKIP：用真实 pnl 直接换算；
- 翻向：用 buy_price 重算输赢金额（缺价用贴线区中位价替代并注明）。

只读分析，不写信号、不改规则。
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from replay_common import (  # noqa: E402
    ROOT,
    get_latest_snapshots,
    load_results_actual,
    load_results_full,
    replay_judge_full,
    wilson,
)

OUT_MD = ROOT / "data" / "hermes_tiezone_factor_scan_20260823.md"
TIE_MAX = 0.15  # 贴线区上限 %（G4 报告 §五口径）

# ====================================================================
# 统计工具
# ====================================================================


def _norm_sf(x: float) -> float:
    return 0.5 * math.erfc(x / math.sqrt(2))


def two_prop_p(w1: int, n1: int, w2: int, n2: int) -> float:
    """双比例合并 z 检验（双侧）。任一组为空返回 1。"""
    if n1 == 0 or n2 == 0:
        return 1.0
    se_sq = ((w1 + w2) / (n1 + n2)) * (1 - (w1 + w2) / (n1 + n2)) * (1 / n1 + 1 / n2)
    if se_sq <= 0:
        return 1.0 if w1 / n1 == w2 / n2 else 0.0
    z = (w1 / n1 - w2 / n2) / math.sqrt(se_sq)
    return min(1.0, 2 * _norm_sf(abs(z)))


def fisher_p(w1: int, n1: int, w2: int, n2: int) -> float:
    """Fisher 精确双侧（2x2 超几何）。"""
    a, b, c, d = w1, n1 - w1, w2, n2 - w2
    n, r1, c1 = a + b + c + d, a + b, a + c
    if n == 0 or min(a, b, c, d) < 0:
        return 1.0
    r2, c2 = n - r1, n - c1

    def log_hyp(k: int) -> float:
        return (
            math.lgamma(r1 + 1) - math.lgamma(k + 1) - math.lgamma(r1 - k + 1)
            + math.lgamma(r2 + 1) - math.lgamma(c1 - k + 1) - math.lgamma(r2 - c1 + k + 1)
            - (math.lgamma(n + 1) - math.lgamma(c1 + 1) - math.lgamma(n - c1 + 1))
        )

    lo, hi = max(0, c1 - r2), min(r1, c1)
    probs = {k: math.exp(log_hyp(k)) for k in range(lo, hi + 1)}
    tot = sum(probs.values()) or 1.0
    p_obs = probs.get(a, 0) / tot
    return min(1.0, sum(v for v in probs.values() if v <= p_obs * (1 + 1e-9)) / tot)


def conservative_p(w1: int, n1: int, w2: int, n2: int) -> float:
    return max(two_prop_p(w1, n1, w2, n2), fisher_p(w1, n1, w2, n2))


def bh_qvalues(pvals: list[float]) -> list[float]:
    """Benjamini-Hochberg FDR 校正。"""
    m = len(pvals)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvals[i])
    q = [1.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        idx = order[rank - 1]
        running = min(running, pvals[idx] * m / rank)
        q[idx] = min(1.0, running)
    return q


# ====================================================================
# 特征抽取
# ====================================================================


def _f(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _sign_dir(x):
    if x is None or abs(x) < 1e-12:
        return None
    return "UP" if x > 0 else "DOWN"


def _tf_feats(tf: dict, tag: str, out: dict) -> None:
    out[f"rd_{tag}"] = tf.get("recent_dir") if tf.get("recent_dir") in ("UP", "DOWN") else None
    out[f"mh_{tag}"] = _sign_dir(_f(tf.get("macd_hist")))
    out[f"ma_{tag}"] = _sign_dir(_f(tf.get("macd_acceleration")))
    out[f"rsi_{tag}"] = _f(tf.get("rsi"))
    out[f"bb_{tag}"] = _f(tf.get("bb_pos"))
    out[f"vr_{tag}"] = _f(tf.get("vol_ratio"))
    cu, cd = int(tf.get("consecutive_up") or 0), int(tf.get("consecutive_down") or 0)
    out[f"consec_{tag}"] = cu - cd


def build_rows():
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
        d, _factors, inp = rep
        dev = _f(inp.get("deviation_pct"))
        if dev is None or abs(dev) < 1e-9 or abs(dev) >= TIE_MAX:
            continue
        final = d.final_direction
        if final not in ("UP", "DOWN"):
            continue

        row = {
            "slug": slug,
            "collect": inp.get("_collect_dt"),
            "dev": dev,
            "abs_dev": abs(dev),
            "final": final,
            "actual": act,
            "win": final == act,
            "s0": _f(d.s0),
            "net": (_f(d.s0) or 0) + (_f(d.pm_row) or 0),
            "regime": inp.get("regime") or "?",
            "core": abs(dev) < 0.05,
        }

        cf = md.get("capital_flow") or {}
        ob = cf.get("order_book") or {}
        ofl = cf.get("order_flow") or {}
        dt = md.get("deviation_trend") or {}
        sent = md.get("sentiment") or {}
        hist = md.get("historical") or {}
        pm = md.get("polymarket") or {}
        kl = md.get("key_levels") or {}
        opening = md.get("opening") or {}
        prev = md.get("prev_hour") or {}
        tick = md.get("ticker_24h") or {}
        pos24 = md.get("price_position_24h") or {}
        vr15 = md.get("v_reversal_15m") or {}
        oip = md.get("oi_price_relation") or {}

        f = row["F"] = {}
        # ---- 资金流族 ----
        f["taker_now"] = _f(cf.get("taker_bs_ratio"))
        f["taker_1h"] = _f(cf.get("taker_bs_avg_1h"))
        trend = cf.get("taker_bs_trend") or []
        vals = [_f(x) for x in trend[-3:] if _f(x) is not None]
        f["taker_r3"] = sum(vals) / len(vals) if vals else None
        f["oi_chg"] = _f(cf.get("oi_change_24h_pct"))
        f["ls_ratio"] = _f(cf.get("long_short_ratio"))
        lst = cf.get("long_short_ratio_trend") or []
        lv = [_f(x) for x in lst if _f(x) is not None]
        f["ls_slope"] = lv[-1] - lv[0] if len(lv) >= 2 else None
        bv, av = _f(ob.get("bid_volume_top10")), _f(ob.get("ask_volume_top10"))
        f["ob_imb"] = (bv - av) / (bv + av) if (bv is not None and av is not None and bv + av > 0) else None
        f["flow_5m"] = _f(ofl.get("imbalance_5m"))
        f["flow_15m"] = _f(ofl.get("imbalance_15m"))
        f["of_partial"] = bool(ofl.get("partial"))
        f["fg"] = _f(sent.get("fear_greed_index"))

        # ---- 偏差动态族 ----
        l3 = [_f(x) for x in (dt.get("last_3min_dev_pct") or []) if _f(x) is not None]
        f["dev_slope"] = l3[-1] - l3[0] if len(l3) >= 2 else None
        f["dev_flag"] = "expanding" if dt.get("expanding") else ("shrinking" if dt.get("shrinking") else ("stable" if dt.get("stable") else None))

        # ---- 微观动量族 ----
        for tag in ("1m", "5m", "15m"):
            _tf_feats(md.get("timeframes", {}).get(tag) or {}, tag, f)

        # ---- 波动/时间族 ----
        f["vol"] = _f(md.get("volatility_5m"))
        try:
            ts = datetime.fromisoformat(md["timestamp"])
            ct = datetime.fromisoformat(md["collect_time_utc"])
            row["collect"] = ct
            # 距结算剩余分钟 = 60 - 快照距本小时锚定的已过分钟
            f["mins"] = 60.0 + (ts - ct).total_seconds() / 60.0
        except Exception:
            row.setdefault("collect", None)
            f["mins"] = None

        # ---- PM 共识族 ----
        f["pm_up"] = _f(pm.get("up_price"))
        f["pm_vol"] = _f(pm.get("volume"))
        if f["pm_up"] is not None:
            f["pm_agrees"] = (f["pm_up"] >= 0.5) == (final == "UP")
            f["pm_conf"] = f["pm_up"] if final == "UP" else 1 - f["pm_up"]
            f["pm_absdiff"] = abs(f["pm_up"] - 0.5)

        # ---- 环境/季节族 ----
        f["hist_up"] = _f(hist.get("up_rate"))
        f["hist_n"] = _f(hist.get("total_samples"))
        f["chg24"] = _f(tick.get("change_pct"))
        f["pos24"] = _f(pos24.get("current_percentile"))

        # ---- 结构族 ----
        cur = _f(md.get("current_price"))
        ws, wr = _f(kl.get("weak_support")), _f(kl.get("weak_resistance"))
        if cur and ws and wr and cur > 0:
            f["dist_sup"] = (cur - ws) / cur * 100
            f["dist_res"] = (wr - cur) / cur * 100
            f["kl_asym"] = f["dist_res"] - f["dist_sup"]
        f["open_dir"] = opening.get("dir") if opening.get("dir") in ("UP", "DOWN") else None
        f["open_range"] = _f(opening.get("range_pct"))
        f["open_vr"] = _f(opening.get("vol_ratio"))
        f["fakeout"] = bool(opening.get("has_fakeout"))
        f["vreversal"] = bool(opening.get("has_v_reversal")) or bool(vr15.get("detected"))
        f["prev_dir_hit"] = prev.get("side") if prev.get("side") in ("UP", "DOWN") else None
        f["prev_chg"] = _f(prev.get("change_pct"))
        f["prev_last3"] = _f(prev.get("last3_chg_pct"))
        f["prev_flips"] = _f(prev.get("flips"))
        mt = md.get("macro_trend_1h_4h") or {}
        f["macro_net"] = (int(mt.get("up_count") or 0)) - (int(mt.get("down_count") or 0))
        f["oip_signal"] = oip.get("signal")
        rows.append(row)
    return rows


# ====================================================================
# 有向因子注册表：(键, 展示名, 族, 方向函数 F->'UP'/'DOWN'/None)
# ====================================================================

RATIO_UP = lambda v, th=1.0: ("UP" if v > th else "DOWN") if v is not None and v != th else None  # noqa: E731
SIGN_UP = lambda v: _sign_dir(v)  # noqa: E731

SIGNED_FACTORS = [
    ("taker_now", "Taker买卖比(即时)", "资金流", lambda F: RATIO_UP(F["taker_now"])),
    ("taker_1h", "Taker买卖比(1h均)", "资金流", lambda F: RATIO_UP(F["taker_1h"])),
    ("taker_r3", "Taker近3段均值", "资金流", lambda F: RATIO_UP(F["taker_r3"])),
    ("ob_imb", "订单簿Top10失衡", "资金流", lambda F: SIGN_UP(F["ob_imb"])),
    ("flow_5m", "订单流失衡5m", "资金流", lambda F: SIGN_UP(F["flow_5m"])),
    ("flow_15m", "订单流失衡15m", "资金流", lambda F: SIGN_UP(F["flow_15m"])),
    ("oi_chg", "OI变化24h", "资金流", lambda F: SIGN_UP(F["oi_chg"])),
    ("ls_ratio", "多空人数比", "资金流", lambda F: RATIO_UP(F["ls_ratio"])),
    ("ls_slope", "多空比斜率", "资金流", lambda F: SIGN_UP(F["ls_slope"])),
    ("rd_1m", "recent_dir 1m", "微观动量", lambda F: F["rd_1m"]),
    ("rd_5m", "recent_dir 5m", "微观动量", lambda F: F["rd_5m"]),
    ("rd_15m", "recent_dir 15m", "微观动量", lambda F: F["rd_15m"]),
    ("mh_1m", "MACD hist 1m", "微观动量", lambda F: F["mh_1m"]),
    ("mh_5m", "MACD hist 5m", "微观动量", lambda F: F["mh_5m"]),
    ("mh_15m", "MACD hist 15m", "微观动量", lambda F: F["mh_15m"]),
    ("ma_1m", "MACD加速 1m", "微观动量", lambda F: F["ma_1m"]),
    ("ma_5m", "MACD加速 5m", "微观动量", lambda F: F["ma_5m"]),
    ("consec_1m", "连阳阴净额 1m", "微观动量", lambda F: SIGN_UP(F["consec_1m"]) if F["consec_1m"] else None),
    ("consec_5m", "连阳阴净额 5m", "微观动量", lambda F: SIGN_UP(F["consec_5m"]) if F["consec_5m"] else None),
    ("consec_15m", "连阳阴净额 15m", "微观动量", lambda F: SIGN_UP(F["consec_15m"]) if F["consec_15m"] else None),
    ("dev_slope", "偏差3分钟斜率", "偏差动态", lambda F: SIGN_UP(F["dev_slope"])),
    ("hist_up", "历史同期涨率", "环境季节", lambda F: RATIO_UP(F["hist_up"], 0.5) if F["hist_up"] not in (None, 0.5) else None),
    ("chg24", "24h涨跌幅", "环境季节", lambda F: SIGN_UP(F["chg24"])),
    ("kl_asym", "支阻不对称(负=近支撑)", "结构", lambda F: SIGN_UP(F["kl_asym"]) if F["kl_asym"] is not None and abs(F["kl_asym"]) > 1e-9 else None),
    ("open_dir", "开盘5m方向", "结构", lambda F: F["open_dir"]),
    ("prev_dir_hit", "上一小时方向", "结构", lambda F: F["prev_dir_hit"]),
    ("prev_chg", "上一小时涨跌", "结构", lambda F: SIGN_UP(F["prev_chg"]) if F["prev_chg"] else None),
    ("prev_last3", "上小时末3根动量", "结构", lambda F: SIGN_UP(F["prev_last3"]) if F["prev_last3"] else None),
    ("macro_net", "宏观1H4H净额", "结构", lambda F: SIGN_UP(F["macro_net"]) if F["macro_net"] else None),
]

LEVEL_BUCKETS = [
    ("vol", "波动率5m档", "波动时间", [(0, 0.10, "≤0.10"), (0.10, 0.20, "0.10-0.20"), (0.20, 0.30, "0.20-0.30"), (0.30, 99, ">0.30")]),
    ("mins", "距结算分钟档", "波动时间", [(0, 15, "≤15"), (15, 25, "15-25"), (25, 99, ">25")]),
    ("pm_conf", "PM对我们方向的置信", "PM共识", [(0, 0.55, "<0.55"), (0.55, 0.65, "0.55-0.65"), (0.65, 1.01, "≥0.65")]),
    ("fg", "恐贪指数档", "环境季节", [(0, 35, "极度恐惧<35"), (35, 65, "中性35-65"), (65, 101, "贪婪≥65")]),
    ("pos24", "24h价格位置百分位", "环境季节", [(0, 30, "<30%"), (30, 70, "30-70%"), (70, 101, "≥70%")]),
    ("rsi_15m", "RSI 15m 档", "微观动量", [(0, 40, "<40"), (40, 60, "40-60"), (60, 101, "≥60")]),
    ("vr_5m", "量比 5m 档", "微观动量", [(0, 0.7, "<0.7 缩量"), (0.7, 1.5, "0.7-1.5 常量"), (1.5, 99, "≥1.5 放量")]),
    ("open_range", "开盘5m振幅档", "结构", [(0, 0.08, "<0.08%"), (0.08, 0.20, "0.08-0.20%"), (0.20, 99, "≥0.20%")]),
]


def main() -> int:
    rows = build_rows()
    n_all = len(rows)
    wins = sum(1 for r in rows if r["win"])
    core = [r for r in rows if r["core"]]
    ext = [r for r in rows if not r["core"]]
    lo_w, hi_w = wilson(wins, n_all)

    L = [
        "# 贴线区专项研究：非位置因子信息量扫描（8/23）\n",
        f"- 样本：|dev|<0.15% 贴线区共 **{n_all} 场**（引擎完整管线复算口径，与 G4 扫描同源）；",
        f"- 区内基线：命中 {wins}/{n_all} = **{wins/n_all*100:.1f}%**，Wilson95% [{lo_w:.1f}, {hi_w:.1f}]；"
        f"输单 {n_all - wins} 场（占全库 72 输的比例见 G4 报告 §五）；",
        f"- 子桶：核心区(<0.05%) {len(core)} 场胜率 {(sum(1 for r in core if r['win'])/len(core)*100 if core else 0):.1f}%"
        f"｜扩展区(0.05-0.15%) {len(ext)} 场胜率 {(sum(1 for r in ext if r['win'])/len(ext)*100 if ext else 0):.1f}%；",
        "- 方法：分析A=因子独立方向命中率（翻向候选）；分析B=顺/逆引擎方向分组（调制器候选）；",
        "- 检验：z 与 Fisher 取大者（保守）+ BH-FDR 家族内校正 + 时间对半切稳定性；",
        "- ⚠️ 选择效应警示：贴线区由\"信号时点 |dev| 小\"定义，任何结论都以此为条件分布。\n",
    ]

    # ================= 分析A =================
    A = []
    for key, label, fam, fn in SIGNED_FACTORS:
        have = [(r, fn(r["F"])) for r in rows]
        have = [(r, drc) for r, drc in have if drc in ("UP", "DOWN")]
        nn = len(have)
        if nn < 10:
            continue
        hit = sum(1 for r, drc in have if drc == r["actual"])
        # 对 50% 的检验（正态近似与精确二项取较大者，保守）
        p_norm = 2 * _norm_sf(abs((hit / nn - 0.5) / math.sqrt(0.25 / nn)))
        # 精确二项（两侧）
        k = hit
        pv = [math.comb(nn, i) / 2**nn for i in range(0, nn + 1)]
        p_obs = pv[k]
        p_exact = min(1.0, sum(v for v in pv if v <= p_obs * (1 + 1e-9)))
        p50 = max(p_norm, p_exact)
        bl, bh = wilson(hit, nn)
        A.append({"key": key, "label": label, "fam": fam, "n": nn, "hit": hit,
                  "rate": hit / nn, "ci": (bl, bh), "p50": p50})

    pa = [a["p50"] for a in A]
    qa = bh_qvalues(pa)
    for a, q in zip(A, qa):
        a["q50"] = q
    A.sort(key=lambda a: a["p50"])

    L.append("## 一、分析A：因子独立方向命中率（该因子若单独当方向预测器）\n")
    L.append("| 因子 | 族 | n(非空) | 命中实际 | 命中率 | Wilson95% | p(vs50%) | q(FDR) |")
    L.append("|---|---|---|---|---|---|---|---|")
    for a in A:
        star = " ✅" if a["q50"] <= 0.10 and a["rate"] > 0.5 else ""
        L.append(
            f"| {a['label']} | {a['fam']} | {a['n']} | {a['hit']} | {a['rate']*100:.1f}%{star} "
            f"| [{a['ci'][0]:.0f},{a['ci'][1]:.0f}] | {a['p50']:.3f} | {a['q50']:.3f} |")
    L.append("")
    sig_a = [a for a in A if a["q50"] <= 0.10]
    L.append(f"- FDR q≤0.10 的因子：{', '.join(a['label'] for a in sig_a) if sig_a else '无'}\n")

    # ================= 分析B =================
    B = []
    for key, label, fam, fn in SIGNED_FACTORS:
        agg_n = agg_w = dis_n = dis_w = dis_fol = 0
        for r in rows:
            drc = fn(r["F"])
            if drc not in ("UP", "DOWN"):
                continue
            if drc == r["final"]:
                agg_n += 1
                agg_w += 1 if r["win"] else 0
            else:
                dis_n += 1
                dis_w += 1 if r["win"] else 0
                dis_fol += 1 if drc == r["actual"] else 0
        if agg_n < 8 or dis_n < 8:
            continue
        p = conservative_p(agg_w, agg_n, dis_w, dis_n)
        dl, dh = wilson(dis_w, dis_n)
        al, ah = wilson(agg_w, agg_n)
        B.append({"key": key, "label": label, "fam": fam,
                  "agg_n": agg_n, "agg_w": agg_w, "dis_n": dis_n, "dis_w": dis_w,
                  "agg_rate": agg_w / agg_n, "dis_rate": dis_w / dis_n,
                  "fol_rate": dis_fol / dis_n if dis_n else 0,
                  "delta": agg_w / agg_n - dis_w / dis_n, "p": p,
                  "ci_a": (al, ah), "ci_d": (dl, dh)})

    pb = [b["p"] for b in B]
    qb = bh_qvalues(pb)
    for b, q in zip(B, qb):
        b["q"] = q
    B.sort(key=lambda b: b["p"])

    L.append("## 二、分析B：因子顺/逆引擎方向的分组胜率（调制器候选）\n")
    L.append("| 因子 | 族 | 顺向组 | 顺组胜率 | 逆向组 | 逆组胜率(跟引擎) | 逆组若跟因子 | 胜率差 | p | q(FDR) |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for b in B:
        star = " ✅" if b["q"] <= 0.10 else ""
        L.append(
            f"| {b['label']} | {b['fam']} | {b['agg_w']}/{b['agg_n']} | {b['agg_rate']*100:.1f}% "
            f"| {b['dis_n']} | {b['dis_rate']*100:.1f}% | {b['fol_rate']*100:.1f}% "
            f"| {b['delta']*100:+.1f}pp | {b['p']:.3f} | {b['q']:.3f}{star} |")
    L.append("")
    sig_b = [b for b in B if b["q"] <= 0.10]
    L.append(f"- FDR q≤0.10：{', '.join(b['label'] for b in sig_b) if sig_b else '无'}\n")

    # ================= 分档水平因子 =================
    L.append("## 三、水平型因子分档胜率（无方向、只有强弱档位）\n")
    L.append("| 因子 | 档 | n | 胜率 | Wilson95% |")
    L.append("|---|---|---|---|---|")
    for key, label, fam, edges in LEVEL_BUCKETS:
        cells = []
        for lo, hi, name in edges:
            sub = [r for r in rows if r["F"].get(key) is not None and lo <= r["F"][key] < hi]
            if not sub:
                continue
            w = sum(1 for r in sub if r["win"])
            bl, bh = wilson(w, len(sub))
            cells.append((name, len(sub), w, bl, bh))
        if len(cells) >= 2:
            for name, nn, w, bl, bh in cells:
                L.append(f"| {label} | {name} | {nn} | {w/nn*100:.1f}% | [{bl:.0f},{bh:.0f}] |")
            # 首尾档对比 p
            (n1c, w1c), (n2c, w2c) = (cells[0][1], cells[0][2]), (cells[-1][1], cells[-1][2])
            pp = conservative_p(w1c, n1c, w2c, n2c)
            L.append(f"| {label} | **首尾档差异 p** | — | — | {pp:.3f} |")
    L.append("")

    # 分类因子三桶
    for cat_key, cat_label, cat_get in [
        ("dev_flag", "偏差动态(3min)", lambda F: F.get("dev_flag")),
        ("pm_agrees", "PM共识是否顺引擎", lambda F: "顺" if F.get("pm_agrees") else ("逆" if F.get("pm_agrees") is not None else None)),
        ("fakeout", "开盘假突破存在", lambda F: "有" if F.get("fakeout") else "无"),
        ("vreversal", "V反信号存在", lambda F: "有" if F.get("vreversal") else "无"),
        ("oip_signal", "OI-价关系信号", lambda F: F.get("oip_signal")),
        ("of_partial", "订单流窗口截断(C2)", lambda F: "partial" if F.get("of_partial") else "full"),
    ]:
        groups = defaultdict(lambda: [0, 0])
        for r in rows:
            g = r["regime"] if cat_key == "regime" else cat_get(r["F"])
            if g is None:
                continue
            groups[str(g)][0] += 1
            groups[str(g)][1] += 1 if r["win"] else 0
        if len(groups) >= 2 and sum(v[0] for v in groups.values()) >= 20:
            parts = []
            for g, (nn, w) in sorted(groups.items()):
                bl, bh = wilson(w, nn)
                parts.append(f"{g}: {w}/{nn}={w/nn*100:.1f}%[{bl:.0f},{bh:.0f}]")
            L.append(f"- **{cat_label}**：{'；'.join(parts)}")
    L.append("")

    # ================= 时间稳定性 =================
    L.append("## 四、时间对半切稳定性（显著项复核）\n")
    rows_sorted = sorted(rows, key=lambda r: (r["collect"] is None, r["collect"]))
    half = len(rows_sorted) // 2
    h1, h2 = rows_sorted[:half], rows_sorted[half:]

    def split_check(key_label, fam, fn):
        res = []
        for hh, name in ((h1, "前半"), (h2, "后半")):
            an = aw = dn = dw = 0
            for r in hh:
                drc = fn(r["F"])
                if drc not in ("UP", "DOWN"):
                    continue
                if drc == r["final"]:
                    an += 1
                    aw += 1 if r["win"] else 0
                else:
                    dn += 1
                    dw += 1 if r["win"] else 0
            res.append((name, an, aw, dn, dw))
        return res

    checks = []
    for b in (sig_b or sorted(B, key=lambda x: x["p"])[:5]):
        fn = dict((k, f) for k, _l, _fm, f in SIGNED_FACTORS)[b["key"]]
        res = split_check(b["key"], b["fam"], fn)
        deltas = []
        detail = []
        for name, an, aw, dn, dw in res:
            if an and dn:
                dd = aw / an - dw / dn
                deltas.append(dd)
                detail.append(f"{name} 差 {dd*100:+.1f}pp (顺{aw}/{an}/逆{dw}/{dn})")
        if len(deltas) == 2 and deltas[0] * deltas[1] > 0:
            verdict = "✅ 符号一致"
        elif len(deltas) == 2:
            verdict = "❌ 符号反转"
        else:
            verdict = "⚠️ 样本不足"
        checks.append((b["label"], "; ".join(detail), verdict))

    if checks:
        L.append("| 因子 | 前半效应 | 后半效应 | 判定 |")
        L.append("|---|---|---|---|")
        for lb, det, vd in checks:
            L.append(f"| {lb} | {det} | — | {vd} |")
    L.append("")

    # ================= 反事实 =================
    L.append("## 五、反事实模拟（仅贴线区内，真实 pnl 口径）\n")
    full = load_results_full()

    def pnl_of(r):
        rec = full.get(r["slug"])
        if not rec:
            return None
        pnl = rec.get("pnl")
        return None if pnl is None else float(pnl)

    pnl_rows = [r for r in rows if pnl_of(r) is not None]
    base_pnl = sum(pnl_of(r) for r in pnl_rows)

    def mod_counterfactual(fn, mode):
        delta = 0.0
        cnt = 0
        for r in pnl_rows:
            drc = fn(r["F"])
            if drc in ("UP", "DOWN") and drc != r["final"]:
                p = pnl_of(r)
                delta -= p * (0.5 if mode == "half" else 1.0)
                cnt += 1
        return delta, cnt

    prices = [float(full[r["slug"]]["buy_price"]) for r in rows
              if full.get(r["slug"]) and full[r["slug"]].get("buy_price")]
    med_price = sorted(prices)[len(prices) // 2] if prices else 0.55

    def flip_counterfactual(fn):
        dwin = 0
        dpnl = 0.0
        cnt = 0
        for r in rows:
            drc = fn(r["F"])
            if drc in ("UP", "DOWN") and drc != r["final"]:
                cnt += 1
                rec = full.get(r["slug"]) or {}
                price = float(rec.get("buy_price") or med_price)
                was_win = r["win"]
                dwin += (-1 if was_win else 1)
                if was_win:
                    dpnl -= (10.0 / price - 10.0)
                else:
                    dpnl += (10.0 / price - 10.0)
        return dwin, dpnl, cnt, med_price

    L.append(f"- 贴线区内有真实 pnl 记录 {len(pnl_rows)} 场，合计 P&L = **{base_pnl:+.2f} USDT**（固定$10注额口径）。")
    L.append(f"- 翻向重算用的缺价回填：贴线区 buy_price 中位数 ≈ {med_price:.2f}。\n")

    cand = list(B[:6])
    if cand:
        L.append("| 调制器候选 | 逆向场数 | 缩仓×0.5 ΔP&L | SKIP参考 ΔP&L |")
        L.append("|---|---|---|---|")
        for b in cand:
            fn = dict((k, f) for k, _l, _fm, f in SIGNED_FACTORS)[b["key"]]
            dh_, cnth = mod_counterfactual(fn, "half")
            ds_, cnts = mod_counterfactual(fn, "skip")
            L.append(f"| {b['label']} | {cnth} | {dh_:+.2f} | {ds_:+.2f} |")
        L.append("")

    acand = [a for a in A if a["rate"] >= 0.56][:6]
    if acand:
        L.append("| 翻向候选(A类) | 触发场数 | Δ命中 | ΔP&L |")
        L.append("|---|---|---|---|")
        for a in acand:
            fn = dict((k, f) for k, _l, _fm, f in SIGNED_FACTORS)[a["key"]]
            dwin, dpnl, cnt, _mp = flip_counterfactual(fn)
            L.append(f"| {a['label']} (独立命中率{a['rate']*100:.0f}%) | {cnt} | {dwin:+d} | {dpnl:+.2f} |")
        L.append("")

    L.append("> 注：SKIP 违背 round13 全量交易原则，仅作亏损上限参考；缩仓/翻向才具备落地形态。\n")

    # ================= 结论占位 =================
    L.append("## 六、结论\n")
    L.append("（由模型基于上述机算结果撰写，见 docs/research_tiezone_factors_20260823.md）")

    OUT_MD.write_text("\n".join(L) + "\n", encoding="utf-8")

    print(f"贴线区样本 {n_all} 场, 基线胜率 {wins/n_all*100:.1f}%")
    print(f"分析A 有效因子 {len(A)} 个; FDR显著 {len(sig_a)} 个: {[a['label'] for a in sig_a]}")
    print(f"分析B 有效因子 {len(B)} 个; FDR显著 {len(sig_b)} 个: {[b['label'] for b in sig_b]}")
    print(f"B前6名: " + ", ".join(f"{b['label']}(Δ{b['delta']*100:+.1f}pp,p={b['p']:.3f})" for b in B[:6]))
    print(f"A前6名: " + ", ".join(f"{a['label']}({a['rate']*100:.1f}%,p={a['p50']:.3f})" for a in A[:6]))
    print(f"报告写入 {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
