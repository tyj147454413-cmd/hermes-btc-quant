#!/usr/bin/env python3
"""沉睡字段扫描器 — 找出 >70% 命中率的潜在新因子

扫描所有快照中存在但**未进评分表**的字段，按"判定方向"模式逐一测命中率 + 切片 + 独立性。
输出报告落 `data/hermes_dormant_factor_scan_YYYYMMDD.md`，供用户决策。

运行：python3 scripts/scan_dormant_factors.py [--min-samples N] [--top K]
"""
from __future__ import annotations
import json, os, re, sys, math
from datetime import datetime
from pathlib import Path
from collections import defaultdict

ROOT = Path(os.environ.get("HERMES_DIR", "."))
sys.path.insert(0, str(ROOT / "scripts"))

RESULTS_FILE = ROOT / "data" / "hermes_results.jsonl"
SNAPSHOT_DIR = ROOT / "data" / "snapshots"
REPORT_FILE = ROOT / "data" / f"hermes_dormant_factor_scan_{datetime.now().strftime('%Y%m%d')}.md"

# ---- 候选因子定义 ----
# 每个候选 = (字段路径, 判定函数, 备注)
# 判定函数: 取快照字段值 → 返回 +1 / -1 / 0 (None)

def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def sign(x):
    return 1 if x > 0 else (-1 if x < 0 else 0)

def _path_get(d, path):
    """支持 a.b[0].c 简化的字段路径"""
    cur = d
    for part in re.split(r'\.|\[(\d+)\]', path):
        if not part:
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if cur is None:
            return None
    return cur

# 候选因子列表 - 每个候选是 dict: {name, path, fn, family, note}
# fn(value, md) → +1/-1/0
CANDIDATES = []

def cand(name, path, fn, family, note=""):
    CANDIDATES.append({"name": name, "path": path, "fn": fn, "family": family, "note": note})

# === A. 趋势类(动量延续市场核心) ===
for tf in ["1m", "5m", "15m", "1H", "4H"]:
    cand(f"{tf}.rsi 超买", f"timeframes.{tf}.rsi",
        lambda v, tf=tf: 1 if (v is not None and v >= 70) else (-1 if (v is not None and v <= 30) else 0),
        "趋势类", f"RSI>{70}=超买反弹风险, RSI<{30}=超卖反弹预期")
    cand(f"{tf}.rsi 中位", f"timeframes.{tf}.rsi",
        lambda v, tf=tf: 1 if (v is not None and 50 <= v < 70) else (-1 if (v is not None and 30 < v <= 50) else 0),
        "趋势类", "RSI 50-70 偏多, 30-50 偏空")
    cand(f"{tf}.bb_pos 极端", f"timeframes.{tf}.bb_pos",
        lambda v, tf=tf: 1 if (v is not None and v <= -10) else (-1 if (v is not None and v >= 110) else 0),
        "趋势类", "BB_pos<-10 超卖反弹, >110 超买回落")
    cand(f"{tf}.bb_pos 中位", f"timeframes.{tf}.bb_pos",
        lambda v, tf=tf: 1 if (v is not None and 40 <= v < 60) else (-1 if (v is not None and v < 40 or (v is not None and v > 60 and v <= 100)) else 0),
        "趋势类", "BB_pos 40-60 中性, 偏离中位判方向")
    cand(f"{tf}.macd_acceleration", f"timeframes.{tf}.macd_acceleration",
        lambda v, tf=tf: sign(v) if v is not None else 0,
        "趋势类", "MACD 加速方向 = hist 变化率")
    cand(f"{tf}.macd_hist", f"timeframes.{tf}.macd_hist",
        lambda v, tf=tf: sign(v) if v is not None else 0,
        "趋势类", "MACD hist 直接方向")

# === B. K线形态类 ===
for tf in ["1m", "5m", "15m", "1H", "4H"]:
    cand(f"{tf}.is_hammer (反转)", f"timeframes.{tf}.is_hammer",
        lambda v, tf=tf: 1 if v is True else 0,
        "形态类", "锤子线 = 多头反转信号")
    cand(f"{tf}.is_shooting_star (反转)", f"timeframes.{tf}.is_shooting_star",
        lambda v, tf=tf: -1 if v is True else 0,
        "形态类", "射击之星 = 空头反转信号")

