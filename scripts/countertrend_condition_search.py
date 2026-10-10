#!/usr/bin/env python3
"""反手预警条件式翻转的网格搜索（2026-08-14）。

用户质疑：① 不应全部翻转，应"达到某条件才翻转"；② 锁低+缩仓0.5 误伤了
18 场判断正确的场次。本脚本在 21 场触发场次上系统性枚举单特征/组合特征阈值，
寻找"翻转该子集后净改善最大"的条件，同时量化误伤。

搜索口径：候选翻转子集 S ⊂ 触发场。若对 S 内场次翻转方向：
- 改善 = S 内基线错误（翻转后正确）的场次数
- 恶化 = S 内基线正确（翻转后错误）的场次数
净效果 = 改善 - 恶化。目标：净效果 > 0 且误伤尽量少。

输出：所有候选条件的净效果排序，供设计分级机制参考。
用法：python3 scripts/countertrend_condition_search.py
"""
from __future__ import annotations

import json
import itertools
import re
import sys
from pathlib import Path

sys.path.insert(0, "scripts")

from signal_validator import extract_factors, _SkipValidation, _f  # noqa: E402
from rule_engine import judge  # noqa: E402

ROOT = Path("/Users/tangyijun/Desktop/hermes btc")
RESULTS = ROOT / "data" / "hermes_results.jsonl"
SNAP = ROOT / "data" / "snapshots"
OUT = ROOT / "data" / "hermes_countertrend_condition_search_20260814.md"

MIN_DIMS = 3


def actual_map() -> dict[str, str]:
    m: dict[str, str] = {}
    for line in RESULTS.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        slug = rec.get("slug")
        act = rec.get("actual")
        if slug and act in ("UP", "DOWN"):
            m.setdefault(slug, act)
    return m


