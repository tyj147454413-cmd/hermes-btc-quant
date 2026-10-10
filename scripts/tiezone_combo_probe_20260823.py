#!/usr/bin/env python3
"""
贴线区专项研究·第二步：增量因子组合投票探针（8/23）。

承接 scripts/tiezone_factor_scan_20260823.py 的结论：
- 真正对引擎有【增量】信息的候选 = 分析B 中 Δ>0 且时间稳定的因子：
  多空比斜率、Taker买卖比(即时)、支阻不对称、MACD hist 5m；
- 单因子翻向全部为负贡献 → 组合只用于"信任度调制"，不用于翻向。

本脚本验证：
1. 投票计数（0-4票顺引擎）与胜率的单调性 + 时间对半稳定性；
2. 分级缩仓反事实（真实 pnl 口径）：票少降仓能挽回多少亏损；
3. 与 PM 置信档的交叉（是否独立信息源）；
4. 因子覆盖率审计（各候选在样本期内的可得性，防幸存者偏差）。

只读分析。
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from replay_common import load_results_full, wilson  # noqa: E402
from tiezone_factor_scan_20260823 import build_rows, conservative_p  # noqa: E402

OUT_MD = Path(__file__).resolve().parent.parent / "data" / "hermes_tiezone_combo_20260823.md"

VOTE_FACTORS = [
    ("taker_now", "Taker即时"),
    ("mh_5m", "MACD hist 5m"),
    ("ls_slope", "多空比斜率"),
    ("kl_asym", "支阻不对称"),
]


def main() -> int:
    rows = build_rows()
    n_all = len(rows)
    wins = sum(1 for r in rows if r["win"])

    def votes_of(r):
        v = 0
        avail = 0
        for key, _label in VOTE_FACTORS:
            val = r["F"].get(key)
            drc = None
            if key == "taker_now":
                drc = ("UP" if val > 1 else "DOWN") if val is not None and val != 1 else None
            elif key == "mh_5m":
                drc = val if val in ("UP", "DOWN") else None
            elif key == "ls_slope":
                drc = ("UP" if val > 0 else "DOWN") if val is not None and val != 0 else None
            elif key == "kl_asym":
                drc = ("UP" if val > 0 else "DOWN") if val is not None and abs(val) > 1e-9 else None
            if drc is None:
                continue
            avail += 1
            if drc == r["final"]:
                v += 1
        return v, avail

    for r in rows:
        v, av = votes_of(r)
        r["votes"], r["avail"] = v, av

    L = [
        "# 贴线区专项研究·第二步：增量因子组合投票探针（8/23）\n",
        f"- 样本同第一步：|dev|<0.15% 共 {n_all} 场，基线命中 {wins}/{n_all}={wins/n_all*100:.1f}%；",
        "- 投票成员（均为\"顺引擎方向\"计 1 票）：Taker买卖比(即时)、MACD hist 5m、多空比斜率、支阻不对称；",
        "- ⚠️ 只做信任度调制，不做翻向（第一步已证单因子翻向全负）。\n",
    ]

    # ---- 覆盖率审计 ----
    L.append("## 一、候选因子覆盖率审计（防幸存者偏差）\n")
    L.append("| 因子 | 非空场数 | 覆盖率 | 备注 |")
    L.append("|---|---|---|---|")
    notes = {"ls_slope": "依赖 long_short_ratio_trend 序列，早期快照可能缺失", "kl_asym": "依赖 key_levels 完整"}
    for key, label in VOTE_FACTORS:
        cnt = sum(1 for r in rows if _has_dir(r, key))
        note = notes.get(key, "")
        L.append(f"| {label} | {cnt} | {cnt/n_all*100:.0f}% | {note} |")
    L.append("")

    # ---- 投票梯度 ----
    L.append("## 二、投票数 × 胜率（可用票口径）\n")
    groups = defaultdict(lambda: [0, 0])
    for r in rows:
        groups[r["votes"]][0] += 1
        groups[r["votes"]][1] += 1 if r["win"] else 0
    L.append("| 顺引擎票数 | 场数 | 命中 | 胜率 | Wilson95% |")
    L.append("|---|---|---|---|---|")
    grad = []
    for v in sorted(groups):
        nn, w = groups[v]
        bl, bh = wilson(w, nn)
        grad.append(w / nn)
        L.append(f"| {v}/{len(VOTE_FACTORS)} | {nn} | {w} | {w/nn*100:.1f}% | [{bl:.0f},{bh:.0f}] |")
    mono = all(grad[i] <= grad[i + 1] for i in range(len(grad) - 1))
    lo_n, lo_w = groups[min(groups)][0], groups[min(groups)][1]
    hi_v, hi_n, hi_w = max(groups)[0] if False else (max(groups), groups[max(groups)][0], groups[max(groups)][1])
    pp = conservative_p(lo_w, lo_n, hi_w, hi_n)
    L.append(f"\n- 单调递增：{'✅ 是' if mono else '❌ 否'}；最低票组 vs 最高票组差异 p={pp:.3f}\n")

    # ---- 时间稳定性 ----
    L.append("## 三、投票梯度的时间对半切稳定性\n")
    rows_sorted = sorted(rows, key=lambda r: (r["collect"] is None, r["collect"]))
    half = len(rows_sorted) // 2
    halves_ok = True
    half_lines = []
    half_grads = []
    for hh, name in ((rows_sorted[:half], "前半"), (rows_sorted[half:], "后半")):
        g = defaultdict(lambda: [0, 0])
        for r in hh:
            g[r["votes"]][0] += 1
            g[r["votes"]][1] += 1 if r["win"] else 0
        vs = sorted(g)
        line = []
        hg = []
        for v in vs:
            nn, w = g[v]
            hg.append((v, w / nn))
            line.append(f"{v}票:{w/nn*100:.0f}%({w}/{nn})")
        slope = hg[-1][1] - hg[0][1]
        half_grads.append(slope)
        half_lines.append(f"{name}: " + "，".join(line))
        L.append(f"- **{name}**：" + "，".join(line))
    if len(half_grads) == 2 and half_grads[0] > 0 and half_grads[1] > 0:
        verdict = "✅ 两半均为正梯度"
    else:
        verdict = "❌ 梯度不稳定"
        halves_ok = False
    L.append(f"\n- 判定：{verdict}\n")

    # ---- 分级缩仓反事实 ----
    L.append("## 四、分级缩仓反事实（真实 pnl 口径，仅贴线区内）\n")
    full = load_results_full()

    def pnl_of(r):
        rec = full.get(r["slug"])
        pnl = rec.get("pnl") if rec else None
        return None if pnl is None else float(pnl)

    pnl_rows = [r for r in rows if pnl_of(r) is not None]
    base_pnl = sum(pnl_of(r) for r in pnl_rows)

    ladders = [
        ("L1 温和：≤1票→×0.5", lambda v: 0.5 if v <= 1 else 1.0),
        ("L2 标准：0票→×0.25；1票→×0.5", lambda v: 0.25 if v == 0 else (0.5 if v == 1 else 1.0)),
        ("L3 激进：≤1票→×0.25；2票→×0.5", lambda v: 0.25 if v <= 1 else (0.5 if v == 2 else 1.0)),
        ("L4 仅极端：0票→×0.5", lambda v: 0.5 if v == 0 else 1.0),
    ]
    L.append(f"- 基准 P&L（{len(pnl_rows)} 场全仓）= **{base_pnl:+.2f} USDT**\n")
    L.append("| 缩仓阶梯 | 受影响场数 | ΔP&L | 新合计 P&L |")
    L.append("|---|---|---|---|")
    for name, fnl in ladders:
        delta = 0.0
        cnt = 0
        for r in pnl_rows:
            s = fnl(r["votes"])
            if s < 1.0:
                delta -= pnl_of(r) * (1 - s)
                cnt += 1
        L.append(f"| {name} | {cnt} | {delta:+.2f} | {base_pnl + delta:+.2f} |")
    L.append("")

    # ---- 与 PM 置信的交叉 ----
    L.append("## 五、投票 × PM对我们方向的置信（独立性检查）\n")
    L.append("| 票数档 | PM<0.55 | PM 0.55-0.65 | PM≥0.65 |")
    L.append("|---|---|---|---|")

    def pm_bucket(r):
        pc = r["F"].get("pm_conf")
        if pc is None:
            return None
        return "<0.55" if pc < 0.55 else ("0.55-0.65" if pc < 0.65 else "≥0.65")

    for vgroup, vname in (((0, 1), "≤1票"), ((2,), "2票"), ((3, 4), "≥3票")):
        cells = []
        for pb in ("<0.55", "0.55-0.65", "≥0.65"):
            sub = [r for r in rows if r["votes"] in vgroup and pm_bucket(r) == pb]
            if sub:
                w = sum(1 for r in sub if r["win"])
                cells.append(f"{w}/{len(sub)}={w/len(sub)*100:.0f}%")
            else:
                cells.append("—")
        L.append(f"| {vname} | " + " | ".join(cells) + " |")
    L.append("")
    low_low = [r for r in rows if r["votes"] <= 1 and pm_bucket(r) in ("<0.55",)]
    if low_low:
        w = sum(1 for r in low_low if r["win"])
        bl, bh = wilson(w, len(low_low))
        L.append(f"- 双弱区（≤1票 且 PM<0.55）：{w}/{len(low_low)}={w/len(low_low)*100:.1f}% [{bl:.0f},{bh:.0f}]"
                 f"——若确认显著低于基线，即为贴线区最优先缩仓格。\n")

    OUT_MD.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"覆盖审计: " + ", ".join(f"{lb}={sum(1 for r in rows if _has_dir(r,k))}/{n_all}" for k, lb in VOTE_FACTORS))
    print(f"投票梯度: " + ", ".join(f"{v}票:{groups[v][1]}/{groups[v][0]}={groups[v][1]/groups[v][0]*100:.0f}%" for v in sorted(groups)))
    print(f"单调性: {'是' if mono else '否'}; 首尾p={pp:.3f}; 半切判定: {verdict}")
    print(f"报告写入 {OUT_MD}")
    return 0


def _has_dir(r, key):
    val = r["F"].get(key)
    if val is None:
        return False
    if key == "taker_now":
        return val != 1
    if key == "mh_5m":
        return val != 0
    if key == "ls_slope":
        return val != 0
    if key == "kl_asym":
        return abs(val) > 1e-9
    return True


if __name__ == "__main__":
    raise SystemExit(main())
