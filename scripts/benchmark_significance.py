#!/usr/bin/env python3
"""
Hermes benchmark 显著性升级（C5 / 审计 P1-11，2026-08-23）。

背景（审计 P1-11 原文）：
    "+7.1pt 超额 Wilson 区间重叠不显著；最强基准是 always_up → 牛市 beta 嫌疑未排除。
     加 regime 切分对照 + 置换检验。"

本脚本回答一个问题：**引擎跑赢笨办法，是本事（alpha）还是行情（beta）+运气？**

方法：
    1. 基准组：always_up / always_down / pm_follow / pm_fade（无脑与跟盘策略）
       引擎组：生产引擎两遍 extreme_wall 重放（与 threshold_grid 同一缓存与口径）
    2. 三重检验（都做在配对设计上——同一场同一结算，天然控制行情）：
       a. Wilson95 区间对照（旧口径，保留展示其局限）
       b. McNemar 精确检验：只看两者结论相反的场次，二项检验"分对能力是否对称"
       c. 置换检验：对不一致场做 20000 次符号翻转，看真实优势落在随机分布的位置
    3. regime 切分：趋势市/摆动市分别对比引擎 vs always_up——
       若只在涨势/单一 regime 赢 → beta 嫌疑成立

判定口径（写死，改动须同步报告模板）：
    ✅alpha成立    最强对手 McNemar p<0.05 且各 regime 方向一致（均不落后）
    🟡证据不足     p<0.05 但 regime 间不一致；或 0.05≤p<0.10
    ❌beta嫌疑    p≥0.10（现有样本无法与运气区分）

安全声明：只读快照/results；只写 data/benchmark_significance_report*.md。

用法：
    python3 scripts/benchmark_significance.py [--perms 20000]
"""

from __future__ import annotations

import argparse
import json
import math
import random
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REPORT_CANONICAL = DATA_DIR / "benchmark_significance_report.md"

sys.path.insert(0, str(ROOT / "scripts"))
import threshold_grid as tg  # noqa: E402  复用缓存加载/重放/指标（同一口径，避免两处漂移）


# ------------------------------------------------------------------
# 统计检验（纯函数，可单测）
# ------------------------------------------------------------------

def binom_two_sided_p(k: int, n: int) -> float:
    """二项双侧精确 p：P(X=k) 两侧尾部求和。n=0 → p=1。"""
    if n == 0:
        return 1.0
    def pmf(i):
        return math.comb(n, i) * (0.5 ** n)
    pk = pmf(k)
    tail = sum(pmf(i) for i in range(n + 1) if pmf(i) <= pk + 1e-15)
    return min(1.0, tail)


def mcnemar_exact(b01: int, b10: int) -> float:
    """配对二元的 McNemar 精确检验。b01=A对B错, b10=A错B对。返回双侧 p。"""
    return binom_two_sided_p(min(b01, b10), b01 + b10)


def permutation_p(b01: int, b10: int, n_perm: int = 20000, seed: int = 41) -> float:
    """符号翻转置换：不一致场随机归边，看 |差| ≥ 真实|差| 的比例（含本身）。"""
    k = b01 + b10
    if k == 0:
        return 1.0
    obs = abs(b01 - b10)
    rng = random.Random(seed)
    hits = 0
    for _ in range(n_perm):
        x = sum(1 for _ in range(k) if rng.random() < 0.5)
        if abs(2 * x - k) >= obs:
            hits += 1
    return hits / n_perm


