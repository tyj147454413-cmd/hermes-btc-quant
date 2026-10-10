#!/usr/bin/env python3
"""
Hermes 阈值敏感性分析（升级计划书 任务 1.3，2026-08-22）。

定位：
    对核心因子做分桶扫描，检验"阈值是否只在单点好、旁边都差"（疑似过拟合）。
    判据：因子值与 P(UP) 的关系若跨桶单调（Spearman |ρ| 高），说明该因子有稳定
    信息量；若只在某一桶突跳、相邻桶反向，标 ⚠️ 疑似过拟合。

安全声明：只读特征库；仅写 data/threshold_sensitivity_report.md。

方法：
    - 数据源：data/features/hermes_features_full.csv（feature_store.py 产物；
      缺文件时自动调 feature_store.build() 现场构建）
    - 每个数值因子按五分位分桶（自动跳过缺失率 >40% 的因子）
    - 每桶输出：n / P(actual=UP) / Wilson95
    - 全表附：桶均值的 Spearman ρ（桶序 vs UP率）
    - 计划书点名的贴线/G4 阈值用 deviation_pct 细分桶专项扫描

用法：
    python3 scripts/threshold_sensitivity.py                 # 默认因子集
    python3 scripts/threshold_sensitivity.py --features rsi_15m,taker_bs_ratio
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

_HERMES_DIR = os.environ.get("HERMES_DIR")
ROOT = Path(_HERMES_DIR) if _HERMES_DIR else Path("/Users/tangyijun/Desktop/hermes btc")
sys.path.insert(0, str(ROOT / "scripts"))

FEATURES_CSV = ROOT / "data" / "features" / "hermes_features_full.csv"

DEFAULT_FEATURES = [
    "deviation_pct", "volatility_5m",
    "taker_bs_ratio", "taker_bs_avg_1h", "flow_imbalance_5m",
    "rsi_1m", "rsi_5m", "rsi_15m", "rsi_1h", "rsi_4h",
    "macd_hist_15m", "macd_hist_4h",
    "pos24h_percentile", "fear_greed", "prev_flips", "long_short_ratio",
]

DEV_BINS = [-99, -0.15, -0.10, -0.05, 0, 0.05, 0.10, 0.15, 99]


def wilson(w: int, n: int, z: float = 1.96):
    if n == 0:
        return 0.0, 0.0
    p = w / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (center - margin) / denom * 100), (center + margin) / denom * 100


def spearman(x: list[float], y: list[float]) -> float:
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx, ry = ranks(x), ranks(y)
    n = len(x)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    return cov / math.sqrt(vx * vy) if vx > 0 and vy > 0 else 0.0


def load_df() -> pd.DataFrame:
    if FEATURES_CSV.exists():
        return pd.read_csv(FEATURES_CSV)
    import feature_store as fs
    df = fs.build("first")
    fs.write_outputs(df, fs.OUT_DIR, emit_dict=False)
    return df


def analyze_feature(df: pd.DataFrame, col: str, bins: int = 5):
    sub = df[[col, "actual"]].dropna()
    sub = sub[sub["actual"].isin(["UP", "DOWN"])]
    if len(sub) < 50:
        return None
    try:
        sub = sub.copy()
        sub["bucket"] = pd.qcut(sub[col], q=bins, duplicates="drop")
    except ValueError:
        return None
    rows = []
    for b, g in sub.groupby("bucket", observed=True):
        n = len(g)
        w = int((g["actual"] == "UP").sum())
        lo, hi = wilson(w, n)
        rows.append({"bucket": str(b), "mean_val": round(g[col].mean(), 5),
                     "n": n, "up_n": w, "up_rate": round(w / n * 100, 1),
                     "wilson": f"{lo:.0f}~{hi:.0f}%"})
    if len(rows) < 3:
        return None
    rho = spearman([r["mean_val"] for r in rows], [r["up_rate"] for r in rows])
    # 单调性检查：相邻桶方向翻转次数
    ups = [r["up_rate"] for r in rows]
    flips = sum(1 for i in range(len(ups) - 1)
                if (ups[i + 1] - ups[i]) * (ups[i] - (ups[i - 1] if i else ups[i])) < 0)
    verdict = ("✅ 单调性好" if abs(rho) >= 0.7 and flips <= 1
               else "⚠️ 非单调，疑似过拟合区" if flips >= 2
               else "△ 中性/弱单调")
    return {"col": col, "rows": rows, "spearman": round(rho, 2), "verdict": verdict}


def main() -> int:
    ap = argparse.ArgumentParser(description="阈值/因子敏感性分析（任务1.3）")
    ap.add_argument("--features", default=",".join(DEFAULT_FEATURES))
    ap.add_argument("--bins", type=int, default=5)
    ap.add_argument("--report", default="data/threshold_sensitivity_report.md")
    args = ap.parse_args()

    df = load_df()
    lab = df["actual"].isin(["UP", "DOWN"])
    df = df[lab]
    print(f"数据行（已结算）: {len(df)}")

    cols = [c.strip() for c in args.features.split(",") if c.strip() in df.columns]
    results = []
    for col in cols:
        miss = df[col].isna().mean()
        if miss > 0.4:
            print(f"- {col}: 缺失率 {miss*100:.0f}% >40%，跳过")
            continue
        r = analyze_feature(df, col, args.bins)
        if r:
            results.append(r)

    lines = [
        "# Hermes 阈值敏感性分析报告（任务 1.3）",
        "",
        f"- 生成(UTC)：{datetime.now(timezone.utc).isoformat()}",
        f"- 样本：{len(df)} 个已结算合约（pick=first 特征库）",
        f"- 分桶：每因子 {args.bins} 分位；判据见文件头注释",
        "",
    ]
    for r in sorted(results, key=lambda x: -abs(x["spearman"])):
        lines += [f"## {r['col']} —— {r['verdict']}（Spearman ρ={r['spearman']}）", "",
                  "| 桶 | 均值 | n | UP数 | P(UP) | Wilson95 |", "|---|---:|---:|---:|---:|---|"]
        for row in r["rows"]:
            lines.append(f"| {row['bucket']} | {row['mean_val']} | {row['n']} "
                         f"| {row['up_n']} | {row['up_rate']}% | {row['wilson']} |")
        lines.append("")

    # 专项：deviation_pct 细分桶（计划书点名 0.05%/0.10%/0.15% 贴线族阈值）
    dev = analyze_feature(df.assign(_b=pd.cut(df["deviation_pct"], DEV_BINS)),
                          "deviation_pct") if "deviation_pct" in df.columns else None
    lines += ["## deviation_pct 细分桶（贴线族 0.05/0.10/0.15 邻域）", "",
              "> 读法：观察 ±0.05/±0.10/±0.15 边界两侧 P(UP) 是否存在台阶；",
              "> 台阶陡且两侧样本充足 → 阈值有支撑；边界任一侧 n<20 → 该阈值统计不可靠。", ""]
    sub = df[["deviation_pct", "actual"]].dropna()
    sub = sub[sub["actual"].isin(["UP", "DOWN"])].copy()
    sub["b"] = pd.cut(sub["deviation_pct"], DEV_BINS)
    lines += ["| 偏差区间 | n | P(UP) |", "|---|---:|---:|"]
    for b, g in sub.groupby("b", observed=True):
        n = len(g)
        if n == 0:
            continue
        lines.append(f"| {b} | {n} | {(g['actual']=='UP').mean()*100:.1f}% |")
    lines.append("")

    rp = ROOT / args.report
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("\n".join(lines), encoding="utf-8")

    strong = [r["col"] for r in results if "✅" in r["verdict"]]
    weak = [r["col"] for r in results if "⚠️" in r["verdict"]]
    print(f"✅ 单调性好的因子: {', '.join(strong) or '无'}")
    print(f"⚠️ 疑似过拟合的因子: {', '.join(weak) or '无'}")
    print(f"报告: {rp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