def load_latest_snapshot(slug: str) -> dict | None:
    if not SNAP.glob(f"{slug}_*.json"):
        return None
    cands = sorted(SNAP.glob(f"{slug}_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in cands:
        try:
            md = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if md.get("slug") == slug:
            return md
    return None


def fmt_hour(slug: str) -> str:
    m = re.search(r"august-(\d+)-2026-(\d+)(am|pm)-et", slug)
    if m:
        return f"{m.group(2)}{m.group(3)}"
    return slug[-20:]


def gather(md: dict, direction: str, conf: str, mode: str) -> dict:
    """提取触发场次全部可机算特征 + 4维命中。"""
    dev = _f(md.get("deviation")) or 0
    anchor = _f(md.get("anchor_price")) or 1
    dev_pct = dev / anchor * 100
    hist = md.get("historical") or {}
    up_rate = _f(hist.get("up_rate"))
    pm = md.get("polymarket") or {}
    pm_up = _f(pm.get("up_price"))
    tf1m = (md.get("timeframes") or {}).get("1m") or {}
    tf5m = (md.get("timeframes") or {}).get("5m") or {}
    tf15m = (md.get("timeframes") or {}).get("15m") or {}
    tf1h = (md.get("timeframes") or {}).get("1H") or {}
    tf4h = (md.get("timeframes") or {}).get("4H") or {}
    cap = md.get("capital_flow") or {}
    pp24 = md.get("price_position_24h") or {}
    prev = md.get("prev_hour") or {}
    dtrend = md.get("deviation_trend") or {}

    pm_side = None
    pm_dir = None
    if isinstance(pm_up, (int, float)):
        pm_side = max(pm_up * 100, (1 - pm_up) * 100) if pm_up > 0 else None
        pm_dir = "UP" if pm_up > 0.5 else ("DOWN" if pm_up < 0.5 else None)

    dims = {
        "weak_signal": ("贴线" in mode and not dtrend.get("expanding")) or conf == "低",
        "pm_consensus": bool(pm_side is not None and 60 <= pm_side < 80 and pm_dir == direction),
        "counter_hist": bool(
            isinstance(up_rate, (int, float))
            and ((up_rate > 0.55 and direction == "DOWN") or (up_rate < 0.45 and direction == "UP"))
        ),
        "trend_exhaust": bool(
            isinstance(_f(tf4h.get("macd_acceleration")), (int, float))
            and ((direction == "DOWN" and (_f(tf4h.get("macd_acceleration")) or 0) > 0)
                 or (direction == "UP" and (_f(tf4h.get("macd_acceleration")) or 0) < 0))
        ),
    }

    # 全部候选特征（数值）
    accel4 = _f(tf4h.get("macd_acceleration"))
    accel1 = _f(tf1h.get("macd_acceleration"))
    hist4 = _f(tf4h.get("macd_hist"))
    hist1 = _f(tf1h.get("macd_hist"))
    feat = {
        "dims": sum(1 for v in dims.values() if v),
        "n_dev": abs(dev_pct),
        "pm_side": pm_side,
        "up_rate": up_rate,
        "accel4": accel4,
        "accel1": accel1,
        "hist4": hist4,
        "hist1": hist1,
        "pos24": _f(pp24.get("current_percentile")),
        "vol": _f(md.get("volatility_5m")),
        "consec": max(int(_f(prev.get("consecutive_up")) or 0), int(_f(prev.get("consecutive_down")) or 0)),
        "flips": _f(prev.get("flips")),
        "taker_bs": _f(cap.get("taker_bs_ratio")),
        "funding": _f(cap.get("funding_rate")),
        "long_short": _f(cap.get("long_short_ratio")),
        "macd15_hist": _f(tf15m.get("macd_hist")),
        "rsi15": _f(tf15m.get("rsi")),
        "rsi1": _f(tf1m.get("rsi")),
        "bb_pos1": _f(tf1m.get("bb_pos")),
        "recent_dir1": tf1m.get("recent_dir"),
        "recent_dir5": tf5m.get("recent_dir"),
        "recent_dir4h": tf4h.get("recent_dir"),
        "vol_ratio1": _f(tf1m.get("vol_ratio")),
        "v_reversal": bool((md.get("v_reversal_15m") or {}).get("detected")),
        "open_dir": (md.get("opening") or {}).get("dir"),
        "tie_mode": "贴线" in mode,
        "is_down": direction == "DOWN",
        "is_up": direction == "UP",
    }
    return dims, feat


def eval_flip(rows, sel_fn) -> tuple[int, int, list, list]:
    """对满足 sel_fn 的场次翻转，返回 (改善, 恶化, 改善场, 恶化场)。"""
    imp = []
    wor = []
    for r in rows:
        if sel_fn(r["feat"]):
            if r["base"] == r["act"]:
                wor.append(r)
            else:
                imp.append(r)
    return len(imp), len(wor), imp, wor


def main() -> int:
    amap = actual_map()
    amap["bitcoin-up-or-down-august-14-2026-1am-et"] = "UP"
    amap.setdefault("bitcoin-up-or-down-august-13-2026-9pm-et", "UP")

    rows = []
    for slug, act in amap.items():
        md = load_latest_snapshot(slug)
        if not md:
            continue
        try:
            factors, inp, notes, mode = extract_factors(md)
        except _SkipValidation:
            continue
        d = judge(inp)
        base_dir = d.final_direction
        if not base_dir:
            continue
        dims, feat = gather(md, base_dir, d.confidence, mode)
        if feat["dims"] < MIN_DIMS:
            continue
        for k, v in dims.items():
            feat[f"_{k}"] = v
        rows.append({
            "slug": slug, "hour": fmt_hour(slug), "act": act, "base": base_dir,
            "correct": base_dir == act, "dims_detail": dims, "feat": feat,
        })

    n = len(rows)
    wrong = [r for r in rows if not r["correct"]]
    right = [r for r in rows if r["correct"]]

    results = []

    def add(name, sel_fn):
        imp, wor_n, imp_r, wor_r = eval_flip(rows, sel_fn)
        results.append((name, imp, wor_n, imp - wor_n, len(imp_r) + len(wor_r),
                        [r["hour"] for r in imp_r], [r["hour"] for r in wor_r]))

    # ===== 单特征条件 =====
    # 预测方向
    add("预测 DOWN", lambda f: f["is_down"])
    add("预测 UP", lambda f: f["is_up"])
    # 维度数
    add("4维全命中", lambda f: f["dims"] == 4)
    add("3维命中", lambda f: f["dims"] == 3)
    # 贴线模式
    add("贴线模式", lambda f: f["tie_mode"])
    add("贴线模式+预测DOWN", lambda f: f["tie_mode"] and f["is_down"])
    # 深夜时段（22pm-4am）
    def late_night(f):
        h = f["_hour"]
        return ("pm" in h and int(h[:-2]) >= 10) or ("am" in h and int(h[:-2]) <= 4)
    for r in rows:
        r["feat"]["_hour"] = r["hour"]
    add("深夜时段(22pm-4am)", late_night)
    add("深夜+预测DOWN", lambda f: late_night(f) and f["is_down"])
    # accel4 阈值
    for t in (5, 10, 15, 20, 25):
        add(f"accel4≥{t}", lambda f, t=t: f["accel4"] is not None and f["accel4"] >= t)
        add(f"accel4≥{t}+预测DOWN", lambda f, t=t: f["accel4"] is not None and f["accel4"] >= t and f["is_down"])
    # accel1 阈值
    for t in (0, 3, 5, 10):
        add(f"accel1≥{t}", lambda f, t=t: f["accel1"] is not None and f["accel1"] >= t)
        add(f"accel1≥{t}+预测DOWN", lambda f, t=t: f["accel1"] is not None and f["accel1"] >= t and f["is_down"])
    # |hist4| 阈值
    for t in (30, 50, 80, 100):
        add(f"|hist4|<{t}", lambda f, t=t: f["hist4"] is not None and abs(f["hist4"]) < t)
        add(f"|hist4|<{t}+预测DOWN", lambda f, t=t: f["hist4"] is not None and abs(f["hist4"]) < t and f["is_down"])
    # pm_side 阈值
    for t in (65, 70, 75):
        add(f"pm_side≥{t}", lambda f, t=t: f["pm_side"] is not None and f["pm_side"] >= t)
        add(f"pm_side≥{t}+预测DOWN", lambda f, t=t: f["pm_side"] is not None and f["pm_side"] >= t and f["is_down"])
    # 反向历史
    add("反向历史命中", lambda f: f["_counter_hist"])
    add("反向历史+预测DOWN", lambda f: f["is_down"] and f["_counter_hist"])
    # 位置
    add("pos24∈[20,50]", lambda f: f["pos24"] is not None and 20 <= f["pos24"] <= 50)
    add("pos24∈[20,50]+预测DOWN", lambda f: f["pos24"] is not None and 20 <= f["pos24"] <= 50 and f["is_down"])

    # ===== 组合条件 =====
    add("预测DOWN+贴线+accel1≥0", lambda f: f["is_down"] and f["tie_mode"] and f["accel1"] is not None and f["accel1"] >= 0)
    add("预测DOWN+贴线+accel4≥5", lambda f: f["is_down"] and f["tie_mode"] and f["accel4"] is not None and f["accel4"] >= 5)
    add("预测DOWN+贴线+|hist4|<50", lambda f: f["is_down"] and f["tie_mode"] and f["hist4"] is not None and abs(f["hist4"]) < 50)
    add("预测DOWN+贴线+深夜", lambda f: f["is_down"] and f["tie_mode"] and late_night(f))
    add("预测DOWN+贴线+反历史", lambda f: f["is_down"] and f["tie_mode"] and f["_counter_hist"])
    add("预测DOWN+反历史+accel1≥0", lambda f: f["is_down"] and f["_counter_hist"] and f["accel1"] is not None and f["accel1"] >= 0)
    add("预测DOWN+4维", lambda f: f["is_down"] and f["dims"] == 4)
    add("预测DOWN+4维+贴线", lambda f: f["is_down"] and f["dims"] == 4 and f["tie_mode"])
    add("预测DOWN+4维+深夜", lambda f: f["is_down"] and f["dims"] == 4 and late_night(f))
    add("预测DOWN+弱信号+顺共识+反历史", lambda f: f["is_down"] and f["_weak_signal"] and f["_pm_consensus"] and f["_counter_hist"])
    add("预测DOWN+弱信号+顺共识+4H衰竭", lambda f: f["is_down"] and f["_weak_signal"] and f["_pm_consensus"] and f["_trend_exhaust"])

    # ===== 排序输出 =====
    results.sort(key=lambda x: x[3], reverse=True)
    lines = []
    lines.append("# 反手预警条件式翻转网格搜索（2026-08-14）\n")
    lines.append(f"> 样本：触发 {n} 场（基线正确 {len(right)} 错误 {len(wrong)}）。"
                 f"候选条件对子集翻转方向后的（改善-恶化）。\n")
    lines.append("> 改善 = 该子集内基线错误→翻转后正确；恶化 = 基线正确→翻转后错误。"
                 "净效果 > 0 才有翻转价值。\n")
    lines.append("## 条件搜索结果（按净效果排序）\n")
    lines.append("| 条件 | 改善 | 恶化 | 净效果 | 覆盖场次 | 改善场(时段) | 恶化场(时段) |")
    lines.append("|------|------|------|--------|----------|-------------|-------------|")
    for name, imp, wor, net, cov, imp_h, wor_h in results:
        lines.append(f"| {name} | {imp} | {wor} | {net:+d} | {cov} | {','.join(imp_h) or '-'} | {','.join(wor_h) or '-'} |")
    lines.append("")
    lines.append("## 错误场全特征（3 场）\n")
    for r in wrong:
        f = r["feat"]
        lines.append(
            f"- {r['slug']}：预测{r['base']}实际{r['act']} | 4维={f['dims']} | 贴线={f['tie_mode']} | "
            f"accel4={f['accel4']} accel1={f['accel1']} hist4={f['hist4']} | pm={f['pm_side']}% | "
            f"up_rate={f['up_rate']} | pos24={f['pos24']}% | 深夜={late_night(f)}"
        )

    OUT.write_text("\n".join(lines) + "\n")
    print(f"已生成 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