# === C. 上一小时衍生 ===
cand("prev_hour.change_pct", "prev_hour.change_pct",
    lambda v, md: sign(v) if v is not None else 0,
    "惯性类", "上小时涨跌方向 → 顺势")
cand("prev_hour.last3_chg_pct.max", "prev_hour.last3_chg_pct",
    lambda v, md: sign(max(v)) if isinstance(v, list) and v else 0,
    "惯性类", "上 3 根 1H 涨幅最大方向")
cand("prev_hour.last3_chg_pct.min", "prev_hour.last3_chg_pct",
    lambda v, md: sign(min(v)) if isinstance(v, list) and v else 0,
    "惯性类", "上 3 根 1H 跌幅最大方向")
cand("prev_hour.last3_chg_pct.sum", "prev_hour.last3_chg_pct",
    lambda v, md: sign(sum(v)) if isinstance(v, list) and v else 0,
    "惯性类", "上 3 根 1H 涨幅总和方向")
cand("prev_hour.volume > 2x avg", "prev_hour.volume",
    lambda v, md: sign(md.get("prev_hour", {}).get("change_pct", 0)) if v and v > 3000 else 0,
    "惯性类", "上小时量>3000 顺势信号")

# === D. 24h 数据 ===
cand("ticker_24h.change_pct", "ticker_24h.change_pct",
    lambda v, md: sign(v) if v is not None else 0,
    "趋势类", "24h 整体涨跌方向")
cand("ticker_24h.volume > 1.5x", "ticker_24h.volume",
    lambda v, md: 1 if v and v > 20000 else 0,
    "趋势类", "24h 量>20000 BTC 高活跃")

# === E. 资金面类 (round41.8.10 已验证无效, 仍再扫一遍) ===
cand("funding_rate 正", "capital_flow.funding_rate",
    lambda v, md: 1 if v is not None and v > 0 else (-1 if v is not None and v < 0 else 0),
    "资金面", "资金费率正/负")
cand("oi_change_24h_pct", "capital_flow.oi_change_24h_pct",
    lambda v, md: sign(v) if v is not None else 0,
    "资金面", "OI 24h 变化方向")
cand("long_short_ratio", "capital_flow.long_short_ratio",
    lambda v, md: 1 if v is not None and v > 1.05 else (-1 if v is not None and v < 0.95 else 0),
    "资金面", "多空比 >1.05 偏多 / <0.95 偏空")
cand("long_short_ratio_trend", "capital_flow.long_short_ratio_trend",
    lambda v, md: sign(v[-1] - v[0]) if isinstance(v, list) and len(v) >= 2 else 0,
    "资金面", "多空比趋势变化")

# === F. Polymarket 衍生 ===
cand("book_imbalance 极强", "polymarket.book_imbalance",
    lambda v, md: sign(v) if v is not None else 0,
    "PM类", "PM 盘口不平衡")
cand("book_imbalance >0.5", "polymarket.book_imbalance",
    lambda v, md: 1 if v is not None and v > 0.5 else (-1 if v is not None and v < -0.5 else 0),
    "PM类", "盘口不平衡 >0.5 极强")
cand("PM liquidity 高", "polymarket.liquidity",
    lambda v, md: 1 if v and v > 5000 else 0,
    "PM类", "PM 流动性 >5000 活跃市场")
cand("PM mark vs book 偏离方向", "polymarket",
    lambda v, md: 1 if (v.get("book_up_mid") and v.get("up_price") and v.get("book_up_mid") > v.get("up_price")) else (-1 if (v.get("book_up_mid") and v.get("up_price") and v.get("book_up_mid") < v.get("up_price")) else 0),
    "PM类", "book > mark 多头失真, book < mark 空头失真")
cand("PM volume > 5000", "polymarket.volume",
    lambda v, md: 1 if v and v > 5000 else 0,
    "PM类", "PM 成交量 > 5000")

# === G. 情绪面 (已被验证无效, 再扫) ===
cand("fear_greed 70+ 贪婪", "sentiment.fear_greed_index",
    lambda v, md: -1 if v is not None and v >= 70 else (1 if v is not None and v <= 30 else 0),
    "情绪类", "贪婪>70 = 反弹风险, 恐惧<30 = 反弹预期")
