#!/usr/bin/env python3
"""反手预警触发场次深入对比（2026-08-14 用户质疑：锁低缩仓误伤正确场次，需分级）。

背景：round17 A 方案对触发场一律锁低+缩仓0.5，但 21 场触发中 18 场基线方向正确
（85.7%）——用户指出这是"误伤"：对大概率正确的场次也降置信缩仓。而 C 翻转
（全部翻转）又恶化 18 场。用户要求设计"满足什么条件才翻转"的分级机制。

本脚本输出 21 场触发场次的逐场详细特征（含 3 错 vs 18 对的对比），
目标是找出能分离"该翻转场次"与"不该动场次"的特征组合。

用法：python3 scripts/countertrend_deep_analysis.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "scripts")

from signal_validator import extract_factors, _SkipValidation, _f  # noqa: E402
from rule_engine import judge  # noqa: E402

ROOT = Path("/Users/tangyijun/Desktop/hermes btc")
RESULTS = ROOT / "data" / "hermes_results.jsonl"
SNAP = ROOT / "data" / "snapshots"
OUT = ROOT / "data" / "hermes_countertrend_deep_analysis_20260814.md"

HIST_BULL = 0.55
HIST_BEAR = 0.45
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


def countertrend_dims(md: dict, direction: str, conf: str, mode: str) -> dict:
    dims: dict[str, bool] = {}
    dev = _f(md.get("deviation")) or 0
    anchor = _f(md.get("anchor_price")) or 1
    dev_pct = dev / anchor * 100
    hist = md.get("historical") or {}
    up_rate = _f(hist.get("up_rate"))
    pm = md.get("polymarket") or {}
    pm_up = _f(pm.get("up_price"))
    tf4h = (md.get("timeframes") or {}).get("4H") or {}
    accel4 = _f(tf4h.get("macd_acceleration"))
    dtrend = md.get("deviation_trend") or {}

    tie_maintain = ("贴线" in (mode or "") and not dtrend.get("expanding"))
    weak_net = conf == "低"
    dims["weak_signal"] = tie_maintain or weak_net

    pm_side = None
    pm_dir = None
    if isinstance(pm_up, (int, float)):
        pm_side = max(pm_up * 100, (1 - pm_up) * 100) if pm_up > 0 else None
        pm_dir = "UP" if pm_up > 0.5 else ("DOWN" if pm_up < 0.5 else None)
    dims["pm_consensus"] = bool(
        pm_side is not None and 60 <= pm_side < 80 and pm_dir == direction
    )

    if isinstance(up_rate, (int, float)):
        hist_dir = "UP" if up_rate > HIST_BULL else ("DOWN" if up_rate < HIST_BEAR else None)
        dims["counter_hist"] = bool(hist_dir is not None and hist_dir != direction)
    else:
        dims["counter_hist"] = False

    if isinstance(accel4, (int, float)):
        dims["trend_exhaust"] = bool(
            (direction == "DOWN" and accel4 > 0) or (direction == "UP" and accel4 < 0)
        )
    else:
        dims["trend_exhaust"] = False

    return dims, {"dev_pct": dev_pct, "up_rate": up_rate, "pm_side": pm_side,
                  "pm_dir": pm_dir, "accel4": accel4}


def fmt_hour(slug: str) -> str:
    import re
    m = re.search(r"august-(\d+)-2026-(\d+)am-et", slug)
    if m:
        return f"{m.group(2)}am"
    m = re.search(r"august-(\d+)-2026-(\d+)pm-et", slug)
    if m:
        return f"{m.group(2)}pm"
    return slug[-20:]


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
        dims, meta = countertrend_dims(md, base_dir, d.confidence, mode)
        n_dims = sum(1 for v in dims.values() if v)
        if n_dims < MIN_DIMS:
            continue
        # 详细特征
        pp24 = md.get("price_position_24h") or {}
        pm = md.get("polymarket") or {}
        pm_up = _f(pm.get("up_price"))
        pm_side = meta["pm_side"]
        tf4h = (md.get("timeframes") or {}).get("4H") or {}
        tf1h = (md.get("timeframes") or {}).get("1H") or {}
        rows.append({
            "slug": slug, "hour": fmt_hour(slug), "act": act, "base": base_dir,
            "correct": base_dir == act, "dims": n_dims,
            "dims_detail": dims, "conf": d.confidence, "net": d.net, "s0": d.s0,
            "mode": mode, "dev_pct": meta["dev_pct"],
            "pm_side": pm_side, "pm_up": _f(pm_up),
            "up_rate": meta["up_rate"], "accel4": meta["accel4"],
            "accel1": _f(tf1h.get("macd_acceleration")),
            "hist4": _f(tf4h.get("macd_hist")),
            "pct24": _f(pp24.get("current_percentile")),
            "flips": (md.get("prev_hour") or {}).get("flips"),
            "vol": _f(md.get("volatility_5m")),
            "taker_bs": _f((md.get("capital_flow") or {}).get("taker_bs_ratio")),
            "consec": max(int(_f((md.get("prev_hour") or {}).get("consecutive_up")) or 0),
                         int(_f((md.get("prev_hour") or {}).get("consecutive_down")) or 0)),
            "n_samples": _f((md.get("historical") or {}).get("total_samples")),
        })

    rows.sort(key=lambda r: r["slug"])
    trig = rows
    n = len(trig)
    wrong = [r for r in trig if not r["correct"]]
    right = [r for r in trig if r["correct"]]

    def col(name, fmt="{v}", extra=""):
        def g(r):
            v = r[name]
            return fmt.format(v=v)
        return g

    lines = []
    lines.append("# 反手预警触发场次深入对比（2026-08-14）\n")
    lines.append(f"> 样本：触发 {n} 场（基线正确 {len(right)} 错误 {len(wrong)}）。"
                 f"目的：寻找能区分\"该翻转\"（{len(wrong)} 场）与\"不该动\"（{len(right)} 场）的特征。\n")
    lines.append("> 特征列：时段 / 实际 / 基线 / 维度(WS=弱信号,PM=顺共识,CT=反历史,TE=4H衰竭) / 置信 / 净分 / "
                 "模式 / 偏差% / PM侧% / 历史up_rate(n) / 4H accel / 1H accel / 24h位置% / flips / vol% / consec\n")

    lines.append("## 全部触发场次明细\n")
    lines.append("| 时段 | 实际 | 基线 | 对错 | 维 | WS PM CT TE | 置信 | 净分 | 模式 | 偏差% | PM侧% | up_rate | accel4 | accel1 | pos24% | flips | consec |")
    lines.append("|------|------|------|------|----|-------------|------|------|------|-------|--------|---------|--------|--------|--------|-------|--------|")
    for r in trig:
        ws = "●" if r["dims_detail"]["weak_signal"] else "·"
        pm = "●" if r["dims_detail"]["pm_consensus"] else "·"
        ct = "●" if r["dims_detail"]["counter_hist"] else "·"
        te = "●" if r["dims_detail"]["trend_exhaust"] else "·"
        mark = "❌" if not r["correct"] else "✅"
        lines.append(
            f"| {r['hour']} | {r['act']} | {r['base']} | {mark} | {r['dims']} | {ws} {pm} {ct} {te} | "
            f"{r['conf']} | {r['net']:+.1f} | {r['mode']} | {r['dev_pct']:+.3f} | "
            f"{r['pm_side']:.0f}% | {r['up_rate']:.3f} | {r['accel4']:+.1f} | {r['accel1']:+.1f} | "
            f"{r['pct24']:.0f}% | {r['flips']} | {r['consec']} |"
        )
    lines.append("")

    lines.append("## 错误场次（应翻转候选）\n")
    for r in wrong:
        lines.append(
            f"- **{r['slug']}**：预测 {r['base']} 实际 {r['act']}，{r['dims']}/4 维"
            f"（净分 {r['net']:+.1f}，模式 {r['mode']}，偏差 {r['dev_pct']:+.3f}%，"
            f"PM {r['pm_side']:.0f}%，up_rate {r['up_rate']:.3f}，accel4 {r['accel4']:+.1f}，"
            f"accel1 {r['accel1']:+.1f}，pos24 {r['pct24']:.0f}%，flips {r['flips']}，vol {r['vol']:.3f}%，consec {r['consec']})"
        )
    lines.append("")

    lines.append("## 正确场次（不应误伤）\n")
    for r in right:
        lines.append(
            f"- {r['slug']}：预测 {r['base']} 实际 {r['act']}，{r['dims']}/4 维"
            f"（净分 {r['net']:+.1f}，模式 {r['mode']}，偏差 {r['dev_pct']:+.3f}%，"
            f"PM {r['pm_side']:.0f}%，up_rate {r['up_rate']:.3f}，accel4 {r['accel4']:+.1f}，"
            f"accel1 {r['accel1']:+.1f}，pos24 {r['pct24']:.0f}%，flips {r['flips']}，vol {r['vol']:.3f}%，consec {r['consec']})"
        )
    lines.append("")

    # 特征交叉对比（错误 vs 正确）
    def avg(rs, key):
        vals = [r[key] for r in rs if isinstance(r[key], (int, float))]
        return sum(vals) / len(vals) if vals else None

    lines.append("## 特征对比（错误场 vs 正确场）\n")
    lines.append("| 特征 | 错误场(avg) | 正确场(avg) | 可分离? |")
    lines.append("|------|------------|------------|---------|")
    pairs = [
        ("dims", "维度数"),
        ("net", "净分(绝对值)"),
        ("dev_pct", "|偏差|%"),
        ("pm_side", "PM侧%"),
        ("up_rate", "历史up_rate"),
        ("accel4", "4H accel"),
        ("accel1", "1H accel"),
        ("hist4", "4H hist"),
        ("pct24", "24h位置%"),
        ("vol", "波动率%"),
        ("consec", "连续同向"),
    ]
    for key, label in pairs:
        wa = avg(wrong, key)
        ra = avg(right, key)
        if wa is None or ra is None:
            lines.append(f"| {label} | {wa} | {ra} | - |")
            continue
        if key == "net":
            wa = avg([dict(r, net=abs(r["net"])) for r in wrong], "net")
            ra = avg([dict(r, net=abs(r["net"])) for r in right], "net")
        sep = "✓" if abs((wa or 0) - (ra or 0)) > 0.5 * max(1, (ra or 0)) else "✗"
        lines.append(f"| {label} | {wa if wa is None else f'{wa:.3f}'} | {ra if ra is None else f'{ra:.3f}'} | {sep} |")
    lines.append("")

    # 错误场共同点
    lines.append("## 错误场共同点分析\n")
    wrong_dir = [r["base"] for r in wrong]
    lines.append(f"- 错误场预测方向：{wrong_dir}（全部为 DOWN）")
    lines.append(f"- 正确场预测方向分布：DOWN {sum(1 for r in right if r['base']=='DOWN')} 场 / "
                 f"UP {sum(1 for r in right if r['base']=='UP')} 场")
    wrong_hours = [r["hour"] for r in wrong]
    lines.append(f"- 错误场时段：{wrong_hours}（深夜至凌晨）")
    right_hours = [r["hour"] for r in right]
    lines.append(f"- 正确场时段：{right_hours}")
    lines.append("")

    # 候选分离特征统计
    lines.append("## 候选分离特征统计（关键）\n")
    # 特征A：预测 DOWN + 深夜时段（12am-4am）
    def late_night(r):
        h = r["hour"]
        if h.endswith("pm"):
            hh = int(h[:-2])
            return hh >= 10
        hh = int(h[:-2])
        return hh <= 4
    dn_late = [r for r in trig if r["base"] == "DOWN" and late_night(r)]
    lines.append(f"- **候选A：预测 DOWN + 深夜时段(22pm-4am)**：触发 {len(dn_late)} 场（错误 {sum(1 for r in dn_late if not r['correct'])} / 正确 {sum(1 for r in dn_late if r['correct'])}）")
    # 特征B：4维全命中
    full4 = [r for r in trig if r["dims"] == 4]
    lines.append(f"- **候选B：4维全命中**：触发 {len(full4)} 场（错误 {sum(1 for r in full4 if not r['correct'])} / 正确 {sum(1 for r in full4 if r['correct'])}）")
    # 特征C：预测 DOWN + 4维全命中
    dn4 = [r for r in trig if r["base"] == "DOWN" and r["dims"] == 4]
    lines.append(f"- **候选C：预测 DOWN + 4维全命中**：触发 {len(dn4)} 场（错误 {sum(1 for r in dn4 if not r['correct'])} / 正确 {sum(1 for r in dn4 if r['correct'])}）")
    # 特征D：顺共识 PM 偏强（>=65%）
    pm_strong = [r for r in trig if r["pm_side"] is not None and r["pm_side"] >= 65]
    lines.append(f"- **候选D：PM顺共识≥65%**：触发 {len(pm_strong)} 场（错误 {sum(1 for r in pm_strong if not r['correct'])} / 正确 {sum(1 for r in pm_strong if r['correct'])}）")
    # 特征E：反向历史强（up_rate 极端偏离）
    hist_ext = [r for r in trig if r["up_rate"] is not None and abs(r["up_rate"] - 0.5) >= 0.15]
    lines.append(f"- **候选E：|up_rate-0.5|≥0.15**：触发 {len(hist_ext)} 场（错误 {sum(1 for r in hist_ext if not r['correct'])} / 正确 {sum(1 for r in hist_ext if r['correct'])}）")
    # 特征F：4H 衰竭强（accel4 反向偏离）
    exhaust_strong = [r for r in trig if r["accel4"] is not None and abs(r["accel4"]) >= 3]
    lines.append(f"- **候选F：|4H accel|≥3**：触发 {len(exhaust_strong)} 场（错误 {sum(1 for r in exhaust_strong if not r['correct'])} / 正确 {sum(1 for r in exhaust_strong if r['correct'])}）")
    # 特征G：错误场 = DOWN + (深夜 或 4维)
    candG = [r for r in trig if r["base"] == "DOWN" and (late_night(r) or r["dims"] == 4)]
    lines.append(f"- **候选G：预测 DOWN + (深夜时段 或 4维全命中)**：触发 {len(candG)} 场（错误 {sum(1 for r in candG if not r['correct'])} / 正确 {sum(1 for r in candG if r['correct'])}）")
    # 特征H：pm_side 顺共识 + 弱信号 + 预测DOWN
    candH = [r for r in trig if r["base"] == "DOWN" and r["dims_detail"]["weak_signal"] and r["dims_detail"]["pm_consensus"] and (r["dims_detail"]["counter_hist"] or r["dims_detail"]["trend_exhaust"])]
    lines.append(f"- **候选H：预测 DOWN + 弱信号 + 顺共识 + (反历史或4H衰竭)**：触发 {len(candH)} 场（错误 {sum(1 for r in candH if not r['correct'])} / 正确 {sum(1 for r in candH if r['correct'])}）")

    OUT.write_text("\n".join(lines) + "\n")
    print(f"已生成 {OUT}")
    print(f"触发 {n} 场：正确 {len(right)} 错误 {len(wrong)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
