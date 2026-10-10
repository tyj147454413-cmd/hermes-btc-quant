#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hermes BTC 1H 因子归因统计脚本（round41.8.8 配套）。

join hermes_results.jsonl（slug/side/actual/pnl）× data/snapshots/*.json（引擎 extract_factors 机算因子），
逐因子比对「因子方向」vs「实际结算方向」，输出：
  ① 全量因子命中率表（per-factor hit rate，含全部引擎因子，不再硬编码）
  ② 条件切片：PM 共识 × 波动率 × 24h 位置（找「什么时候该因子最可靠/最不可靠」）
  ③ 多数投票 + S0 净分正确率（模型基线对照）

运行：python3 scripts/factor_attribution.py [--min-samples N]
输出：data/hermes_factor_stats.json（机器可读全量）+ 控制台摘要。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from signal_validator import extract_factors  # noqa: E402

DATA = ROOT / "data"
RESULTS_FILE = DATA / "hermes_results.jsonl"
SNAPSHOT_DIR = DATA / "snapshots"

FACTOR_LABELS = {
    "price_position": "价格位置(vs锚定)",
    "taker_bs": "Taker B/S 分级",
    "taker_1h": "1H Taker B/S",
    "order_flow": "订单流 5m/15m",
    "macd_15m": "15m MACD hist",
    "kline": "K线序列(legacy混计,round44前)",
    "kline_15m": "K线序列 15m窗(round44拆分)",
    "kline_1h": "K线序列 1H窗(round44拆分)",
    "momentum_burst": "momentum_burst 放量顺势",
    "sr_proximity": "sr_proximity 阻力支撑邻近",
    "macro_trend": "宏观趋势族 1H4H",
    "seasonality": "历史季节性",
    "divergence": "背离",
    "fake_breakout": "假突破",
    "short_bounce_factor": "短周期反弹先兆",
    "opening_micro": "开盘微观方向",
    "wall_divergence": "订单簿墙背离",
    "taker_trend": "Taker 趋势",
    "taker_1m": "1m taker 趋势",
}


def sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def slug_of_snapshot(filename: str) -> str:
    """bitcoin-up-or-down-august-24-2026-9am-et_20260824_211602.json → slug"""
    return re.sub(r"_\d{8}_\d{6}\.json$", "", filename)


def load_results() -> dict[str, dict]:
    """slug → {side, actual, pnl}（actual ∈ {UP,DOWN} 且有 pnl 才纳入）"""
    results: dict[str, dict] = {}
    if not RESULTS_FILE.exists():
        return results
    for line in RESULTS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("actual") in ("UP", "DOWN") and r.get("pnl") is not None:
            results[r["slug"]] = r
    return results


def load_latest_snapshot_map() -> dict[str, Path]:
    """slug → 最新快照路径（按文件名时间戳排序，去重）"""
    m: dict[str, Path] = {}
    for p in sorted(SNAPSHOT_DIR.glob("*.json")):
        slug = slug_of_snapshot(p.name)
        # 文件名含时间戳，字符串序 == 时间序（同 slug 取最后=最新）
        m[slug] = p
    return m


def hit(factor_val: float, actual: str) -> str:
    """correct / wrong / neutral（+1=UP，-1=DOWN，0=中性）"""
    if factor_val is None or factor_val == 0:
        return "neutral"
    predicts_up = factor_val > 0
    if (predicts_up and actual == "UP") or (not predicts_up and actual == "DOWN"):
        return "correct"
    return "wrong"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-samples", type=int, default=5, help="条件切片最少样本数")
    args = ap.parse_args()

    results = load_results()
    snap_map = load_latest_snapshot_map()

    factor_stat: dict[str, dict[str, int]] = defaultdict(lambda: {"correct": 0, "wrong": 0, "neutral": 0})
    cond_pm: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    cond_vol: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    cond_pct: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    # round41.8.9 全面统计新增维度：
    dir_stat: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))  # 看多/看空 → [对, 错]
    win_stat: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))  # 模型赢/输单 → [对, 错]
    contrib: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])  # [对的贡献分, 错的贡献分]（|因子值|）

    model_correct = model_total = joined = skipped = 0
    vote_correct = vote_total = 0
    s0_correct = s0_total = 0

    for slug, r in results.items():
        actual = r["actual"]
        side = r.get("side")
        if slug not in snap_map:
            skipped += 1
            continue
        try:
            md = json.loads(snap_map[slug].read_text())
            factors, inp, notes, mode = extract_factors(md)
        except Exception:
            skipped += 1
            continue
        joined += 1

        # 模型整体方向正确率（side vs actual）
        if side in ("UP", "DOWN"):
            model_total += 1
            model_correct += int(side == actual)

        # 多数投票 + S0 正确率
        pos = sum(1 for v in factors.values() if v > 0)
        neg = sum(1 for v in factors.values() if v < 0)
        vote_total += 1
        vote_correct += int(sign(pos - neg) == sign(1 if actual == "UP" else -1))
        s0_total += 1
        s0_correct += int(sign(sum(factors.values())) == sign(1 if actual == "UP" else -1))

        # 条件切片
        pm = md.get("polymarket") or {}
        pm_up, pm_down = pm.get("up_price"), pm.get("down_price")
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

        for fname, fval in factors.items():
            h = hit(fval, actual)
            if h == "correct":
                factor_stat[fname]["correct"] += 1
                cond_pm[fname][pm_bucket][0] += 1
                cond_vol[fname][vol_bucket][0] += 1
                cond_pct[fname][pct_bucket][0] += 1
                contrib[fname][0] += abs(fval)
            elif h == "wrong":
                factor_stat[fname]["wrong"] += 1
                cond_pm[fname][pm_bucket][1] += 1
                cond_vol[fname][vol_bucket][1] += 1
                cond_pct[fname][pct_bucket][1] += 1
                contrib[fname][1] += abs(fval)
            else:
                factor_stat[fname]["neutral"] += 1
            # 看多/看空拆分
            if fval > 0:
                dir_stat[fname]["看多"][0 if h == "correct" else 1] += 1
            elif fval < 0:
                dir_stat[fname]["看空"][0 if h == "correct" else 1] += 1
            # 赢单/输单拆分（模型 side vs actual）
            if side in ("UP", "DOWN") and h != "neutral":
                win_stat[fname]["赢单" if side == actual else "输单"][0 if h == "correct" else 1] += 1

    # 汇总
    stats = {
        "generated_at": datetime.now().isoformat(),
        "total_results": len(results),
        "joined": joined,
        "skipped": skipped,
        "model_accuracy": (model_correct / model_total) if model_total else None,
        "model_total": model_total,
        "vote_accuracy": (vote_correct / vote_total) if vote_total else None,
        "s0_accuracy": (s0_correct / s0_total) if s0_total else None,
        "factors": {},
        "conditional": {"pm": {}, "vol": {}, "pct": {}},
    }

    factor_rows = []
    for fname, st in factor_stat.items():
        n = st["correct"] + st["wrong"]
        rate = st["correct"] / n if n else None
        factor_rows.append((fname, FACTOR_LABELS.get(fname, fname), st["correct"], st["wrong"], st["neutral"], rate, n))
        stats["factors"][fname] = {
            "label": FACTOR_LABELS.get(fname, fname),
            "correct": st["correct"], "wrong": st["wrong"], "neutral": st["neutral"],
            "hit_rate": rate, "n": n,
        }
        # 看多/看空拆分
        dir_rows = {}
        for dkey, dcw in dir_stat[fname].items():
            dn = dcw[0] + dcw[1]
            if dn >= args.min_samples:
                dir_rows[dkey] = {"correct": dcw[0], "wrong": dcw[1], "hit_rate": dcw[0] / dn, "n": dn}
        if dir_rows:
            stats["factors"][fname]["by_direction"] = dir_rows
        # 赢单/输单拆分
        win_rows = {}
        for wkey, wcw in win_stat[fname].items():
            wn = wcw[0] + wcw[1]
            if wn >= args.min_samples:
                win_rows[wkey] = {"correct": wcw[0], "wrong": wcw[1], "hit_rate": wcw[0] / wn, "n": wn}
        if win_rows:
            stats["factors"][fname]["by_model_result"] = win_rows
        # 贡献度
        if n:
            stats["factors"][fname]["contribution"] = {
                "correct_pts": round(contrib[fname][0], 1),
                "wrong_pts": round(contrib[fname][1], 1),
                "net_pts": round(contrib[fname][0] - contrib[fname][1], 1),
            }

    def build_cond(cond):
        out = {}
        for fname, buckets in cond.items():
            for bucket, (c, w) in buckets.items():
                n = c + w
                if n < args.min_samples:
                    continue
                out.setdefault(fname, {})[bucket] = {"correct": c, "wrong": w, "hit_rate": c / n, "n": n}
        return out

    stats["conditional"]["pm"] = build_cond(cond_pm)
    stats["conditional"]["vol"] = build_cond(cond_vol)
    stats["conditional"]["pct"] = build_cond(cond_pct)

    (DATA / "hermes_factor_stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2))

    # 控制台摘要
    print(f"=== 因子归因统计（join {joined}/{len(results)}，跳过 {skipped}）===")
    if model_total:
        print(f"模型方向正确率: {model_correct}/{model_total} = {model_correct/model_total*100:.1f}%")
    if vote_total:
        print(f"因子多数投票正确率: {vote_correct}/{vote_total} = {vote_correct/vote_total*100:.1f}%")
    if s0_total:
        print(f"S0 净分方向正确率: {s0_correct}/{s0_total} = {s0_correct/s0_total*100:.1f}%")
    print()
    print(f"{'因子':<26} {'命中':>8} {'样本':>6} {'命中率':>8} {'看多':>8} {'看空':>8} {'净贡献':>8}")
    print("-" * 76)
    for fname, label, c, w, neu, rate, n in sorted(factor_rows, key=lambda x: -(x[6] or 0)):
        if rate is None:
            continue
        flag = "" if rate >= 0.55 else (" ⚠️" if rate < 0.45 else "")
        # 看多/看空
        d = stats["factors"].get(fname, {}).get("by_direction", {})
        bull = f"{d['看多']['hit_rate']*100:.0f}%" if "看多" in d else "—"
        bear = f"{d['看空']['hit_rate']*100:.0f}%" if "看空" in d else "—"
        ct = stats["factors"].get(fname, {}).get("contribution", {})
        net = f"{ct['net_pts']:+.0f}" if ct else "—"
        print(f"{label:<26} {c:>4}/{n:<5} {n:>6} {rate*100:>7.1f}%{flag} {bull:>8} {bear:>8} {net:>8}")
    print()
    print(f"=== 条件切片（n≥{args.min_samples} 且偏离整体 >8pt）===")
    for dim, dimname in (("pm", "PM共识"), ("vol", "波动率"), ("pct", "24h位置")):
        print(f"--- {dimname} ---")
        found = False
        for fname, buckets in stats["conditional"][dim].items():
            base = stats["factors"].get(fname, {}).get("hit_rate")
            if base is None:
                continue
            label = FACTOR_LABELS.get(fname, fname)
            for bucket, b in buckets.items():
                dev = b["hit_rate"] - base
                if abs(dev) > 0.08:
                    found = True
                    print(f"  {label} @ {bucket}: {b['hit_rate']*100:.1f}% (整体 {base*100:.1f}%, Δ{dev*100:+.1f}pt, n={b['n']})")
        if not found:
            print("  （无显著偏离）")
    print()
    print("结果已写: data/hermes_factor_stats.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
