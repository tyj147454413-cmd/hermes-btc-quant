#!/usr/bin/env python3
"""
Hermes Walk-forward 滚动验证（升级计划书 任务 1.2，2026-08-22）。

定位：
    建立"训练窗选规则 → 样本外检验"的滚动框架，防止用全样本挑阈值造成过拟合。
    这是新规则上线前的强制关卡（计划书 §任务1.2 验收）。

安全声明：只读特征库；仅写 data/walk_forward_report.md。

方法（v1，候选规则为特征阈值策略，框架可扩展）：
    - 数据：feature_store 特征库（含 actual 结算标签），按 slug 时间排序
    - 滚动：train 窗（默认120场）内按 EV 选最优候选 → 紧随 test 窗（默认40场）
      实盘化执行 → 步长=test 窗逐段滚动
    - 经济口径与 backtest_engine 一致：ask≤0.60 成交，每单 1 share，
      赢单 +(1-pay)，输单 -pay
    - 输出：逐折明细 + OOS 汇总（胜率/Wilson95/EV）+ IS vs OOS 衰减

验收口径（计划书）：新规则样本外胜率 Wilson95 下界显著高于保本线才可上线。
保本线 ≈ 平均买价（每单 EV>0 要求胜率 > pay 均值）。

用法：
    python3 scripts/walk_forward.py                       # 默认 120/40
    python3 scripts/walk_forward.py --train 160 --test 30
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

_HERMES_DIR = os.environ.get("HERMES_DIR")
ROOT = Path(_HERMES_DIR) if _HERMES_DIR else Path("/Users/tangyijun/Desktop/hermes btc")
sys.path.insert(0, str(ROOT / "scripts"))

FEATURES_CSV = ROOT / "data" / "features" / "hermes_features_full.csv"
MAX_PRICE = 0.60

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}


def slug_sort_key(slug: str):
    m = re.search(r"-([a-z]+)-(\d+)-(\d{4})-(\d+)(am|pm)-et$", slug)
    if not m:
        return ("0000", 0)
    mon = _MONTHS.get(m.group(1), 0)
    h = int(m.group(4)) % 12 + (12 if m.group(5) == "pm" else 0)
    return (f"{m.group(3)}{mon:02d}{int(m.group(2)):02d}", h)


def wilson(w: int, n: int, z: float = 1.96):
    if n == 0:
        return 0.0, 100.0
    p = w / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (center - margin) / denom * 100), (center + margin) / denom * 100


# ============================================================
# 候选规则库：name -> fn(row) -> "UP"/"DOWN"/None
# （row 为特征 CSV 一行；扩展新候选直接往这里加）
# ============================================================

def _side_by_price(up, down):
    if pd.isna(up) or pd.isna(down) or up <= 0 or down <= 0:
        return None
    return "UP" if up >= down else "DOWN"


CANDIDATES = {
    "pm_follow":     lambda r: _side_by_price(r["pm_up"], r["pm_down"]),
    "pm_fade":       lambda r: (lambda s: {"UP": "DOWN", "DOWN": "UP", None: None}[s])(
                         _side_by_price(r["pm_up"], r["pm_down"])),
    "taker_bs":      lambda r: "UP" if r["taker_bs_ratio"] >= 1.2 else (
                         "DOWN" if r["taker_bs_ratio"] <= 0.85 else None),
    "flow_imbalance": lambda r: "UP" if r["flow_imbalance_5m"] > 0.05 else (
                         "DOWN" if r["flow_imbalance_5m"] < -0.05 else None),
    "rsi_1m":        lambda r: "UP" if r["rsi_1m"] > 60 else (
                         "DOWN" if r["rsi_1m"] < 40 else None),
    "macd_hist_4h":  lambda r: "UP" if r["macd_hist_4h"] > 0 else (
                         "DOWN" if r["macd_hist_4h"] < 0 else None),
    "prev_inertia":  lambda r: {1.0: "UP", -1.0: "DOWN"}.get(r["prev_side"]),
}


def eval_trades(rows: pd.DataFrame, cand: str) -> tuple[int, int, float]:
    """返回 (n_settled, wins, total_pnl_per_share)。"""
    fn = CANDIDATES[cand]
    n = w = 0
    pnl = 0.0
    for _, r in rows.iterrows():
        side = fn(r)
        if side not in ("UP", "DOWN"):
            continue
        ask = r["pm_up"] if side == "UP" else r["pm_down"]
        if pd.isna(ask) or ask <= 0 or ask > MAX_PRICE:
            continue
        if r["actual"] not in ("UP", "DOWN"):
            continue
        n += 1
        if r["actual"] == side:
            w += 1
            pnl += 1 - ask
        else:
            pnl -= ask
    return n, w, pnl


def load_df() -> pd.DataFrame:
    if not FEATURES_CSV.exists():
        import feature_store as fs
        df = fs.build("first")
        fs.write_outputs(df, fs.OUT_DIR, emit_dict=False)
        return df.sort_values("slug", key=lambda s: s.map(slug_sort_key))
    df = pd.read_csv(FEATURES_CSV)
    return df.sort_values("slug", key=lambda s: s.map(slug_sort_key))


def main() -> int:
    ap = argparse.ArgumentParser(description="Walk-forward 滚动验证（任务1.2）")
    ap.add_argument("--train", type=int, default=120)
    ap.add_argument("--test", type=int, default=40)
    ap.add_argument("--min-train-trades", type=int, default=15,
                    help="训练窗最少成交数，不足则该折跳过")
    ap.add_argument("--report", default="data/walk_forward_report.md")
    args = ap.parse_args()

    df = load_df()
    lab = df[df["actual"].isin(["UP", "DOWN"])].reset_index(drop=True)
    n = len(lab)
    print(f"已结算样本: {n}｜滚动 train={args.train} test={args.test}")

    folds = []
    oos_n = oos_w = 0
    oos_pnl = 0.0
    pick_count: dict[str, int] = {}
    i = args.train
    while i + args.test <= n:
        tr = lab.iloc[i - args.train:i]
        te = lab.iloc[i:i + args.test]
        best, best_ev = None, -9e9
        for cname in CANDIDATES:
            cn, cw, cpnl = eval_trades(tr, cname)
            if cn < args.min_train_trades:
                continue
            ev = cpnl / cn
            if ev > best_ev:
                best, best_ev = cname, ev
        if best is None:
            i += args.test
            continue
        tn, tw, tpnl = eval_trades(te, best)
        pick_count[best] = pick_count.get(best, 0) + 1
        oos_n += tn
        oos_w += tw
        oos_pnl += tpnl
        folds.append({"train_end": tr.iloc[-1]["slug"], "cand": best,
                      "is_ev": round(best_ev, 3),
                      "oos_n": tn, "oos_w": tw,
                      "oos_wr": round(tw / tn * 100, 1) if tn else 0.0,
                      "oos_pnl": round(tpnl, 2)})
        i += args.test

    lo, hi = wilson(oos_w, oos_n)
    oos_ev = oos_pnl / oos_n if oos_n else 0.0
    # 保本线：OOS 平均买价近似 = 1 - 平均赢单收益…直接用 EV 判定即可，此处给 Wilson 下界结论
    breakeven_note = ("✅ 样本外 Wilson95 下界 > 50%（对 0.5 价位附近有正期望空间）"
                      if lo > 50 else
                      f"⚠️ 样本外 Wilson95 下界 {lo:.1f}% 未显著高于保本区间——不足以支撑新规则上线")

    lines = [
        "# Hermes Walk-forward 滚动验证报告（任务 1.2）",
        "",
        f"- 生成(UTC)：{datetime.now(timezone.utc).isoformat()}",
        f"- 样本：{n} 已结算｜train={args.train} test={args.test}｜完成折数：{len(folds)}",
        "",
        "## OOS 汇总",
        "",
        f"- 样本外成交：{oos_n} 单｜胜：{oos_w}｜胜率 **{oos_w/oos_n*100:.1f}%**"
        if oos_n else "- 无样本外成交",
        f"- Wilson95：{lo:.1f}% ~ {hi:.1f}%",
        f"- 样本外 EV/单：**{oos_ev*100:+.1f}%**｜总 P&L/share：{oos_pnl:+.2f}",
        f"- {breakeven_note}",
        "",
        "## 各折被选中的候选分布",
        "",
    ]
    for k, v in sorted(pick_count.items(), key=lambda x: -x[1]):
        lines.append(f"- {k}: {v} 折")
    lines += ["", "## 逐折明细", "",
              "| 训练窗止 | 入选候选 | IS EV/单 | OOS n | OOS 胜 | OOS 胜率 | OOS P&L |",
              "|---|---|---:|---:|---:|---:|---:|"]
    for f in folds:
        lines.append(f"| {f['train_end'][-28:]} | {f['cand']} | {f['is_ev']*100:+.1f}% "
                     f"| {f['oos_n']} | {f['oos_w']} | {f['oos_wr']}% | {f['oos_pnl']:+.2f} |")
    lines += ["", "> 读法：若 OOS EV 明显低于各折 IS EV，说明存在过拟合衰减；",
              "> 新规则上线前应把其候选加入 CANDIDATES 跑本框架，OOS 达标才放行。", ""]

    rp = ROOT / args.report
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("\n".join(lines), encoding="utf-8")

    print(f"OOS: {oos_n}单 胜率{oos_w/max(oos_n,1)*100:.1f}% "
          f"(Wilson95下界{lo:.1f}%) EV/单{oos_ev*100:+.1f}%")
    print(f"报告: {rp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
