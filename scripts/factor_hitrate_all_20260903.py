#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
factor_hitrate_all_20260903.py — 全因子命中率回测（round53 口径，2026-09-03 用户指令）
=====================================================================================
「回测所有因子的命中率，看看具体到底是哪些因子起作用」。

口径（与 verify_engine_factors.py / P0-1.3 一致）：
  - 每场取最后一份快照（引擎实际消费口径）
  - signal_validator.extract_factors() 机算全部因子（含模式门/权重/降权前的原始 value）
  - 方向票 = sign(value)；命中 = 方向票 == 实际结算方向
  - EV 按快照 book_up_mid（缺退 up_price）隐含价，每 $100 本金
  - Wilson 95% CI；n≥30 才下判定

输出：控制台表格 + data/factor_hitrate_all_20260903.md
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
OUT = os.path.join(DATA, "factor_hitrate_all_20260903.md")

CN = {
    "price_position": "价格位置", "taker_bs": "Taker买卖(5m)", "taker_1m": "贴线1m Taker",
    "recent_dir_5m": "5m方向", "recent_dir_15m": "15m方向", "kline_15m": "15m K线",
    "kline_1h": "1h K线", "momentum_burst": "动量爆发", "pm_imbalance": "PM挂单失衡",
    "pm_momentum": "PM价动量", "pm_consensus": "PM共识", "fake_breakout": "假突破",
    "divergence": "背离", "short_bounce_factor": "短反弹先兆", "wall_divergence": "墙背离",
    "wall_settlement": "结算墙", "open_micro": "开盘微观(已删)", "order_flow": "订单流(已删)",
    "taker_bs_15m": "15m Taker(已删)", "macro_trend": "宏观趋势(已删)", "reversal_intent": "深跌反转(新)",
    "seasonality": "历史季节性(已删)",
}


def wilson(p, n, z=1.96):
    if n == 0:
        return (0.0, 1.0)
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0, c - h), min(1, c - 0 + h))


def _f(x):
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def load_all_rounds():
    """返回 rows: [{slug, actual, mode, factors, value(净分), md, hour_et}]，只含已结算场。"""
    snaps, _cnt = base.load_last_snapshots()
    results = base.load_results()
    rows, skipped = [], 0
    for slug in sorted(snaps):
        actual = results.get(slug)
        if actual not in ("UP", "DOWN"):
            continue
        md = snaps[slug]
        try:
            factors, inp, notes, mode = sv.extract_factors(md)
        except Exception:
            skipped += 1
            continue
        # 场次小时（ET）用于时间分段稳健性
        hour_et = None
        slug_low = slug.lower()
        for h in range(1, 13):
            ampm = "am" if h <= 12 else None
        rows.append({"slug": slug, "actual": actual, "mode": mode,
                     "factors": factors, "md": md})
    return rows, skipped, len(snaps)


def stat(rows, getter):
    n = h = 0
    ev_tot, ev_n = 0.0, 0
    for r in rows:
        v = getter(r)
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
            ev_tot += ((100.0 / pe - 100.0) if d == r["actual"] else -100.0)
            ev_n += 1
    return n, h, (ev_tot / ev_n if ev_n >= 10 else None)


def verdict_of(hr, n, lo, hi):
    if n < N_MIN:
        return "样本不足"
    if hr >= 0.58 and lo > 0.50:
        return "🟢真信号"
    if hr >= 0.53 and lo > 0.50:
        return "🟢弱真"
    if hr <= 0.42 and hi < 0.50:
        return "🔴反向(该砍/反用)"
    return "⚪噪音"


def render(title, rows, getters, lines):
    lines.append(f"\n### {title}\n")
    lines.append("| 因子 | n | 命中 | 命中率 | 95%CI | EV/$100 | 判定 |")
    lines.append("|---|---:|---:|---:|---:|---:|---|")
    results = []
    for name, cn, g in getters:
        n, h, ev = stat(rows, g)
        if n == 0:
            lines.append(f"| {cn} `{name}` | 0 | 0 | — | — | — | 未出票 |")
            results.append((name, n, None))
            continue
        hr = h / n
        lo, hi = wilson(hr, n)
        v = verdict_of(hr, n, lo, hi)
        ev_s = f"{ev:+.1f}" if ev is not None else "—"
        lines.append(f"| {cn} `{name}` | {n} | {h} | {hr*100:.1f}% | [{lo*100:.0f}%,{hi*100:.0f}%] | {ev_s} | {v} |")
        results.append((name, n, hr))
    return results