def verdict_of(p_mcn: float, p_perm: float, regime_ok: bool) -> str:
    p = max(p_mcn, p_perm)  # 取更保守者
    if p < 0.05 and regime_ok:
        return "✅alpha成立"
    if p < 0.05:
        return "🟡证据不足(regime间不一致)"
    if p < 0.10:
        return "🟡证据不足(边缘)"
    return "❌beta嫌疑(与运气不可区分)"


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="benchmark 显著性（C5/审计P1-11）")
    ap.add_argument("--perms", type=int, default=20000)
    ap.add_argument("--archive", action="store_true")
    args = ap.parse_args()

    ts = datetime.now(timezone.utc)
    cache = tg.load_settled()
    n_total = len(cache)
    engine_mod = tg.make_patched_engine(None)

    # 逐场：引擎侧 + 基准侧（配对设计）
    rows = []
    for rec in cache:
        d = tg.decide(engine_mod, rec, 3.0)
        eng_side = d.final_direction if d else None
        pm_up = rec["pm_up"]
        rows.append({
            "slug": rec["slug"], "actual": rec["actual"], "regime": rec["regime"],
            "eng": eng_side,
            "up": "UP", "down": "DOWN",
            "pm_follow": ("UP" if (pm_up is not None and pm_up >= 50) else
                          ("DOWN" if pm_up is not None else None)),
            "pm_up_raw": pm_up,
            # C5审计修复(🟡6): DOWN 优先真实 pm_down（与 threshold_grid/wf_gate 口径对齐）
            "pm_down_raw": rec.get("pm_down"),
        })
        if rows[-1]["pm_follow"] == "UP":
            rows[-1]["pm_fade"] = "DOWN"
        elif rows[-1]["pm_follow"] == "DOWN":
            rows[-1]["pm_fade"] = "UP"
        else:
            rows[-1]["pm_fade"] = None

    def ask_of(side: str, pm_up, pm_down=None) -> float | None:
        a = None
        if side == "UP" and pm_up is not None:
            a = pm_up / 100.0
        elif side == "DOWN":
            if pm_down is not None:
                a = pm_down / 100.0
            elif pm_up is not None:
                a = (100 - pm_up) / 100.0
        return a if (a is not None and 0 < a <= tg.MAX_PRICE) else None

    def settle(row: dict, key: str):
        """(win, pnl_share) 或 None(未成交)。"""
        side = row[key]
        if side not in ("UP", "DOWN"):
            return None
        a = ask_of(side, row["pm_up_raw"], row.get("pm_down_raw"))
        if a is None:
            return None
        win = side == row["actual"]
        return win, (1 - a) if win else -a

    strategies = ["eng", "up", "down", "pm_follow", "pm_fade"]
    labels = {"eng": "生产引擎", "up": "always_up(无脑买涨)", "down": "always_down",
              "pm_follow": "pm_follow(跟盘)", "pm_fade": "pm_fade(反盘)"}

    stats = {}
    for s in strategies:
        res = [settle(r, s) for r in rows]
        n = sum(1 for x in res if x)
        w = sum(1 for x in res if x and x[0])
        pnl = sum(x[1] for x in res if x)
        lo, hi = tg.wilson(w, n)
        stats[s] = {"n": n, "w": w, "wr": round(w / n * 100, 1) if n else 0.0,
                    "lo": round(lo, 1), "hi": round(hi, 1), "ev": pnl / n if n else 0.0}

    print(f"样本: {n_total} 已结算")
    for s in strategies:
        st = stats[s]
        print(f"  {labels[s]:18s} n={st['n']:3d} wr={st['wr']:5.1f}% "
              f"Wilson[{st['lo']},{st['hi']}] EV{st['ev']*100:+6.1f}%")

    # ---- 配对检验：引擎 vs 每个对手 ----
    lines = [
        "# Benchmark 显著性报告（C5 / 审计 P1-11）",
        "",
        f"- 生成(UTC)：{ts.isoformat()}｜样本：{n_total} 已结算｜置换次数：{args.perms}",
        "- 配对设计：同一场同结算，只比结论相反的场次（McNemar/置换天然控制行情共变）。",
        "",
        "## 总表",
        "",
        "| 策略 | 成交n | 胜率 | Wilson95 | EV/单 |", "|---|---:|---:|---|---:|",
    ]
    for s in strategies:
        st = stats[s]
        lines.append(f"| {labels[s]} | {st['n']} | {st['wr']}% | [{st['lo']}, {st['hi']}] "
                     f"| {st['ev']*100:+.1f}% |")

    strongest = max((s for s in strategies if s != "eng"), key=lambda s: stats[s]["wr"])
    lines += ["", "## 引擎 vs 各对手（配对检验）", "",
              "| 对手 | 引擎对/对手错 | 引擎错/对手对 | McNemar p | 置换 p | 判定 |",
              "|---|---:|---:|---:|---:|---|"]
    verdicts = {}
    for s in strategies:
        if s == "eng":
            continue
        b01 = b10 = 0
        for r in rows:
            a, b = settle(r, "eng"), settle(r, s)
            if a is None or b is None:
                continue
            if a[0] and not b[0]:
                b01 += 1
            elif b[0] and not a[0]:
                b10 += 1
        p_m = mcnemar_exact(b01, b10)
        p_p = permutation_p(b01, b10, args.perms)
        verdicts[s] = (p_m, p_p)
        lines.append(f"| {labels[s]} | {b01} | {b10} | {p_m:.4f} | {p_p:.4f} | （下表给判定） |")

    # ---- regime 切分（引擎 vs 最强对手） ----
    regimes = sorted({r["regime"] for r in rows})
    lines += ["", f"## Regime 切分：引擎 vs {labels[strongest]}", "",
              "| Regime | 引擎n/胜率 | 对手n/胜率 | 引擎-对手(pp) |", "|---|---|---|---:|"]
    regime_ok = True
    regime_rows = []
    for rg in regimes:
        sub = [r for r in rows if r["regime"] == rg]
        def wr_of(key):
            res = [settle(r, key) for r in sub]
            n = sum(1 for x in res if x)
            w = sum(1 for x in res if x and x[0])
            return n, (w / n * 100 if n else 0.0)
        ne, we = wr_of("eng")
        no_, wo = wr_of(strongest)
        delta = we - wo
        regime_rows.append(delta)
        lines.append(f"| {rg} | {ne}/{we:.1f}% | {no_}/{wo:.1f}% | {delta:+.1f} |")
        if delta < -2:
            regime_ok = False

    p_m, p_p = verdicts[strongest]
    v = verdict_of(p_m, p_p, regime_ok)
    lines += ["", "## 结论", "",
              f"- 最强对手 = **{labels[strongest]}**（胜率 {stats[strongest]['wr']}%）",
              f"- McNemar p={p_m:.4f}｜置换 p={p_p:.4f}｜regime 方向一致={regime_ok}",
              f"- **判定：{v}**",
              "",
              "> 口径：p<0.05 且各 regime 不落后 → alpha 成立；p≥0.10 → 现有样本无法把引擎优势",
              "> 与运气/行情区分（审计 P1-11 的 beta 嫌疑即此含义）。样本继续积累后重跑本脚本复核。",
              ""]

    REPORT_CANONICAL.write_text("\n".join(lines), encoding="utf-8")
    if args.archive:
        arch = DATA_DIR / f"benchmark_significance_report_{ts.strftime('%Y%m%d_%H%M')}.md"
        shutil.copyfile(REPORT_CANONICAL, arch)

    print(f"\n=== 结论 === 最强对手 {labels[strongest]}: McNemar p={p_m:.4f} 置换 p={p_p:.4f}")
    print(f"regime 切分差值: {['%+.1f' % x for x in regime_rows]}")
    print(f"判定: {v}")
    print(f"报告: {REPORT_CANONICAL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