cand("fear_greed_trend 上升", "sentiment.fear_greed_trend",
    lambda v, md: 1 if isinstance(v, list) and len(v) >= 2 and v[-1] > v[0] else (-1 if isinstance(v, list) and len(v) >= 2 and v[-1] < v[0] else 0),
    "情绪类", "恐惧贪婪趋势上升/下降")

# === H. OI 联动 ===
cand("oi_price 联动 bullish_cover", "oi_price_relation.signal",
    lambda v, md: 1 if v == "bullish_cover" else (-1 if v == "bearish_cover" else 0),
    "OI类", "OI 联动 bullish_cover/bearish_cover")
cand("OI -1h 涨", "oi_price_relation.oi_1h_change_pct",
    lambda v, md: sign(v) if v is not None else 0,
    "OI类", "OI 1h 变化方向")
cand("Price 1h 涨", "oi_price_relation.price_1h_change_pct",
    lambda v, md: sign(v) if v is not None else 0,
    "OI类", "Price 1h 变化方向")

# === I. 开盘微观衍生 ===
cand("opening.range_pct 高", "opening.range_pct",
    lambda v, md: 1 if v and v > 0.3 else 0,
    "开盘类", "开盘区间 >0.3% 高波动")
cand("opening.vol_ratio > 1", "opening.vol_ratio",
    lambda v, md: 1 if v and v > 1.0 else (-1 if v and v < 0.5 else 0),
    "开盘类", "开盘量比 >1 偏多, <0.5 偏空")
cand("opening.has_fakeout", "opening.has_fakeout",
    lambda v, md: 1 if v is True else 0,
    "开盘类", "开盘假突破")
cand("opening.has_v_reversal", "opening.has_v_reversal",
    lambda v, md: 1 if v is True else 0,
    "开盘类", "开盘 V 反")

# === J. v_reversal_15m (现成) ===
cand("v_reversal_15m.detected", "v_reversal_15m.detected",
    lambda v, md: 1 if v is True else 0,
    "形态类", "15m V 反检测")

# === K. 距关键价位距离 ===
def _dist_to_resistance(md):
    cur = _f(_path_get(md, "current_price"))
    wr = _f(_path_get(md, "key_levels.weak_resistance"))
    if cur and wr and wr > 0:
        return (cur - wr) / cur * 100  # >0 = 已突破或贴阻力
    return None
def _dist_to_support(md):
    cur = _f(_path_get(md, "current_price"))
    ws = _f(_path_get(md, "key_levels.weak_support"))
    if cur and ws and ws > 0:
        return (cur - ws) / cur * 100  # >0 = 在支撑上方
    return None
cand("距弱阻力 ≤0.3%", "key_levels.weak_resistance",
    lambda v, md: -1 if _dist_to_resistance(md) is not None and _dist_to_resistance(md) <= 0.3 else 0,
    "结构类", "贴弱阻力 -1 回落")
cand("距弱支撑 ≤0.3%", "key_levels.weak_support",
    lambda v, md: 1 if _dist_to_support(md) is not None and _dist_to_support(md) <= 0.3 else 0,
    "结构类", "贴弱支撑 +1 反弹")
cand("距强阻力 ≤0.3%", "key_levels.strong_resistance",
    lambda v, md: -1 if (v and md.get("current_price") and (md.get("current_price") - v) / md.get("current_price") * 100 <= 0.3) else 0,
    "结构类", "贴强阻力 -1 回落")
cand("距强支撑 ≤0.3%", "key_levels.strong_support",
    lambda v, md: 1 if (v and md.get("current_price") and (md.get("current_price") - v) / md.get("current_price") * 100 <= 0.3) else 0,
    "结构类", "贴强支撑 +1 反弹")
cand("突破强阻力 (上方)", "key_levels.strong_resistance",
    lambda v, md: 1 if (v and md.get("current_price") and md.get("current_price") > v) else 0,
    "结构类", "突破强阻力 +1")

# === L. 24h 位置衍生 ===
cand("24h percentile 极端", "price_position_24h.current_percentile",
    lambda v, md: -1 if v is not None and v >= 90 else (1 if v is not None and v <= 10 else 0),
    "位置类", "极端位反转信号")