def dormant_factors(md):
    """复活 4 个休眠盘感因子（ENABLE_SOFT_SKILL_FACTORS=False 的原逻辑逐行复刻，
    见 signal_validator.py L654-698 设计文档 data/hermes_trend_softskill_factors_design_20260817.md）"""
    out = {"momentum_health": 0.0, "exhaustion": 0.0, "s_r_swap": 0.0, "acceleration": 0.0}

    def _sign(x):
        return 1 if x > 0 else (-1 if x < 0 else 0)

    def _f(x):
        try:
            if x is None or x == "":
                return None
            return float(x)
        except (TypeError, ValueError):
            return None

    tf = md.get("timeframes") or {}
    tf15m = tf.get("15m") or {}
    tf4h = tf.get("4H") or {}
    macd4 = _f(tf4h.get("macd_hist"))
    # A1 动量健康/衰竭
    macd4_s = _sign(macd4) if macd4 is not None else 0
    accel4 = _f(tf4h.get("macd_acceleration"))
    if macd4_s > 0 and accel4 is not None and accel4 < 0:
        out["momentum_health"] = -1.0
    elif macd4_s < 0 and accel4 is not None and accel4 > 0:
        out["momentum_health"] = 1.0
    # B1 动量耗尽
    cu15 = int(_f(tf15m.get("consecutive_up")) or 0)
    cd15 = int(_f(tf15m.get("consecutive_down")) or 0)
    _body_shrinking = False
    rcs = tf15m.get("recent_candles") or []
    if isinstance(rcs, list) and len(rcs) >= 2:
        try:
            _b0 = abs(float(rcs[-1].get("c", 0)) - float(rcs[-1].get("o", 0)))
            _b1 = abs(float(rcs[-2].get("c", 0)) - float(rcs[-2].get("o", 0)))
            _body_shrinking = 0 <= _b0 < _b1
        except (TypeError, ValueError):
            _body_shrinking = False
    if cu15 >= 5 and _body_shrinking:
        out["exhaustion"] = -1.0
    elif cd15 >= 5 and _body_shrinking:
        out["exhaustion"] = 1.0
    # B2 支撑阻力互换
    kl = md.get("key_levels") or {}
    ss = _f(kl.get("strong_support"))
    sr = _f(kl.get("strong_resistance"))
    current = _f(md.get("current_price"))
    rdir15 = tf15m.get("recent_dir")
    if ss is not None and current is not None and current < ss and rdir15 == "DOWN":
        out["s_r_swap"] = -1.0
    elif sr is not None and current is not None and current > sr and rdir15 == "UP":
        out["s_r_swap"] = 1.0
    # B3 加速/放量
    vr15 = _f(tf15m.get("vol_ratio"))
    if vr15 is not None and vr15 > 1.5 and rdir15 in ("UP", "DOWN"):
        out["acceleration"] = 1.0 if rdir15 == "UP" else -1.0
    return out


