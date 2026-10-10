#!/usr/bin/env python3
"""
图表分析.md v1.4 引擎离线回测（纯历史K线+资金面，无锚定价）

目的：验证"图表分析.md"三维预演框架（庄家博弈/散户心理/历史数据 + 百家乐连庄 + 德州扑克读庄）
是否能从"只用历史数据"预测当前小时方向，胜率是否突破 63% 天花板。

铁律（图表分析.md §〇）：
- 预测对象 = 快照 slug 标注的 ET 小时涨跌
- 输入只用"截至上一小时收盘"的历史：prev_hour + klines_1h截断到prev_hour + historical + capital_flow
- 禁用当前小时数据：polymarket / anchor_price / current_price / deviation / timeframes当前小时

回测策略：把 v1.4 可量化规则实现为确定性规则引擎（非LLM），对每个有结算结果的快照
输出方向，对比 actual 统计胜率 + Wilson 下界。纯本地，无网络。

输出：终端摘要 + data/shape_flow_backtest_*.md
"""
# pyright: reportAny=false, reportExplicitAny=false, reportUnknownVariableType=false, reportUnknownMemberType=false

import json
import math
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
DATA_DIR = THIS_DIR.parent / "data"
SNAP_DIR = DATA_DIR / "snapshots"
RESULTS = DATA_DIR / "hermes_results.jsonl"

