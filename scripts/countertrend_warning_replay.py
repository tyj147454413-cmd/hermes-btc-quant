#!/usr/bin/env python3
"""反手预警模式回测重放验证（2026-08-14）。

背景：8/14 1AM ET 与 8/13 9PM 同型错误——贴线/弱信号 + G3 顺 PM 共识
+ 反向历史时段 + 4H 衰竭 → 被庄家反转收割（预测 DOWN 实际 UP）。

本脚本回答核心问题：**如果把"反手预警"固化为规则，会不会误判其他场次？**
用 hermes_results.jsonl（权威结算）+ data/snapshots（下单时点快照）重放全场次：

1. 用 extract_factors(md) + judge(inp) 机算基线方向（round16 现行规则）
2. 从快照检测"反手预警"组合维度是否触发
3. 对触发场次，分别用三种反应强度做后处理：
   - A 只缩仓+锁低（方向不变，force scale=0.5 + 置信度锁低）
   - B 阻止顺共识锁低（若方向由 G3 顺共识覆盖而来则回退原始净分方向）
   - C 翻转方向（预测方向取反）
4. 对比基线 vs A/B/C 的方向正确率，量化改善/恶化场次

判定标准（决定落地反应强度）：
- 反手预警触发场次中"反方向胜率"显著 > 基线 → 可考虑更高强度
- 若改判会误判（恶化）较多场次 → 落到保守 A（只缩仓不翻向）

用法：python3 scripts/countertrend_warning_replay.py
"""
from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, "scripts")

from signal_validator import extract_factors, _SkipValidation, _f  # noqa: E402
from rule_engine import judge  # noqa: E402

ROOT = Path("/Users/tangyijun/Desktop/hermes btc")
RESULTS = ROOT / "data" / "hermes_results.jsonl"
SNAP = ROOT / "data" / "snapshots"
OUT = ROOT / "data" / "hermes_countertrend_warning_replay_20260814.md"

# 反手预警维度阈值（可调）
HIST_BULL = 0.55   # up_rate > 0.55 视为时段偏多（predict DOWN = 反向）
HIST_BEAR = 0.45   # up_rate < 0.45 视为时段偏空（predict UP = 反向）
PM_CONS_LO = 60    # PM 顺共识区下界 %
PM_CONS_HI = 80    # PM 顺共识区上界 %
MIN_DIMS = 3       # 反手预警触发所需最小维度数（1AM/9PM 均命中 4 维）


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
    """取该 slug 最新的快照（下单时点，signal_validator 同口径）。"""
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
    """检测反手预警组合维度（独立维度各自布尔）。

    返回 {weak_signal, pm_consensus, counter_hist, trend_exhaust} 及各自说明。
    direction = 预测/基线方向；conf = 引擎置信度；mode = 贴线/常规模式。
    """
    dims: dict[str, bool] = {}
    notes: dict[str, str] = {}

    dev = _f(md.get("deviation")) or 0
    dev_pct = dev / (_f(md.get("anchor_price")) or 1) * 100
    hist = (md.get("historical") or {})
    up_rate = _f(hist.get("up_rate"))
    pm = md.get("polymarket") or {}
    pm_up = _f(pm.get("up_price"))
    tf4h = (md.get("timeframes") or {}).get("4H") or {}
    accel4 = _f(tf4h.get("macd_acceleration"))
    dtrend = md.get("deviation_trend") or {}

    # 维度1：弱信号/贴线 —— 贴线维持 或 低置信 或 |偏差| 贴近锚
    tie_maintain = ("贴线" in (mode or "") and not dtrend.get("expanding"))
    weak_net = conf == "低"
    dims["weak_signal"] = tie_maintain or weak_net
    notes["weak_signal"] = f"mode={mode} conf={conf} tie_maintain={tie_maintain}"

    # 维度2：PM 顺共识（60-80% 且与预测方向同向）
    pm_side = None
    pm_dir = None
    if isinstance(pm_up, (int, float)):
        pm_side = max(pm_up * 100, (1 - pm_up) * 100) if pm_up > 0 else None
        pm_dir = "UP" if pm_up > 0.5 else ("DOWN" if pm_up < 0.5 else None)
    dims["pm_consensus"] = bool(
        pm_side is not None and PM_CONS_LO <= pm_side < PM_CONS_HI and pm_dir == direction
    )
    notes["pm_consensus"] = f"PM {pm_dir} {pm_side if pm_side is not None else 'N/A'}% 顺{direction}"

    # 维度3：反向历史时段 —— 预测方向与 historical.up_rate 主倾向相反
    if isinstance(up_rate, (int, float)):
        hist_dir = "UP" if up_rate > HIST_BULL else ("DOWN" if up_rate < HIST_BEAR else None)
        dims["counter_hist"] = bool(hist_dir is not None and hist_dir != direction)
        notes["counter_hist"] = f"up_rate={up_rate} 时段主倾向={hist_dir} 预测={direction} 反向={dims['counter_hist']}"
    else:
        dims["counter_hist"] = False
        notes["counter_hist"] = "up_rate 缺失"

    # 维度4：4H 衰竭反向 —— 预测 DOWN 但 accel>0（跌势衰竭→反弹）；预测 UP 但 accel<0（动能衰竭→回落）
    if isinstance(accel4, (int, float)):
        if direction == "DOWN" and accel4 > 0:
            dims["trend_exhaust"] = True
            notes["trend_exhaust"] = f"4H accel={accel4}>0 跌势衰竭→反弹，逆预测DOWN"
        elif direction == "UP" and accel4 < 0:
            dims["trend_exhaust"] = True
            notes["trend_exhaust"] = f"4H accel={accel4}<0 动能衰竭→回落，逆预测UP"
        else:
            dims["trend_exhaust"] = False
            notes["trend_exhaust"] = f"4H accel={accel4} 顺预测或无衰竭"
    else:
        dims["trend_exhaust"] = False
        notes["trend_exhaust"] = "4H accel 缺失"

    return dims, notes


