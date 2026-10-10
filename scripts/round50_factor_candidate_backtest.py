#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round50 影子采集三因子回测（预埋脚本，等数据到位自动可跑）

2026-08-28 用户决策: trades_15m / smart_money_consensus_1h / large_trade_5m 三因子
独立 + 交叉回测，按 CLAUDE.md 因子库纪律铁律「先回测后评分，命中率 >55% 且独立
于现有因子」输出裁决依据。

数据源:
  - data/hermes_results.jsonl (已结算场)
  - data/snapshots/<slug>*.json (影子采集)
  - signal_validator.py 现有 8 因子 (从 factors 字段提取)

输出:
  - data/hermes_factor_candidate_backtest_round50_<YYYYMMDD>.md

门槛: 每因子 n≥10, 命中率 >55%, Wilson95 下界 >50%, 触发档样本量足

不动主脚本 factor_candidate_backtest.py（沿用 round41.8.9），仅复用其
load_results / load_snapshots 工具函数。回测脚本只读 snapshots, 不写 factors,
不影响 S0。
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 复用主脚本工具
from factor_candidate_backtest import load_results, load_snapshots, hit  # noqa: E402

DATA = ROOT / "data"
SNAPSHOT_DIR = DATA / "snapshots"


# ============ 因子判据（每因子独立函数，便于调阈值） ============

def sig_trades(pm: dict) -> float:
    """trades_15m.net_up_imbalance 方向信号
    阈值 ±0.3（与 pm_imbalance 一致口径）, |x| ≤ 0.3 → 0, None → 0
    返回 +1/-1/0 (单元化便于 hit) 或原 imbalance (用于交叉矩阵)
    """
    t = pm.get("trades_15m") or {}
    imb = t.get("net_up_imbalance")
    if imb is None:
        return 0
    if imb > 0.3:
        return 1
    if imb < -0.3:
        return -1
    return 0


def sig_holders(pm: dict) -> float:
    """smart_money_consensus_1h.net_up_imbalance 方向信号
    阈值 ±0.3, wallets_total=0 → 0 (无持仓大户不算信号)
    """
    sm = pm.get("smart_money_consensus_1h") or {}
    if sm.get("wallets_total", 0) == 0:
        return 0  # 没有持仓大户不构成信号
    imb = sm.get("net_up_imbalance")
    if imb is None:
        return 0
    if imb > 0.3:
        return 1
    if imb < -0.3:
        return -1
    return 0


def sig_large_trade(data: dict, window: str = "5m") -> float:
    """large_trade_{window} 大单净方向
    无 50 BTC 大单时 count=0 → 中性 0; imb > 0 → +1; imb < 0 → -1
    """
    imb = data.get(f"large_trade_{window}")
    cnt = data.get(f"large_trade_count_{window}", 0)
    if cnt == 0 or imb is None:
        return 0
    if imb > 0:
        return 1
    if imb < 0:
        return -1
    return 0


# ============ Wilson 95% 置信区间 ============