ET = timezone(timedelta(hours=-4))


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def wilson_ci(w: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = w / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return center - half, center + half


def _s(v, d=0.0):
    """安全取值。"""
    try:
        return float(v) if v is not None else d
    except (TypeError, ValueError):
        return d


def load_snapshot(f: Path) -> dict | None:
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def trunc_klines_to_prev_hour(klines: list[dict], prev_hour_ts: int) -> list[dict]:
    """只保留 <= prev_hour 开盘时间的那根及之前（即排除当前小时未收盘K线）。"""
    out = []
    for k in klines:
        if k["t"] // 1000 < prev_hour_ts:
            out.append(k)
    return out


def extract_features(d: dict) -> dict:
    """从快照提取图表分析.md v1.4 可量化特征（只用历史，不用当前小时）。"""
    ph = d.get("prev_hour") or {}
    cf = d.get("capital_flow") or {}
    hist = d.get("historical") or {}
    kl = d.get("klines_1h") or []

    # --- 连庄 / 方向序列（来自 prev_hour 与 klines 截断） ---
    seq = []
    for k in kl:
        if k["t"] // 1000 < _s(ph.get("close", 0)):  # fallback; real trunc below
            pass
    # 方向序列：用 klines_1h 所有已收盘根（最后1根=当前小时，去掉）
    dir_seq = []
    if kl:
        for k in kl[:-1]:
            dir_seq.append("UP" if k["c"] >= k["o"] else "DOWN")
    # 连庄长度
    consecutive = 0
    if dir_seq:
        last_dir = dir_seq[-1]
        for sd in reversed(dir_seq):
            if sd == last_dir:
                consecutive += 1
            else:
                break
    prev_side = ph.get("side")
    prev_dir_hist = ph.get("direction_history") or []
    flips = _s(ph.get("flips"), 1)
    consecutive_up = _s(ph.get("consecutive_up"), 0)
    consecutive_down = _s(ph.get("consecutive_down"), 0)

    # --- 庄家剧本（taker + OI） ---
    taker = _s(cf.get("taker_bs_ratio"), 1.0)
    oi_chg = _s(cf.get("oi_change_24h_pct"), 0.0)
    taker_trend = cf.get("taker_bs_trend") or []
    taker_trend_dir = 0
    if len(taker_trend) >= 3:
        taker_trend_dir = 1 if taker_trend[-1] > taker_trend[0] else (-1 if taker_trend[-1] < taker_trend[0] else 0)

    # --- 散户反指 ---
    lsr = _s(cf.get("long_short_ratio"), 1.0)
    order_flow = (cf.get("order_flow") or {}).get("imbalance_5m")
    of = _s(order_flow, 0.0)

    # --- 历史时段 ---
    up_rate = _s(hist.get("up_rate"), 0.5)
    n_hist = _s(hist.get("total_samples"), 0)

    # --- 结构位（从截断 klines 算 24h 高低点） ---
    highs = [k["h"] for k in kl[:-1]][-24:]
    lows = [k["l"] for k in kl[:-1]][-24:]
    hi24 = max(highs) if highs else 0
    lo24 = min(lows) if lows else 0
    # prev close 作为"现价"基准（因为不能用当前小时价格）
    prev_close = _s(ph.get("close"), 0)

    return {
        "dir_seq": dir_seq,
        "consecutive": consecutive,
        "prev_side": prev_side,
        "prev_dir_hist": prev_dir_hist,
        "flips": flips,
        "consecutive_up": consecutive_up,
        "consecutive_down": consecutive_down,
        "taker": taker,
        "oi_chg": oi_chg,
        "taker_trend_dir": taker_trend_dir,
        "lsr": lsr,
        "order_flow": of,
        "up_rate": up_rate,
        "n_hist": n_hist,
        "hi24": hi24,
        "lo24": lo24,
        "prev_close": prev_close,
    }


def judge(f: dict) -> tuple[str, float, str]:
    """
    图表分析.md v1.4 可量化规则引擎。
    返回 (方向UP/DOWN, 置信度0-1, 判定依据描述)。
    """
    reasons = []

    # === 视角① 庄家剧本（taker + OI + order_flow）===
    # 德州扑克：价值下注 vs 诈唬（OI 是关键）
    taker = f["taker"]
    oi = f["oi_chg"]
    of = f["order_flow"]
    # 价值做多：taker买 + OI增；诈唬做多：taker买 + OI减
    taker_buy = taker >= 1.3
    taker_sell = taker <= 0.77
    oi_up = oi > 0
    oi_down = oi < 0
    zhuang_score = 0.0
    if taker_buy and oi_up:
        zhuang_score += 1.0   # 价值下注真多头
    elif taker_buy and oi_down:
        zhuang_score -= 0.5   # 诈唬拉高出货
    elif taker_sell and oi_up:
        zhuang_score -= 1.0   # 真空头
    elif taker_sell and oi_down:
        zhuang_score += 0.5   # 砸盘洗盘（警惕反弹）
    if of > 0.1:
        zhuang_score += 0.5
    elif of < -0.1:
        zhuang_score -= 0.5

    # === 视角② 散户反指 ===
    lsr = f["lsr"]
    sanhu_score = 0.0
    if lsr > 1.3:
        sanhu_score -= 0.5   # 散户拥挤做多→反指偏空
    elif lsr < 0.8:
        sanhu_score += 0.5   # 散户偏空→反指偏多

    # === 视角③ 历史数据 ===
    hist_score = 0.0
    if f["up_rate"] > 0.6 and f["n_hist"] >= 20:
        hist_score += 1.0
    elif f["up_rate"] < 0.4 and f["n_hist"] >= 20:
        hist_score -= 1.0

    # === 连庄（顶层方向判断器）===
    lz = f["consecutive"]
    lz_dir = f["dir_seq"][-1] if f["dir_seq"] else None
    lz_score = 0.0
    if lz_dir and lz >= 2:
        # 连庄顺势（但结合庄家剧本验证真假）
        if lz_dir == "UP":
            lz_score += 1.0
        else:
            lz_score -= 1.0
        # 庄家剧本验证：连庄但庄家派发=诱多
        if lz_dir == "UP" and taker_sell and oi_down:
            lz_score = -0.5  # 诱多，改反向
        if lz_dir == "DOWN" and taker_buy and oi_up:
            lz_score = 0.5

    # === 结构位（贴近支撑/压力不追单）===
    prev_close = f["prev_close"]
    hi, lo = f["hi24"], f["lo24"]
    # 距支撑/压力距离
    dist_to_lo = (prev_close - lo) / lo if lo else 0
    dist_to_hi = (hi - prev_close) / hi if hi else 0

    # === 加权合成 ===
    # 庄家40% 散户30% 历史30%，连庄作为修正
    total = zhuang_score * 0.4 + sanhu_score * 0.3 + hist_score * 0.3 + lz_score * 0.4

    reasons.append(f"庄家{zhuang_score:+.2f}(taker{f['taker']:.2f}/OI{oi:+.1f}%/of{of:+.2f})")
    reasons.append(f"散户{sanhu_score:+.2f}(lsr{lsr:.2f})")
    reasons.append(f"历史{hist_score:+.2f}(up_rate{f['up_rate']:.2f}/n{f['n_hist']:.0f})")
    reasons.append(f"连庄{lz_score:+.2f}(consecutive{lz}/dir{lz_dir})")

    # 贴近强支撑/压力 → 降置信
    conf_penalty = 0
    if dist_to_lo < 0.003:
        conf_penalty += 1
    if dist_to_hi < 0.003:
        conf_penalty += 1

    direction = "UP" if total > 0 else "DOWN"
    conf = min(0.65, 0.55 + abs(total) * 0.25)
    conf -= conf_penalty * 0.1

    return direction, conf, "; ".join(reasons)


def main() -> int:
    results = load_jsonl(RESULTS)
    # slug -> actual
    actual_map = {}
    for r in results:
        slug = r.get("slug", "")
        actual_map[slug] = r.get("actual")
    print(f"results: {len(results)} 场 | 有actual的slug: {len(actual_map)}")

    # 加载所有大快照（含 klines_1h）
    snaps = {}
    for f in sorted(SNAP_DIR.glob("*.json")):
        if f.stat().st_size < 40000:
            continue  # 只用含 klines_1h 的大快照
        d = load_snapshot(f)
        if not d:
            continue
        slug = d.get("slug", "")
        # 每个 slug 取最新一份快照
        if slug not in snaps:
            snaps[slug] = d
    print(f"大快照 slug 数: {len(snaps)}")

    # 关联：有快照且有结算
    matched = []
    for slug, d in snaps.items():
        if slug in actual_map:
            matched.append((slug, d, actual_map[slug]))
    print(f"可回测（快照+结算+有klines_1h）: {len(matched)}")

    # 回测
    rows = []
    for slug, d, actual in matched:
        f = extract_features(d)
        pred, conf, reason = judge(f)
        win = 1 if pred == actual else 0
        rows.append((slug, actual, pred, win, conf, reason))

    wins = sum(r[3] for r in rows)
    n = len(rows)
    if n == 0:
        print("无可回测样本")
        return 1
    lo, hi = wilson_ci(wins, n)
    print("\n=== 图表分析.md v1.4 引擎回测结果 ===")
    print(f"样本 {n} | 胜 {wins} | 胜率 {wins/n:.1%} | Wilson95% [{lo:.1%}, {hi:.1%}]")
    print(f"对比：1H市场历史天花板 ~63%")

    # 分桶：按置信度
    print("\n按置信度分桶:")
    for band in [(0.55, 0.6, "低置信"), (0.6, 0.63, "中置信"), (0.63, 1.01, "高置信")]:
        lo_b, hi_b, name = band
        sub = [r for r in rows if lo_b <= r[4] < hi_b]
        if sub:
            sw = sum(r[3] for r in sub)
            slo, shi = wilson_ci(sw, len(sub))
            print(f"  {name}: {sw}/{len(sub)} = {sw/len(sub):.1%} [{slo:.1%},{shi:.1%}]")

    # 输出明细到 md
    out = []
    out.append("# 图表分析.md v1.4 引擎离线回测")
    out.append("")
    out.append(f"样本 {n} | 胜 {wins} | 胜率 {wins/n:.1%} | Wilson95% [{lo:.1%}, {hi:.1%}]")
    out.append(f"对比：1H市场历史天花板 ~63%（240场实测）")
    out.append("")
    out.append("| slug | actual | pred | win | conf | 判定依据 |")
    out.append("|------|--------|------|-----|------|---------|")
    for slug, actual, pred, win, conf, reason in rows:
        out.append(f"| {slug} | {actual} | {pred} | {'✓' if win else '✗'} | {conf:.2f} | {reason} |")
    out.append("")
    report = DATA_DIR / f"shape_flow_backtest_{datetime.now().strftime('%Y%m%d')}.md"
    report.write_text("\n".join(out), encoding="utf-8")
    print(f"\n明细 → {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