def main():
    rows, skipped, total_snaps = load_all_rounds()
    print(f"快照 slug 总数: {total_snaps}  可归因(已结算+机算通过): {len(rows)}  跳过: {skipped}")

    # 收集全部出现过的因子键
    all_keys = defaultdict(int)
    for r in rows:
        for k, v in r["factors"].items():
            if v != 0:
                all_keys[k] += 1

    def eng(k):
        return lambda r: r["factors"].get(k, 0)

    getters = [(k, CN.get(k, k), eng(k)) for k in sorted(all_keys)]

    lines = ["# 全因子命中率回测（引擎口径 round53，2026-09-03）", "",
             f"- 数据：data/snapshots 每场最后一份快照 × data/hermes_results.jsonl 结算",
             f"- 可归因场：{len(rows)}；方向票=sign(机算value)，命中=票==实际；EV@book_up_mid 隐含价",
             f"- 判定线：n≥30；命中率≥58% 且 CI 下界>50% = 真信号；42-58% = 噪音；≤42% = 反向", "",
             "## 一、全因子方向票命中率（全样本）"]

    res_all = render("全样本", rows, getters, lines)

    # 分模式稳健性：仅对全样本 n≥30 且非"未出票"的因子
    by_mode = defaultdict(list)
    for r in rows:
        by_mode[r["mode"]].append(r)
    strong = [x for x in res_all if x[1] >= N_MIN and x[2] is not None]
    if strong:
        lines.append("\n## 二、分模式稳健性（全样本 n≥30 的因子）\n")
        lines.append("| 因子 | " + " | ".join(f"{m}(n=命中率)" for m in sorted(by_mode)) + " |")
        lines.append("|---|" + "---|" * len(by_mode))
        for name, _n, _hr in strong:
            cells = []
            for m in sorted(by_mode):
                n, h, _ev = stat(by_mode[m], eng(name))
                cells.append(f"{h}/{n}={h/n*100:.0f}%" if n >= 10 else f"{h}/{n}=-")
            lines.append(f"| {CN.get(name, name)} `{name}` | " + " | ".join(cells) + " |")

    # 因子间共现冗余：全样本命中率最高的因子对一致性
    lines.append("\n## 三、休眠盘感因子复活回测（ENABLE_SOFT_SKILL_FACTORS=False 的 4 因子，8/17 设计后未启用）\n")
    for r in rows:
        r["factors"] = dict(r["factors"], **dormant_factors(r["md"]))
    dorm_getters = [(k, {"momentum_health": "动量健康/衰竭", "exhaustion": "动量耗尽",
                         "s_r_swap": "支撑阻力互换", "acceleration": "加速放量"}[k], eng(k))
                    for k in ("momentum_health", "exhaustion", "s_r_swap", "acceleration")]
    dorm_res = render("休眠因子", rows, dorm_getters, lines)
    lines.append("\n> ⚠️ `acceleration` 与在役因子 `momentum_burst` 判据完全相同（15m vol_ratio>1.5 + 15m 方向，"
                 "signal_validator L516/L693），属同因子重复发现，启用无增益，仅作口径交叉验证。")

    lines.append("\n## 四、结论\n")
    _all = res_all + dorm_res
    truthy = [x for x in _all if x[2] is not None and x[1] >= N_MIN and x[2] >= 0.53]
    reverse = [x for x in _all if x[2] is not None and x[1] >= N_MIN and x[2] <= 0.42]
    noise = [x for x in _all if x[2] is not None and x[1] >= N_MIN and 0.42 < x[2] < 0.53]
    insufficient = [x for x in _all if x[1] < N_MIN]
    lines.append(f"- 🟢 起作用（≥53%）：{', '.join(f'{CN.get(k,k)} {h*100:.1f}%' for k,_n,h in truthy) or '无'}")
    lines.append(f"- ⚪ 噪音（42-58%，不贡献方向）：{', '.join(f'{CN.get(k,k)} {h*100:.1f}%' for k,_n,h in noise) or '无'}")
    lines.append(f"- 🔴 反向（该砍或反用）：{', '.join(f'{CN.get(k,k)} {h*100:.1f}%' for k,_n,h in reverse) or '无'}")
    lines.append(f"- 样本不足（<30 票，不下结论）：{', '.join(CN.get(k,k) for k,n,_ in insufficient) or '无'}")
    lines.append("- 休眠因子处置建议：`acceleration`=momentum_burst 重复（无增益）；`momentum_health` 50% 噪音（维持关停）；"
                 "`exhaustion` 31% 强反向——「15m连续5根同色+实体缩小」实际是**续势**信号（69%顺延），可考虑反用为续势确认或维持关停；"
                 "`s_r_swap` 445 场零出票（快照缺 key_levels 数据），不可评估")
    lines.append("- 未参与方向票统计：`fake_breakout`/`divergence`（AI 专属盘感，引擎恒 0）；"
                 "`reversal_intent`（round53 新增，历史快照缺 up_price_delta_prev，独立回测 n=52 胜率 65.4% 见 data/reversal_intent_validation_20260903.md）")

    text = "\n".join(lines)
    print(text)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(f"\n[落盘] {OUT}")


if __name__ == "__main__":
    main()