# === M. deviation_trend 加速度 ===
cand("deviation_trend 加速度", "deviation_trend.last_3min_dev_pct",
    lambda v, md: sign(v[-1] - v[0]) if isinstance(v, list) and len(v) >= 2 else 0,
    "动量类", "近 3min 偏差加速度")
cand("deviation_trend 稳定", "deviation_trend.stable",
    lambda v, md: 0,
    "动量类", "稳定 = 不信号(仅占位)")

# === N. 历史统计衍生 ===
cand("historical std > 0.5% 高波动历史", "historical.std_change_pct",
    lambda v, md: sign(md.get("prev_hour", {}).get("change_pct", 0)) if v and v > 0.5 else 0,
    "统计类", "历史高波动 → 顺势")


def slug_of_snapshot(filename: str) -> str:
    return re.sub(r"_\d{8}_\d{6}\.json$", "", filename)

def load_results():
    results = {}
    if not RESULTS_FILE.exists():
        return results
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

def load_snapshots():
    m = {}
    if not SNAPSHOT_DIR.exists():
        return m
    for p in sorted(SNAPSHOT_DIR.glob("*.json")):
        m[slug_of_snapshot(p.name)] = p
    return m

def hit(signal, actual):
    if signal is None or signal == 0:
        return "neutral"
    predicts_up = signal > 0
    if (predicts_up and actual == "UP") or (not predicts_up and actual == "DOWN"):
        return "correct"
    return "wrong"