def reaction_apply(direction: str, conf: str, scale, dims: dict, strength: str):
    """按反应强度对基线方向/置信度/仓位做后处理。

    A 只缩仓+锁低：方向不变，置信度锁低，scale 至少 0.5
    B 阻止顺共识锁低：方向不变 + 置信度锁低 + scale 至少 0.5（与 A 在方向层面等价，
      仅当回放需区分"是否由 G3 顺共识覆盖"时用于标记，方向维度不翻转）
    C 翻转方向：预测方向取反（用于量化翻向收益上限）
    """
    new_dir, new_conf, new_scale = direction, conf, scale
    if strength in ("A", "B"):
        new_conf = "低"
        cur = 0.5 if scale in (None, 1.0) else scale
        new_scale = min(cur, 0.5) if isinstance(cur, (int, float)) else 0.5
    elif strength == "C":
        new_dir = "DOWN" if direction == "UP" else "UP"
        new_conf = "低"
        new_scale = 0.5
    return new_dir, new_conf, new_scale


def wilson_lower(p: float, n: int, z: float = 1.645) -> float:
    """Wilson 置信下界（单侧 95%）。"""
    if n == 0:
        return 0.0
    q = 1 - p
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * q / n + z * z / (4 * n * n))
    return centre - half


def main() -> int:
    amap = actual_map()
    # 1AM ET 结算未入库，手工注入（已由 TradingView 1H 蜡烛确认：close 63379.83 > anchor 63372.56 → UP）
    amap["bitcoin-up-or-down-august-14-2026-1am-et"] = "UP"
    # 8/13 9PM（已入库则跳过，未入库手工注入；经核对在库）
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
        base_conf = d.confidence
        base_scale = d.position_scale
        dims, dim_notes = countertrend_dims(md, base_dir, base_conf, mode)
        n_dims = sum(1 for v in dims.values() if v)
        triggered = n_dims >= MIN_DIMS

        reactions = {}
        if triggered:
            for st in ("A", "B", "C"):
                nd, ncf, nsc = reaction_apply(base_dir, base_conf, base_scale, dims, st)
                reactions[st] = (nd, ncf, nsc)
        rows.append({
            "slug": slug, "act": act, "base": base_dir, "conf": base_conf,
            "scale": base_scale, "dims": n_dims, "dims_detail": dims,
            "notes": dim_notes, "triggered": triggered, "mode": mode,
            "reactions": reactions,
            "base_correct": base_dir == act,
        })

    n = len(rows)
    trig = [r for r in rows if r["triggered"]]
    n_trig = len(trig)

    def accuracy(rows_, key="base"):
        if key == "base":
            return sum(1 for r in rows_ if r["base"] == r["act"])
        return sum(1 for r in rows_ if r["reactions"].get(key, (None,))[0] == r["act"])

    def eval_react(st):
        """反应强度 st 相对基线的改善/恶化/持平计数（仅触发场次）。"""
        imp = wor = flat = 0
        for r in trig:
            nd, _, _ = r["reactions"][st]
            if nd == r["act"] and r["base"] != r["act"]:
                imp += 1
            elif nd != r["act"] and r["base"] == r["act"]:
                wor += 1
            else:
                flat += 1
        return imp, wor, flat

    lines = []
    lines.append("# 反手预警模式回测重放验证（2026-08-14）\n")
    lines.append("> 目的：回答\"如果固化为规则，会误判其他场次吗？\"\n")
    lines.append(f"> 样本：{n} 场（有快照 + 有权威结算，含 1AM ET/9PM 两正例注入）；"
                 f"基线方向 = 现行 round16 引擎机算，实际 = hermes_results.jsonl 权威结算。\n")
    lines.append(f"> 反手预警触发阈值：{MIN_DIMS}/4 维度（弱信号贴线 + PM顺共识60-80% + 反向历史时段 + 4H衰竭反向）\n")

    lines.append("## 全样本正确率（基线 round16）\n")
    lines.append(f"| 口径 | 正确 | 正确率 | Wilson95下界 |")
    lines.append(f"|------|------|--------|-------------|")
    acc_b = accuracy(rows)
    lines.append(f"| 基线（round16 现行） | {acc_b}/{n} | {acc_b/n*100:.1f}% | {wilson_lower(acc_b/n, n)*100:.1f}% |\n")

    lines.append("## 反手预警触发场次（核心）\n")
    lines.append(f"触发场次：**{n_trig}/{n}**（{n_trig/n*100:.1f}%）\n")
    if n_trig:
        trig_correct = sum(1 for r in trig if r["base"] == r["act"])
        lines.append(f"触发场次中基线正确率：{trig_correct}/{n_trig} = {trig_correct/n_trig*100:.1f}%\n")

        lines.append("| 场次 | 实际 | 基线方向 | 维度数 | 各维度 | A锁低 | B阻止 | C翻转 | 基线对错 |")
        lines.append("|------|------|---------|--------|--------|------|-------|-------|---------|")
        for r in trig:
            dim_names = "".join(k[0].upper() for k, v in r["dims_detail"].items() if v)
            ra = r["reactions"]["A"][0]
            rb = r["reactions"]["B"][0]
            rc = r["reactions"]["C"][0]
            base_ok = "✅" if r["base_correct"] else "❌"
            lines.append(f"| {r['slug']} | {r['act']} | {r['base']} | {r['dims']} | {dim_names} | "
                         f"{ra} | {rb} | {rc} | {base_ok} |")
        lines.append("")

        lines.append("## 三种反应强度 vs 基线（仅触发场次）\n")
        lines.append("| 反应强度 | 改善 | 恶化 | 持平 | 净效果 | 触发场改判后正确率 | Wilson95下界 |")
        lines.append("|----------|------|------|------|--------|--------------------|-------------|")
        for st, desc in (("A", "只缩仓+锁低(不翻向)"), ("B", "阻止顺共识锁低"), ("C", "翻转方向")):
            imp, wor, flat = eval_react(st)
            acc = accuracy(trig, st)
            net = imp - wor
            lines.append(f"| {st}({desc}) | {imp} | {wor} | {flat} | {net:+d} | "
                         f"{acc}/{n_trig} = {acc/n_trig*100:.1f}% | {wilson_lower(acc/n_trig, n_trig)*100:.1f}% |")
        lines.append("")

    # 未触发场次
    nontrig = [r for r in rows if not r["triggered"]]
    lines.append("## 未触发场次（反手预警不动，确认不受影响）\n")
    lines.append(f"未触发场次：{len(nontrig)}，基线正确率 {accuracy(nontrig)}/{len(nontrig)}"
                 f" = {accuracy(nontrig)/len(nontrig)*100:.1f}%（反手预警规则不影响这些场次）\n")

    # 反手预警触发场次中"反方向胜率"
    if n_trig:
        # C（翻转）= 反方向；统计翻转方向是否更接近实际
        rev_win = sum(1 for r in trig if r["reactions"]["C"][0] == r["act"])
        lines.append("## 关键判据：反手预警触发场次中\"反方向\"胜率\n")
        lines.append(f"- 触发 {n_trig} 场中，反方向（翻转）命中实际 {rev_win}/{n_trig} = {rev_win/n_trig*100:.1f}%"
                     f"（Wilson95下界 {wilson_lower(rev_win/n_trig, n_trig)*100:.1f}%）\n")
        lines.append("> 保本参考：翻向后仍需 vs 基线方向对比；n<30 时反方向胜率仅作观察级，不作落地翻转依据。\n")

    lines.append("## 结论\n")
    if n_trig and rev_win / n_trig > 0.5 and wilson_lower(rev_win / n_trig, n_trig) > 0.5 and n_trig >= 30:
        lines.append("- **反手预警触发场次中反方向胜率显著>50%（样本≥30）→ 可考虑落地翻转（但受\"全局翻转已证伪\"纪律约束，须谨慎）。")
    elif n_trig and rev_win / n_trig > 0.5:
        lines.append("- 反手预警触发场次反方向胜率>50% 但样本 <30（Wilson 下界 ≤50%）→ **观察级**，暂不落地翻转，倾向保守缩仓/锁低（A/B）。")
    else:
        lines.append("- **反手预警触发场次中反方向胜率 ≤50% → 不应翻转方向**；固化为\"缩仓+锁低\"（A/B，方向不变）最安全，避免误判其他场次。")
    lines.append(f"- 触发阈值 {MIN_DIMS}/4 维度：1AM ET 与 8/13 9PM 均命中 4 维（强触发）；若阈值过高/过低可在脚本顶部调整。")
    lines.append("- 反手预警只影响触发场次，未触发场次方向/仓位完全不受影响（见上表）。")

    OUT.write_text("\n".join(lines) + "\n")
    print(f"已生成 {OUT}")
    print(f"样本 {n} 场，反手预警触发 {n_trig}，基线正确率 {acc_b}/{n}={acc_b/n*100:.1f}%")
    if n_trig:
        imp_a, wor_a, _ = eval_react("A")
        imp_c, wor_c, _ = eval_react("C")
        print(f"A(锁低): 改善{imp_a} 恶化{wor_a} | C(翻转): 改善{imp_c} 恶化{wor_c} | 反方向命中 {rev_win}/{n_trig}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
