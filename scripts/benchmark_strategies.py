#!/usr/bin/env python3
"""
Hermes 基准策略对照（升级计划书 任务 1.4，2026-08-22）。

定位：
    在同一回测框架下比较 Hermes 规则引擎与四个朴素基准，
    输出超额收益（alpha）结论 —— 回答「Hermes 到底有没有 alpha」。

安全声明：只读历史数据；仅写 data/benchmark_report.md 新文件。

基准：
    follow_pm   永远顺 PM 共识
    fade_pm     永远逆 PM 共识
    always_up   永远买 UP
    always_down 永远买 DOWN
    random      随机方向（seed=42）

用法：
    python3 scripts/benchmark_strategies.py                 # 默认参数
    python3 scripts/benchmark_strategies.py --pick last     # 对照口径
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERMES_DIR = os.environ.get("HERMES_DIR")
ROOT = Path(_HERMES_DIR) if _HERMES_DIR else Path("/Users/tangyijun/Desktop/hermes btc")
sys.path.insert(0, str(ROOT / "scripts"))

from replay_common import load_results_full  # noqa: E402
import backtest_engine as bt  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Hermes vs 基准策略对照（任务1.4）")
    ap.add_argument("--pick", choices=["first", "last"], default="first")
    ap.add_argument("--max-price", type=float, default=0.60)
    ap.add_argument("--fee", type=float, default=0.0)
    ap.add_argument("--report", default="data/benchmark_report.md")
    args = ap.parse_args()

    labels = load_results_full()
    snaps = bt.get_snapshots(args.pick)
    events = []
    for slug, path in sorted(snaps.items()):
        try:
            events.append({"slug": slug,
                           "md": json.loads(path.read_text(encoding="utf-8"))})
        except (OSError, json.JSONDecodeError):
            continue

    names = ["follow_pm", "fade_pm", "always_up", "always_down", "random", "hermes_engine"]
    strategies = bt.build_strategies(names, args.max_price)
    results = {st.name: bt.run_backtest(st, events, labels, args.max_price, args.fee)["metrics"]
               for st in strategies}

    hermes = results["hermes_engine"]
    baselines = {k: v for k, v in results.items() if k != "hermes_engine"}
    best_base = max(baselines.values(), key=lambda m: m["ev_per_trade"])
    worst_base = min(baselines.values(), key=lambda m: m["ev_per_trade"])
    avg_base_ev = sum(m["ev_per_trade"] for m in baselines.values()) / len(baselines)
    alpha_best = hermes["ev_per_trade"] - best_base["ev_per_trade"]
    alpha_avg = hermes["ev_per_trade"] - avg_base_ev

    # 胜率差是否统计显著（两比例近似，用 Wilson 区间粗判）
    sig_note = ""
    if hermes["n_settled"] >= 30:
        h_lo = hermes["wilson95"][0]
        b_hi = best_base["wilson95"][1]
        sig_note = (f"Hermes 胜率 Wilson 下界 {h_lo}% vs 最强基准上界 {b_hi}% → "
                    + ("区间分离，优势较稳健" if h_lo > b_hi else "区间重叠，样本尚不足以严格区分"))

    lines = [
        "# Hermes vs 基准策略对照报告（任务 1.4）",
        "",
        f"- 生成(UTC)：{datetime.now(timezone.utc).isoformat()}",
        f"- 口径：pick={args.pick}，max_price={args.max_price}，fee={args.fee}，事件 {len(events)} 个",
        "",
        "| 策略 | 结算 | 胜率 | Wilson95 | EV/单 | 总P&L/share | 最大回撤 |",
        "|---|---:|---:|---|---:|---:|---:|",
    ]
    order = sorted(results.values(), key=lambda m: -m["ev_per_trade"])
    for m in order:
        tag = "**Hermes**" if m["strategy"] == "hermes_engine" else m["strategy"]
        lines.append(
            f"| {tag} | {m['n_settled']} | {m['win_rate']*100:.1f}% "
            f"| {m['wilson95'][0]}~{m['wilson95'][1]}% | {m['ev_per_trade']*100:+.1f}% "
            f"| {m['total_pnl_per_share']:+.2f} | {m['max_drawdown_per_share']:.2f} |")

    lines += [
        "",
        "## Alpha 结论",
        "",
        f"- Hermes EV/单 **{hermes['ev_per_trade']*100:+.1f}%** vs 最强基准 "
        f"{best_base['strategy']} {best_base['ev_per_trade']*100:+.1f}% → "
        f"**超额 {alpha_best*100:+.1f}pt/单**",
        f"- vs 基准平均 {avg_base_ev*100:+.1f}% → 超额 {alpha_avg*100:+.1f}pt/单",
        f"- {sig_note}",
        "",
        "> 判读：若 Hermes EV 不高于基准平均，说明规则链路没有带来信息增量，",
        "> 应先修策略再谈加仓；本表为历史样本结论，非未来保证。",
        "",
    ]

    rp = ROOT / args.report
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("\n".join(lines), encoding="utf-8")

    print(f"Hermes EV/单 {hermes['ev_per_trade']*100:+.1f}%｜最强基准 {best_base['strategy']} "
          f"{best_base['ev_per_trade']*100:+.1f}%｜超额 {alpha_best*100:+.1f}pt")
    print(f"报告: {rp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
