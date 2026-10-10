#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
factor_sensitivity_p013.py — 阈值敏感性分析（P0-1.3）
=====================================================
基于现有快照+结算真值做敏感性诊断，输出:
  ① 全体因子 Wilson 胜率 + 95% CI（n>=30 严格过滤）
  ② 漂移告警：前/后半段胜率差 vs 历史 CI 半径
  ③ 8 族归类汇总
  ④ 砍掉建议（治过拟合风险）

铁律：只做统计诊断，不动 rule_engine 权重，不改 prompt_exec.md，不改任何规则文件。
复用 factor_hitrate_study.py 的 extract_factors/wilson 口径保持一致。
"""
import json, math, os, sys, glob, re
from collections import defaultdict

HERMES_DIR = os.environ.get("HERMES_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERMES_DIR, "data")
SNAP_DIR = os.path.join(DATA, "snapshots")
sys.path.insert(0, os.path.join(HERMES_DIR, "scripts"))
import factor_hitrate_study as base   # 复用 extract_factors / wilson / load_last_snapshots / load_results

# ---------------- 8 族归类（数据驱动，按因子语义聚类） ----------------
FAMILY = {
    "动量信号": [
        "recent_dir_1m", "recent_dir_5m", "recent_dir_15m", "recent_dir_1H", "recent_dir_4H",
    ],
    "K线连续": [
        "连续K线_5m", "连续K线_1H",
    ],
    "均值回归": [
        "布林位置MR_15m", "RSI超买超卖MR_15m",
    ],
    "MACD族": [
        "MACD_15m", "MACD加速度_15m", "MACD_5m",
    ],
    "订单流/成交": [
        "TakerB/S", "TakerB/S_1h均值", "订单流失衡_5m", "订单流失衡_15m",
    ],
    "持仓情绪": [
        "资金费率", "多空比", "恐贪反向",
    ],
    "偏离/位置": [
        "偏离延续(dev符号)", "偏离扩张", "24h位置动量", "开盘方向",
    ],
    "PM共识/时段": [
        "PM共识跟随", "历史时段UP率", "上小时动量延续",
    ],
}

# ---------------- 判定阈值 ----------------
N_MIN = 30                       # 严格样本下限（旧脚本是 15）
TRUE_RATE = 0.58                 # 真信号下限
TRUE_CI_LO = 0.50                # CI 下界必须严格 > 0.50（不可含 50%）
FALSE_RATE = 0.42                # 反向下限
FALSE_CI_HI = 0.50               # CI 上界必须严格 < 0.50
DRIFT_MIN = 0.10                 # |漂移| 绝对值门槛
DRIFT_CI_MULT = 2.0              # 漂移 > 前段 CI 半宽×DRIFT_CI_MULT 才告警


def evaluate(subset, factors):
    """单个 subset 的因子命中评估（含朴素 EV）"""
    out = []
    for fname in factors:
        n = h = 0
        ev_total = 0.0
        ev_n = 0
        for r in subset:
            v = r["fac"].get(fname)
            if v is None: continue
            n += 1
            if v == r["actual"]: h += 1
            # 朴素 EV（与旧脚本同口径）
            p = r.get("up_price")
            if p and 0.01 < p < 0.99:
                pe = p if v == "UP" else 1 - p
                ev_total += ((100.0 / pe - 100.0) if v == r["actual"] else -100.0)
                ev_n += 1
        if n == 0:
            continue
        hr = h / n
        lo, hi = base.wilson(hr, n)
        ev = round(ev_total / ev_n, 1) if ev_n >= 10 else None
        out.append({
            "factor": fname, "n": int(n), "hit": int(h), "rate": hr,
            "ci": (lo, hi), "ci_half": (hi - lo) / 2,
            "ev_per_$100": ev,
        })
    return out


def verdict(e):
    """单因子判定（n>=30 严格版）"""
    if e["n"] < N_MIN:
        return "❓样本不足"
    if e["rate"] >= TRUE_RATE and e["ci"][0] > TRUE_CI_LO:
        return "🟢真信号"
    if e["rate"] <= FALSE_RATE and e["ci"][1] < FALSE_CI_HI:
        return "🔴反向"
    return "⚪噪音"


def kill_decision(e, drift_flag, drift_size, prev_rate):
    """砍掉建议（保守：只标 🟢 为保留，其它都建议砍或观察）"""
    if e["n"] < N_MIN:
        return "✂️ 砍（数据不足，n<30）"
    if verdict(e) == "🟢真信号":
        if drift_flag:
            return f"⚠️ 保留但监控（漂移 {drift_size:+.1%}，上轮 {prev_rate:.1%}）"
        return "✅ 保留"
    if verdict(e) == "🔴反向":
        return "✂️ 砍（反向命中）"
    return "✂️ 砍（噪音，胜率 ≤ 58% 且 CI 含 50%）"


def fmt_pct(x):
    return f"{x*100:.1f}%"


def main():
    snaps, _ = base.load_last_snapshots()
    results = base.load_results()

    rows = []
    for slug, s in sorted(snaps.items()):
        actual = results.get(slug)
        if actual not in ("UP", "DOWN"):
            continue
        fac, meta = base.extract_factors(s)
        rows.append({"slug": slug, "actual": actual, "fac": fac, **meta})
    print(f"可归因小时数: {len(rows)}")

    if not rows:
        print("无数据"); return

    # 时间排序 + 切前后半段（漂移告警用）
    rows.sort(key=lambda r: (r["slug"][16:26], base.slug_et_hour(r["slug"]) or 0))
    half = len(rows) // 2
    prev_rows, post_rows = rows[:half], rows[half:]

    FACTORS = list(rows[0]["fac"].keys())

    # 全体评估
    overall = evaluate(rows, FACTORS)

    # 前/后半段评估（用于漂移）
    prev_eval = {e["factor"]: e for e in evaluate(prev_rows, FACTORS)}
    post_eval = {e["factor"]: e for e in evaluate(post_rows, FACTORS)}

    # ===== 漂移告警 =====
    drift_alerts = []
    for e in overall:
        p, q = prev_eval.get(e["factor"]), post_eval.get(e["factor"])
        if not p or not q: continue
        if p["n"] < N_MIN or q["n"] < N_MIN: continue
        size = q["rate"] - p["rate"]
        ci_half = p["ci_half"]
        flag = abs(size) >= max(DRIFT_MIN, ci_half * DRIFT_CI_MULT)
        e["drift_size"] = size
        e["drift_flag"] = flag
        e["prev_rate"] = p["rate"]
        e["post_rate"] = q["rate"]
        if flag:
            drift_alerts.append(e)

    # ===== 报告 =====
    rep = [
        "# P0-1.3 阈值敏感性分析（治过拟合风险）",
        "",
        f"- 生成：快照×结算 可归因样本 = **{len(rows)} 小时**",
        f"- 区间：{rows[0]['slug'][:42]} … {rows[-1]['slug'][:42]}",
        f"- 漂移切点：前段 n={half} / 后段 n={len(rows)-half}",
        f"- 严格过滤：**n >= {N_MIN}**（旧版 n>=15 已升级为更严的 30）",
        f"- 判定：🟢真信号 胜率 ≥ {TRUE_RATE:.0%} 且 CI 下界 > 50%；🔴反向 胜率 ≤ {FALSE_RATE:.0%} 且 CI 上界 < 50%；⚪噪音 其它",
        f"- 漂移告警：|后段−前段| ≥ {DRIFT_MIN:.0%} **或** ≥ 前段 CI 半宽 × {DRIFT_CI_MULT:.1f}",
        "",
        "**只诊断、不改规则**——结论供第五步权重设计参考。",
        "",
    ]

    # ----- 全体真值表 -----
    rep.append("## 一、全体因子真值表（n>=30 严格过滤）\n")
    rep.append("| 因子 | 族 | n | 命中 | 胜率 | 95%CI | 朴素EV/$100 | 漂移 | 判定 | 砍掉建议 |")
    rep.append("|---|---|---:|---:|---:|---:|---:|---|---|---|")

    # 按族排序
    fam_index = {f: fam for fam, fs in FAMILY.items() for f in fs}
    overall.sort(key=lambda e: (fam_index.get(e["factor"], "未分类"), -e["n"]))

    for e in overall:
        fam = fam_index.get(e["factor"], "未分类")
        drift_s = f"{e.get('drift_size', 0):+.1%}" if "drift_size" in e else "—"
        drift_flag = "🚨" if e.get("drift_flag") else "—"
        ev_s = f"{e['ev_per_$100']:+.1f}" if e.get("ev_per_$100") is not None else "—"
        kill = kill_decision(e, e.get("drift_flag", False),
                             e.get("drift_size", 0), e.get("prev_rate", 0))
        rep.append(f"| {e['factor']} | {fam} | {e['n']} | {e['hit']} | {fmt_pct(e['rate'])} "
                   f"| [{fmt_pct(e['ci'][0])},{fmt_pct(e['ci'][1])}] | {ev_s} "
                   f"| {drift_flag}{drift_s} | {verdict(e)} | {kill} |")

    # ----- 漂移告警专章 -----
    rep.append(f"\n## 二、漂移告警（前/后半段胜率差 ≥ {DRIFT_MIN:.0%} 或 ≥ 前段 CI 半宽 × {DRIFT_CI_MULT:.1f}）\n")
    if not drift_alerts:
        rep.append("**无漂移告警**——所有 n>=30 的因子前后半段胜率差均在阈值内。\n")
    else:
        rep.append(f"**共 {len(drift_alerts)} 个因子触发漂移告警**：\n")
        rep.append("| 因子 | 族 | 前段 n | 前段胜率 | 后段 n | 后段胜率 | 漂移 | 前段 CI 半宽 | 阈值 |")
        rep.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
        for e in sorted(drift_alerts, key=lambda x: -abs(x.get("drift_size", 0))):
            p = prev_eval[e["factor"]]
            q = post_eval[e["factor"]]
            threshold = max(DRIFT_MIN, p["ci_half"] * DRIFT_CI_MULT)
            fam = fam_index.get(e["factor"], "未分类")
            rep.append(f"| {e['factor']} | {fam} | {p['n']} | {fmt_pct(p['rate'])} "
                       f"| {q['n']} | {fmt_pct(q['rate'])} | {e.get('drift_size', 0):+.1%} "
                       f"| {fmt_pct(p['ci_half'])} | {fmt_pct(threshold)} |")
        rep.append("")
        rep.append("**含义**：这些因子的胜率在样本期发生过结构性变化，可能是市场regime切换，也可能是因子本身不稳定（过拟合/失效）。第五步权重设计必须把漂移纳入——保守做法是降权或加窗口化更新。")

    # ----- 8 族汇总 -----
    rep.append("\n## 三、8 族归类汇总\n")
    rep.append("| 族 | 因子数 | 真信号 | 反向 | 噪音 | 样本不足 | 健康度 |")
    rep.append("|---|---:|---:|---:|---:|---:|---|")

    for fam, factors in FAMILY.items():
        sub = [e for e in overall if e["factor"] in factors]
        if not sub: continue
        n_total = len(sub)
        n_true = sum(1 for e in sub if verdict(e) == "🟢真信号")
        n_false = sum(1 for e in sub if verdict(e) == "🔴反向")
        n_noise = sum(1 for e in sub if verdict(e) == "⚪噪音")
        n_weak = sum(1 for e in sub if verdict(e) == "❓样本不足")
        # 健康度 = 真信号比例（n>=30 因子中）
        qualified = [e for e in sub if e["n"] >= N_MIN]
        healthy = n_true / len(qualified) if qualified else 0
        health_icon = "🟢强" if healthy >= 0.5 else ("🟡弱" if healthy >= 0.25 else "🔴废")
        rep.append(f"| {fam} | {n_total} | {n_true} | {n_false} | {n_noise} | {n_weak} | {health_icon}（{fmt_pct(healthy)} 真信号） |")

    # ----- 砍掉清单 -----
    rep.append("\n## 四、砍掉清单（按族汇总）\n")

    kill_groups = defaultdict(list)
    for e in overall:
        if e["n"] < N_MIN: continue
        kill = kill_decision(e, e.get("drift_flag", False),
                             e.get("drift_size", 0), e.get("prev_rate", 0))
        if "✂️" in kill or "⚠️" in kill:
            fam = fam_index.get(e["factor"], "未分类")
            kill_groups[(fam, "✂️ 砍" in kill)].append(e)

    rep.append("### 4.1 建议直接砍（噪音 / 反向）\n")
    has_kill = False
    for (fam, is_kill), factors in sorted(kill_groups.items()):
        if not is_kill: continue
        has_kill = True
        rep.append(f"**{fam}**：")
        for e in sorted(factors, key=lambda x: x["rate"]):
            reason = "反向" if verdict(e) == "🔴反向" else f"噪音 {fmt_pct(e['rate'])}"
            rep.append(f"- `{e['factor']}` n={e['n']} 胜率 {fmt_pct(e['rate'])} [{fmt_pct(e['ci'][0])},{fmt_pct(e['ci'][1])}] → {reason}")
        rep.append("")
    if not has_kill:
        rep.append("（无）\n")

    rep.append("### 4.2 建议监控（保留但有漂移告警）\n")
    monitor = [e for e in overall if e.get("drift_flag") and e["n"] >= N_MIN and verdict(e) == "🟢真信号"]
    if monitor:
        for e in monitor:
            rep.append(f"- `{e['factor']}`（{fam_index.get(e['factor'], '?')}）后段胜率 {fmt_pct(e.get('post_rate', 0))}，上轮 {fmt_pct(e.get('prev_rate', 0))}")
    else:
        rep.append("（无真信号因子触发漂移告警）\n")

    # ----- 总体结论 -----
    n_qualified = sum(1 for e in overall if e["n"] >= N_MIN)
    n_true = sum(1 for e in overall if e["n"] >= N_MIN and verdict(e) == "🟢真信号")
    n_false = sum(1 for e in overall if e["n"] >= N_MIN and verdict(e) == "🔴反向")
    n_noise = sum(1 for e in overall if e["n"] >= N_MIN and verdict(e) == "⚪噪音")
    n_drift = len(drift_alerts)

    rep.append("\n## 五、总体结论\n")
    rep.append(f"- 合格样本（n>=30）因子：**{n_qualified}** 个")
    rep.append(f"- 🟢真信号：**{n_true}** 个")
    rep.append(f"- ⚪噪音：**{n_noise}** 个")
    rep.append(f"- 🔴反向：**{n_false}** 个")
    rep.append(f"- 🚨 漂移告警：**{n_drift}** 个\n")

    if n_qualified > 0:
        rep.append(f"- **真信号比例**：{n_true}/{n_qualified} = **{n_true/n_qualified:.0%}**\n")

    rep.append("\n**过拟合风险评估**：")
    if n_true >= 5 and n_drift <= 3:
        rep.append("- 因子集中度健康（>=5 真信号、<=3 漂移）→ 当前权重风险可控")
    elif n_true < 3:
        rep.append("- 🔴 **真信号 < 3 个，因子集严重营养不良**——权重设计应考虑减少因子数量或寻找新因子")
    elif n_drift > 5:
        rep.append("- 🔴 **漂移因子过多（>5 个）**——市场 regime 可能切换，所有权重应保守处理")
    else:
        rep.append("- 🟡 中等——存在结构性风险，建议每周重跑本脚本观察漂移趋势")

    rep.append("")
    rep.append("---")
    rep.append("")
    rep.append("**诚实声明**：① 全部为样本内统计，未做 Bonferroni 多重比较校正（27 因子 × 多切片会有假阳性）；② 漂移告警为前后半段对比，样本期短时不稳定；③ 结论仅供第五步权重设计参考，本轮不落地任何规则变更。")

    text = "\n".join(rep)
    out_path = os.path.join(HERMES_DIR, "docs", "factor_sensitivity_p013_20260830.md")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    open(out_path, "w").write(text)
    print(f"\n完整报告已写入: {out_path}")
    print(f"\n摘要: 样本 {len(rows)} 合格 {n_qualified} | 🟢 {n_true} ⚪ {n_noise} 🔴 {n_false} | 漂移 {n_drift}")


if __name__ == "__main__":
    main()