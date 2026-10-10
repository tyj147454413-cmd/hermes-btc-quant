#!/usr/bin/env python3
"""ML 数据集构建器（round45 前置研究）：快照 × 结算 → 特征矩阵 + 标签。

设计原则：
- 只用「决策时点已知」的信息（无未来函数）；标签 = 该小时 Binance 实际结算方向。
- 因子列 = extract_factors 机算输出；连续列 = 原始读数（比分级版信息量大）。
- 每 slug 取最新一份快照；与 hermes_results 实际结算 join；无法提取的场丢弃并计数。

用法：python3 scripts/ml_build_dataset.py   → 输出 data/ml_dataset.csv
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from signal_validator import extract_factors  # noqa: E402

SNAPSHOT_DIR = ROOT / "data" / "snapshots"
RESULTS_FILE = ROOT / "data" / "hermes_results.jsonl"
OUT_CSV = ROOT / "data" / "ml_dataset.csv"

FACTOR_KEYS = [
    "price_position", "taker_bs", "taker_bs_15m", "order_flow",
    "kline_15m", "kline_1h", "momentum_burst", "pm_momentum",
    "macro_trend", "seasonality", "short_bounce_factor",
    "wall_divergence", "open_micro",
]

CONTINUOUS_KEYS = [
    # (输出列名, 来源, 提取方式)
    ("dev_pct",        "inp",  lambda i, m: i.get("deviation_pct")),
    ("volatility_5m",  "inp",  lambda i, m: i.get("volatility_5m")),
    ("taker_bs_raw",   "md",   lambda i, m: (m.get("capital_flow") or {}).get("taker_bs_ratio")),
    ("taker_15m_raw",  "md",   lambda i, m: m.get("taker_15m_ratio")),
    ("imb_5m",         "md",   lambda i, m: ((m.get("capital_flow") or {}).get("order_flow") or {}).get("imbalance_5m")),
    ("imb_15m",        "md",   lambda i, m: ((m.get("capital_flow") or {}).get("order_flow") or {}).get("imbalance_15m")),
    ("up_price_delta", "md",   lambda i, m: (m.get("polymarket") or {}).get("up_price_delta")),
    ("pm_book_up",     "inp",  lambda i, m: i.get("book_up_mid")),          # 0-100 PM 共识价
    ("book_imbalance", "inp",  lambda i, m: i.get("book_imbalance")),
    ("taker_1h_buy_pct", "inp", lambda i, m: i.get("taker_1h_buy_pct")),
    ("rsi_4h",         "inp",  lambda i, m: i.get("rsi_4h")),
    ("bb_pos_4h",      "inp",  lambda i, m: i.get("bb_pos_4h")),
    ("macd_1h_hist",   "inp",  lambda i, m: i.get("macd_1h_hist")),
    ("macd_4h_hist",   "inp",  lambda i, m: i.get("macd_4h_hist")),
    ("hist_up_rate",   "inp",  lambda i, m: i.get("historical_up_rate")),
    ("minutes_to_settle", "inp", lambda i, m: i.get("minutes_to_settle")),
]


def load_actual() -> dict[str, str]:
    out = {}
    for line in RESULTS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("actual") in ("UP", "DOWN") and r.get("slug"):
            out[r["slug"]] = r["actual"]
    return out


def latest_snapshots() -> dict[str, Path]:
    slugs = defaultdict(list)
    for p in sorted(SNAPSHOT_DIR.glob("*.json")):
        slugs.setdefault(p.name.split("_202")[0], []).append(p)
    return {s: sorted(ps, key=lambda x: x.stat().st_mtime)[-1] for s, ps in slugs.items()}


def build_dataframe() -> tuple[pd.DataFrame, dict]:
    """构建特征矩阵（供本脚本 main 与 shadow_ml_predict 复用）。"""
    actual = load_actual()
    snaps = latest_snapshots()
    rows, skipped = [], {"no_result": 0, "extract_fail": 0}
    for slug, path in sorted(snaps.items()):
        if slug not in actual:
            skipped["no_result"] += 1
            continue
        try:
            md = json.loads(path.read_text())
            factors, inp, notes, mode = extract_factors(md)
        except Exception:
            skipped["extract_fail"] += 1
            continue

        row = {"slug": slug, "mode": mode}
        ct = md.get("collect_time_utc")
        row["collect_time"] = ct
        for k in FACTOR_KEYS:
            v = factors.get(k)
            row[k] = float(v) if v is not None else np.nan
        for name, src, fn in CONTINUOUS_KEYS:
            try:
                v = fn(inp, md) if src == "inp" else fn(inp, md)
            except Exception:
                v = None
            row[name] = float(v) if isinstance(v, (int, float)) else np.nan
        row["label"] = 1 if actual[slug] == "UP" else 0
        rows.append(row)
    df = pd.DataFrame(rows).sort_values("collect_time").reset_index(drop=True)
    return df, skipped


def main() -> int:
    df, skipped = build_dataframe()
    rows = None
    df.to_csv(OUT_CSV, index=False)
    print(f"数据集: {len(df)} 行 × {len(df.columns)} 列 → {OUT_CSV}")
    print(f"跳过: {skipped}")
    print(f"标签分布: UP={int(df.label.sum())} / DOWN={int((1 - df.label).sum())}"
          f"（UP占比 {df.label.mean()*100:.1f}%）")
    print(f"时间范围: {df.collect_time.min()} → {df.collect_time.max()}")
    print()
    print("各列非缺失率:")
    for c in FACTOR_KEYS + [n for n, _, _ in CONTINUOUS_KEYS]:
        s = f"{df[c].notna().mean()*100:5.1f}%"
        print(f"  {c:<20} {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