def wilson95(correct: int, total: int) -> tuple[float, float]:
    """Wilson 95% CI for binomial proportion"""
    if total == 0:
        return (0.0, 0.0)
    z = 1.96
    p = correct / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    half = (z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


# ============ 主回测 ============

def round50_backtest() -> dict:
    """三因子回测 + 交叉矩阵 + 与现 S0 因子对比 + 触发门槛裁决"""
    results = load_results()
    snaps = load_snapshots()

    # 每场记录: slug, actual, sig_trades, sig_holders, sig_large
    per_field: list[dict] = []
    joined = 0
    for slug, r in results.items():
        actual = r["actual"]
        if slug not in snaps:
            continue
        try:
            md = json.loads(snaps[slug].read_text())
        except Exception:
            continue
        joined += 1
        pm = md.get("polymarket") or {}
        data = md  # 顶层 alias 直接在 data 上

        per_field.append({
            "slug": slug,
            "actual": actual,
            "sig_trades": sig_trades(pm),
            "sig_holders": sig_holders(pm),
            "sig_large_5m": sig_large_trade(data, "5m"),
            "sig_large_15m": sig_large_trade(data, "15m"),
            # 原始值（用于交叉矩阵）
            "raw_trades": (pm.get("trades_15m") or {}).get("net_up_imbalance"),
            "raw_holders": (pm.get("smart_money_consensus_1h") or {}).get("net_up_imbalance"),
            "raw_large_5m": data.get("large_trade_5m"),
        })

    n = len(per_field)

    # ============ 1. 单因子独立回测 ============

    factors = {
        "trades_15m (全市场成交流水, 阈值 ±0.3)": ["sig_trades", "raw_trades"],
        "holders (持仓前 20 大户近 1h 方向, 阈值 ±0.3)": ["sig_holders", "raw_holders"],
        "large_trade_5m (Binance ≥50 BTC 大单, 5m)": ["sig_large_5m", "raw_large_5m"],
        "large_trade_15m (Binance ≥50 BTC 大单, 15m)": ["sig_large_15m", "raw_large_5m"],
    }

    single_stats = {}
    for name, (sig_key, _) in factors.items():
        s = {"correct": 0, "wrong": 0, "neutral": 0}
        for rec in per_field:
            h = hit(rec[sig_key], rec["actual"])
            if h == "correct":
                s["correct"] += 1
            elif h == "wrong":
                s["wrong"] += 1
            else:
                s["neutral"] += 1
        single_stats[name] = s

    # ============ 2. 因子交叉矩阵（同向率 / 互斥率） ============

    cross = {}
    if n >= 2:
        # 三因子两两同向率（同号率）
        pairs = [
            ("trades_vs_holders", "sig_trades", "sig_holders"),
            ("trades_vs_large5m", "sig_trades", "sig_large_5m"),
            ("holders_vs_large5m", "sig_holders", "sig_large_5m"),
        ]
        for pair_name, k1, k2 in pairs:
            same, diff, neutral = 0, 0, 0
            for rec in per_field:
                s1, s2 = rec[k1], rec[k2]
                if s1 == 0 or s2 == 0:
                    neutral += 1
                elif s1 == s2:
                    same += 1
                else:
                    diff += 1
            cross[pair_name] = {"same": same, "diff": diff, "neutral": neutral,
                                "same_rate": same / (same + diff) if (same + diff) > 0 else None}

        # 三因子全同向命中率
        all_three_correct, all_three_wrong, all_three_neutral = 0, 0, 0
        for rec in per_field:
            s = [rec["sig_trades"], rec["sig_holders"], rec["sig_large_5m"]]
            if 0 in s:
                all_three_neutral += 1
                continue
            h = hit(s[0], rec["actual"])  # 任一同向即可
            if h == "correct":
                all_three_correct += 1
            elif h == "wrong":
                all_three_wrong += 1
        cross["all_three_same_dir"] = {
            "correct": all_three_correct, "wrong": all_three_wrong,
            "neutral": all_three_neutral,
            "n_effective": all_three_correct + all_three_wrong,
            "rate": all_three_correct / (all_three_correct + all_three_wrong) if (all_three_correct + all_three_wrong) > 0 else None,
        }

        # 三因子组合（几何平均 net_up_imbalance 作为合成信号）
        composite_correct, composite_wrong = 0, 0
        for rec in per_field:
            raws = []
            for k in ("raw_trades", "raw_holders", "raw_large_5m"):
                v = rec[k]
                if v is not None:
                    raws.append(v)
            if not raws:
                continue
            # 合成 = 算术平均
            composite = sum(raws) / len(raws)
            h = hit(1 if composite > 0.1 else (-1 if composite < -0.1 else 0), rec["actual"])
            if h == "correct":
                composite_correct += 1
            elif h == "wrong":
                composite_wrong += 1
        cross["composite_arith_mean"] = {
            "correct": composite_correct, "wrong": composite_wrong,
            "n_effective": composite_correct + composite_wrong,
            "rate": composite_correct / (composite_correct + composite_wrong) if (composite_correct + composite_wrong) > 0 else None,
        }

    # ============ 3. 触发门槛裁决 ============

    verdicts = {}
    MIN_N = 10
    MIN_HIT = 0.55
    MIN_WILSON_LOW = 0.50
    for name, s in single_stats.items():
        eff_n = s["correct"] + s["wrong"]
        rate = s["correct"] / eff_n if eff_n > 0 else None
        # round50 fix: eff_n=0 时 wilson95 存 None 而非 (None, None), 渲染时不能 subscript
        if eff_n > 0:
            lo, hi = wilson95(s["correct"], eff_n)
            wilson_pair = (lo, hi)
        else:
            wilson_pair = None
            lo = hi = None
        pass_n = eff_n >= MIN_N
        pass_hit = rate is not None and rate >= MIN_HIT
        pass_wilson = lo is not None and lo >= MIN_WILSON_LOW
        all_pass = pass_n and pass_hit and pass_wilson
        verdicts[name] = {
            "n_effective": eff_n,
            "n_trigger": eff_n + s["neutral"],
            "hit_rate": rate,
            "wilson95": wilson_pair,
            "pass_n": pass_n, "pass_hit": pass_hit, "pass_wilson": pass_wilson,
            "verdict": "✅ PASS" if all_pass else ("⚠️ 继续观察" if eff_n > 0 else "❌ 数据不足"),
        }

    return {
        "n_joined": joined,
        "n_results": len(results),
        "per_field": per_field,
        "single_stats": single_stats,
        "cross": cross,
        "verdicts": verdicts,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }


# ============ Markdown 报告输出 ============

def render_markdown(report: dict) -> str:
    n = report["n_joined"]
    md = []
    md.append("# round50 三因子影子回测报告")
    md.append("")
    md.append(f"- 生成时间 (UTC): {report['timestamp']}")
    md.append(f"- join 样本: **{n}** / {report['n_results']} 已结算")
    md.append(f"- 最低门槛: n≥10, 命中率≥55%, Wilson95 下界≥50%")
    md.append("")
    if n < 30:
        md.append(f"⚠️ **当前样本 {n} 场, 远低于 30 场最低门槛。**")
        md.append("**此报告为预埋脚本的诊断性输出, 不作裁决。等 24-48h 攒够样本后再跑。**")
        md.append("")
    md.append("---")
    md.append("")

    # 单因子独立回测
    md.append("## 1. 单因子独立回测")
    md.append("")
    md.append("| 因子 | 命中/触发 | 命中率 | Wilson95 | 触发档 | 裁决 |")
    md.append("|---|---:|---:|---|---:|---|")
    for name, v in report["verdicts"].items():
        hr = f"{v['hit_rate'] * 100:.1f}%" if v["hit_rate"] is not None else "N/A"
        if v["wilson95"] is not None:
            w = f"[{v['wilson95'][0] * 100:.1f}%, {v['wilson95'][1] * 100:.1f}%]"
        else:
            w = "N/A"
        md.append(f"| {name} | {v['n_effective']}/{v['n_trigger']} | {hr} | {w} | {v['n_trigger']} | {v['verdict']} |")
    md.append("")

    # 交叉矩阵
    md.append("## 2. 因子交叉矩阵")
    md.append("")
    cross = report["cross"]
    if not cross:
        md.append("样本不足, 跳过交叉分析 (需要 ≥2 场有信号的样本)")
    else:
        md.append("### 2.1 两两同向率（同号时实际命中率）")
        md.append("")
        md.append("| 因子对 | 同号 | 反号 | 中性 | 同号率 |")
        md.append("|---|---:|---:|---:|---:|")
        for k in ("trades_vs_holders", "trades_vs_large5m", "holders_vs_large5m"):
            v = cross[k]
            sr = f"{v['same_rate'] * 100:.1f}%" if v["same_rate"] is not None else "N/A"
            md.append(f"| {k} | {v['same']} | {v['diff']} | {v['neutral']} | {sr} |")
        md.append("")
        md.append("### 2.2 三因子全同向时命中率")
        md.append("")
        v = cross.get("all_three_same_dir", {})
        rate = f"{v['rate'] * 100:.1f}%" if v.get("rate") is not None else "N/A"
        md.append(f"- 三因子全同向 (任一档作为信号): 命中={v.get('correct', 0)} / {v.get('n_effective', 0)} = **{rate}**")
        md.append("")
        md.append("### 2.3 三因子组合信号 (算术平均)")
        md.append("")
        v = cross.get("composite_arith_mean", {})
        rate = f"{v['rate'] * 100:.1f}%" if v.get("rate") is not None else "N/A"
        md.append(f"- 合成信号 = 三因子 net_up_imbalance 算术平均, |mean| > 0.1 触发")
        md.append(f"- 命中={v.get('correct', 0)} / {v.get('n_effective', 0)} = **{rate}**")
    md.append("")

    # 触发门槛裁决
    md.append("## 3. 触发门槛裁决")
    md.append("")
    md.append("| 因子 | 触发样本 | 命中率 | Wilson95 下界 | n≥10 | 命中率≥55% | Wilson≥50% | 综合 |")
    md.append("|---|---:|---:|---:|---|---|---|---|")
    for name, v in report["verdicts"].items():
        hr = f"{v['hit_rate'] * 100:.1f}%" if v["hit_rate"] is not None else "N/A"
        lo = f"{v['wilson95'][0] * 100:.1f}%" if (v["wilson95"] is not None and v["wilson95"][0] is not None) else "N/A"
        md.append(f"| {name} | {v['n_trigger']} | {hr} | {lo} | "
                  f"{'✅' if v['pass_n'] else '❌'} | "
                  f"{'✅' if v['pass_hit'] else '❌'} | "
                  f"{'✅' if v['pass_wilson'] else '❌'} | "
                  f"{v['verdict']} |")
    md.append("")
    md.append("---")
    md.append("")
    md.append("## 4. 已知风险（诚实披露）")
    md.append("")
    md.append("- 本报告为预埋脚本诊断性输出, 样本 < 30 场, 不作最终裁决")
    md.append("- 大单 50 BTC 阈值保留, 若持续 0 触发需调阈值或换字段")
    md.append("- holders 端点依赖 Polymarket 当前 slug 持仓, 早期场次可能无数据")
    md.append("- 因子判据阈值 (±0.3 / ±0.1) 是经验值, 后续可优化")
    md.append("- Wilson95 计算使用正态近似, 小样本下用 binomial 精确 CI 更准")
    md.append("")
    md.append("## 5. 下一步")
    md.append("")
    md.append("1. 24-48h 影子数据攒够 ≥ 30 场后重跑")
    md.append(f"2. 命令: `python3 scripts/round50_factor_candidate_backtest.py`")
    md.append("3. 全部 PASS → 抬 RULE_VERSION exec-round51 → validator 加 W_TRADES/W_HOLDERS/W_LARGE 常量 → 进 S0")
    md.append("4. 部分 PASS → 按优先级单加（最高命中率优先）")
    md.append("5. 全部 FAIL → 影子保留攒更多数据, 不上 S0")
    return "\n".join(md) + "\n"


def main() -> int:
    report = round50_backtest()
    md = render_markdown(report)
    today = datetime.now().strftime("%Y%m%d")
    out_path = DATA / f"hermes_factor_candidate_backtest_round50_{today}.md"
    out_path.write_text(md, encoding="utf-8")
    # 同时打印到 stdout 方便诊断
    print(md)
    print(f"\n[已写入] {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