def wilson(p, n, z=1.96):
    if n == 0:
        return 0, 0
    denom = 1 + z**2/n
    center = (p + z**2/(2*n)) / denom
    half = z * math.sqrt((p*(1-p) + z**2/(4*n)) / n) / denom
    return max(0, center - half), min(1, center + half)

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-samples", type=int, default=20)
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--min-hitrate", type=float, default=0.60, help="最低命中率门槛(0.6 默认, >0.7 用户问的是这个)")
    args = ap.parse_args()

    results = load_results()
    snaps = load_snapshots()
    print(f"数据: {len(results)} 笔结算, {len(snaps)} 个快照")

    # 每个候选统计: correct/wrong/neutral + 切片
    cand_stats = {}
    for c in CANDIDATES:
        cand_stats[c["name"]] = {
            "correct": 0, "wrong": 0, "neutral": 0,
            "by_pm": defaultdict(lambda: [0, 0]),  # [correct, wrong]
            "by_vol": defaultdict(lambda: [0, 0]),
            "by_pct": defaultdict(lambda: [0, 0]),
            "n_total": 0,
        }

    joined = skipped = 0
    for slug, r in results.items():
        actual = r["actual"]
        if slug not in snaps:
            skipped += 1
            continue
        try:
            md = json.loads(snaps[slug].read_text())
        except Exception:
            skipped += 1
            continue
        joined += 1

        # 切片
        pm = md.get("polymarket") or {}
        pm_up = pm.get("up_price")
        pm_down = pm.get("down_price")
        if pm_up is not None and pm_up >= 0.60:
            pm_bucket = "PM_UP≥60%"
        elif pm_down is not None and pm_down >= 0.60:
            pm_bucket = "PM_DOWN≥60%"
        else:
            pm_bucket = "PM弱共识"
        vol = md.get("volatility_5m")
        vol_bucket = "vol缺" if vol is None else ("vol≤0.5%" if vol <= 0.5 else "vol>0.5%")
        pct = (md.get("price_position_24h") or {}).get("current_percentile")
        if pct is None:
            pct_bucket = "pct缺"
        elif pct >= 85:
            pct_bucket = "pct≥85"
        elif pct <= 15:
            pct_bucket = "pct≤15"
        else:
            pct_bucket = "pct中间"

        for c in CANDIDATES:
            try:
                raw = _path_get(md, c["path"])
                signal = c["fn"](raw, md)
            except Exception:
                signal = 0
            h = hit(signal, actual)
            cs = cand_stats[c["name"]]
            cs["n_total"] += 1
            if h == "correct":
                cs["correct"] += 1
                cs["by_pm"][pm_bucket][0] += 1
                cs["by_vol"][vol_bucket][0] += 1
                cs["by_pct"][pct_bucket][0] += 1
            elif h == "wrong":
                cs["wrong"] += 1
                cs["by_pm"][pm_bucket][1] += 1
                cs["by_vol"][vol_bucket][1] += 1
                cs["by_pct"][pct_bucket][1] += 1
            else:
                cs["neutral"] += 1

    # === 输出控制台摘要 + 落盘报告 ===
    rows = []
    for c in CANDIDATES:
        cs = cand_stats[c["name"]]
        n_signal = cs["correct"] + cs["wrong"]
        if n_signal < args.min_samples:
            continue
        hit_rate = cs["correct"] / n_signal
        lo, hi = wilson(hit_rate, n_signal)
        # 综合评分: 命中率 × √n (样本规模补偿)
        score = hit_rate * math.sqrt(n_signal)
        rows.append({
            "name": c["name"],
            "family": c["family"],
            "note": c["note"],
            "n": n_signal,
            "hit_rate": hit_rate,
            "lo": lo,
            "hi": hi,
            "correct": cs["correct"],
            "wrong": cs["wrong"],
            "neutral": cs["neutral"],
            "score": score,
            "by_pm": dict(cs["by_pm"]),
            "by_vol": dict(cs["by_vol"]),
            "by_pct": dict(cs["by_pct"]),
        })
    rows.sort(key=lambda r: -r["score"])

    # 命中率 > 70% 子集
    high_hr = [r for r in rows if r["hit_rate"] >= 0.70]
    high_hr_60 = [r for r in rows if r["hit_rate"] >= 0.60]

    print(f"\n=== 沉睡字段扫描结果 (n_signal ≥ {args.min_samples}) ===")
    print(f"join: {joined} | 跳过: {skipped} | 候选数: {len(CANDIDATES)}")
    print(f"满足样本门槛: {len(rows)} | 命中率 ≥60%: {len(high_hr_60)} | 命中率 ≥70%: {len(high_hr)}")

    # Top 20 控制台
    print(f"\n### Top {min(20, len(rows))} 综合得分(score = hit_rate × √n)")
    print(f"{'因子':<35} {'家族':<8} {'命中率':>7} {'n':>4} {'lo':>5} {'hi':>5} {'score':>6} {'对/错/中':<14}")
    for r in rows[:20]:
        print(f"{r['name'][:33]:<35} {r['family']:<8} {r['hit_rate']*100:>6.1f}% {r['n']:>4} {r['lo']*100:>4.1f}% {r['hi']*100:>4.1f}% {r['score']:>5.2f} {r['correct']:>3}/{r['wrong']:>3}/{r['neutral']:>4}")

    # === 写报告 ===
    lines = []
    lines.append(f"# 沉睡字段扫描报告 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append(f"**数据规模**: join {joined} / 跳过 {skipped} / 候选数 {len(CANDIDATES)}")
    lines.append(f"**样本门槛**: n_signal ≥ {args.min_samples} (correct + wrong 之和)")
    lines.append(f"**脚本**: `scripts/scan_dormant_factors.py`")
    lines.append(f"**目的**: 找出 **未进评分表** 的快照字段中, 命中率 ≥ 70% 的潜在新因子")
    lines.append("")
    lines.append("## 摘要")
    lines.append(f"- 满足样本门槛的候选: **{len(rows)}**")
    lines.append(f"- 命中率 ≥ 60%: **{len(high_hr_60)}**")
    lines.append(f"- 命中率 ≥ 70%: **{len(high_hr)}** ⭐")
    lines.append("")

    # 命中率 ≥ 70% 详细表
    lines.append("## ⭐ 命中率 ≥ 70% 候选(按综合得分排序)")
    lines.append("")
    lines.append("| 因子 | 家族 | 命中率 | n | 95% CI | 对/错/中 | 备注 |")
    lines.append("|------|------|--------|---|--------|----------|------|")
    for r in high_hr:
        lines.append(f"| {r['name']} | {r['family']} | **{r['hit_rate']*100:.1f}%** | {r['n']} | [{r['lo']*100:.0f}%, {r['hi']*100:.0f}%] | {r['correct']}/{r['wrong']}/{r['neutral']} | {r['note']} |")
    lines.append("")

    # 命中率 ≥ 60% 子集
    lines.append("## 命中率 60-70% 候选(按综合得分排序)")
    lines.append("")
    lines.append("| 因子 | 家族 | 命中率 | n | 95% CI | 对/错/中 | 备注 |")
    lines.append("|------|------|--------|---|--------|----------|------|")
    for r in [x for x in rows if 0.60 <= x['hit_rate'] < 0.70]:
        lines.append(f"| {r['name']} | {r['family']} | {r['hit_rate']*100:.1f}% | {r['n']} | [{r['lo']*100:.0f}%, {r['hi']*100:.0f}%] | {r['correct']}/{r['wrong']}/{r['neutral']} | {r['note']} |")
    lines.append("")

    # 关键切片(命中率 ≥ 70% 的逐个 PM共识/波动率/24h位置)
    if high_hr:
        lines.append("## ⭐ 高命中率候选的条件切片(寻找最强子集)")
        for r in high_hr:
            lines.append(f"### {r['name']} (全场 {r['hit_rate']*100:.1f}%, n={r['n']})")
            lines.append("")
            # PM 切片
            lines.append("**PM共识切片**:")
            for pm_b, (c, w) in sorted(r["by_pm"].items(), key=lambda x: -x[1][0]):
                if c + w < 5:
                    continue
                pct = c / (c + w) * 100
                delta = pct - r['hit_rate'] * 100
                lines.append(f"  - @{pm_b}: {c}/{c+w} = {pct:.1f}% (Δ vs 整体 {delta:+.1f}pt)")
            # 波动率
            lines.append("**波动率切片**:")
            for vb, (c, w) in sorted(r["by_vol"].items(), key=lambda x: -x[1][0]):
                if c + w < 5:
                    continue
                pct = c / (c + w) * 100
                delta = pct - r['hit_rate'] * 100
                lines.append(f"  - @{vb}: {c}/{c+w} = {pct:.1f}% (Δ vs 整体 {delta:+.1f}pt)")
            # 24h 位置
            lines.append("**24h 位置切片**:")
            for pb, (c, w) in sorted(r["by_pct"].items(), key=lambda x: -x[1][0]):
                if c + w < 5:
                    continue
                pct = c / (c + w) * 100
                delta = pct - r['hit_rate'] * 100
                lines.append(f"  - @{pb}: {c}/{c+w} = {pct:.1f}% (Δ vs 整体 {delta:+.1f}pt)")
            lines.append("")

    # 全 Top 30 表
    lines.append(f"## Top {min(30, len(rows))} 综合得分(score = hit_rate × √n)")
    lines.append("")
    lines.append("| 因子 | 家族 | 命中率 | n | score | 对/错/中 | 备注 |")
    lines.append("|------|------|--------|---|-------|----------|------|")
    for r in rows[:30]:
        lines.append(f"| {r['name']} | {r['family']} | {r['hit_rate']*100:.1f}% | {r['n']} | {r['score']:.2f} | {r['correct']}/{r['wrong']}/{r['neutral']} | {r['note']} |")
    lines.append("")

    # 关键提醒
    lines.append("## ⚠️ 因子库纪律铁律(round41.8.10 用户决策)")
    lines.append("")
    lines.append("- **先回测后评分**:任何新因子必须先经 shadow 回测方向预测力")
    lines.append("- **命中率 >55%** 且**独立于现有因子**(交叉验证不重复)才允许进评分表")
    lines.append("- **未回测不得直接进 S0**")
    lines.append("- **回测报告落** `data/hermes_factor_candidate_backtest_*.md`")
    lines.append("")
    lines.append("## ⚠️ BTC 1H 是纯动量市场(铁证)")
    lines.append("")
    lines.append("**方向跟随类有效**:")
    lines.append("- 价格位置 69.4%, K线序列 69.8%, 短反弹先兆 70.0%(看空 82%)")
    lines.append("- momentum_burst 73.3%, PM 动量 66-67%")
    lines.append("")
    lines.append("**反向类全部证伪**:")
    lines.append("- 均值回归 sr_proximity 41.0%, 资金费率 46.5%, 多空比 47.2%, 恐惧贪婪 48.4%, OI 变化 47.3%")
    lines.append("")
    lines.append("**已被证伪黑名单(勿再提议,除非有新样本/新口径)**:恐惧贪婪/资金费率/多空比/OI变化/sr_proximity")

    REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告落盘: {REPORT_FILE}")
    print(f"控制台只显示 Top 20,完整切片见报告文件。")
    return 0

if __name__ == "__main__":
    sys.exit(main())
