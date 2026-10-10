#!/usr/bin/env python3
"""特征剔除诊断（ablation study）：量化 dev_pct/momentum_burst 的"增量预测力"占比。

三组对比，全部复用 train_lgbm.py 的 walk-forward 口径
（12m 训练 / 2m 验证 / 1m 测试逐月滚动，early_stop=200，isotonic 验证窗校准）：
  A) full          全特征基线（应复现 wf_report_dmXX.json 的 OOS AUC，验证口径一致）
  B) ablated       剔除 dev_pct + momentum_burst（剩 ~30 特征）
  C) momentum_only 仅 dev_pct + momentum_burst（2 特征"单飞"上界）

另附零成本统计（quick_stats）：dev_pct/momentum_burst 与 label 的
Pearson 相关 + 单特征 AUC，直接刻画"决策点信息占标签信息的比例"。

用法：
    .venv/Scripts/python model/ablate_features.py --dms 15,35
产出：
    model/data/ablation_report.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import roc_auc_score

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "model"))

from train_lgbm import (DATA_DIR, PARAMS, EARLY_STOP, TRAIN_MONTHS, VAL_MONTHS,
                        eval_block, month_add, m_start)  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ABLATE = ["dev_pct", "momentum_burst"]
# 动量代理全家桶（二阶深度剔除）：决策点已实现的价格路径 / 动量变换。
# dev_pct 虽被删，模型会找同义词替身（kline_15m/open_dir/taker_5m 等），
# 本组把这些"同义替换"也一并剔除，测真正与决策点路径正交的信息残量。
DEEP_ABLATE = [
    "dev_pct", "momentum_burst", "kline_15m", "kline_1h", "open_dir",
    "open_range_pct", "volatility_5m", "pos24h_percentile", "range_24h_pct",
    "taker_1h_buy_pct", "taker_5m_last", "taker_15m", "imb_5m", "imb_15m",
    "rsi_1h", "bb_pos_1h", "macd_1h_hist",
]
# decision_min -> (dataset 后缀, manifest 后缀)
DATASETS = {15: "", 35: "_dm35"}


def run_wf(df: pd.DataFrame, keep: list[str], tag: str):
    """与 train_lgbm.main 完全一致的 walk-forward 循环。返回 (oos, windows, last_model)。"""
    t0 = time.time()
    start, end = df["dt"].iloc[0], df["dt"].iloc[-1]
    first_test = month_add(start, TRAIN_MONTHS + VAL_MONTHS)
    all_p, win_rows = [], []
    last_model = None
    test_m = first_test
    while test_m <= m_start(end):
        next_m = month_add(test_m, 1)
        tr_end, va_end, te_end = month_add(test_m, -VAL_MONTHS), test_m, next_m
        tr = df[(df["dt"] >= month_add(test_m, -TRAIN_MONTHS - VAL_MONTHS)) & (df["dt"] < tr_end)]
        va = df[(df["dt"] >= tr_end) & (df["dt"] < va_end)]
        te = df[(df["dt"] >= va_end) & (df["dt"] < te_end)]
        if len(tr) < 1000 or len(va) < 100 or len(te) < 50:
            print(f"  [{tag}] {test_m.date()} window too small, skip "
                  f"(tr={len(tr)} va={len(va)} te={len(te)})")
            test_m = next_m
            continue
        model = lgb.train(
            PARAMS, lgb.Dataset(tr[keep], tr["label"]),
            valid_sets=[lgb.Dataset(va[keep], va["label"])],
            callbacks=[lgb.early_stopping(EARLY_STOP, verbose=False)])
        last_model = model
        pv = model.predict(va[keep], num_iteration=model.best_iteration)
        iso = IsotonicRegression(out_of_bounds="clip").fit(pv, va["label"])
        raw_te = model.predict(te[keep], num_iteration=model.best_iteration)
        pt = iso.predict(raw_te)
        res = eval_block(te["label"].reset_index(drop=True), pd.Series(pt))
        res["window"] = str(test_m.date())
        win_rows.append(res)
        all_p.append(pd.DataFrame({"dt": te["dt"].values, "label": te["label"].values,
                                   "p_cal": pt}))
        print(f"  [{tag}] {test_m.date()}: n={res['n']} AUC={res['auc']:.3f} "
              f"Brier={res['brier']:.4f} hit@0.65={res['hit@0.65']:.3f}(n={res['n@0.65']})")
        test_m = next_m
    if not win_rows:
        return None, None, None
    oos = pd.concat(all_p, ignore_index=True)
    overall = eval_block(oos["label"], oos["p_cal"])
    print(f"  [{tag}] OOS: n={overall['n']} AUC={overall['auc']:.3f} "
          f"Brier={overall['brier']:.4f} acc={overall['acc']:.3f} "
          f"({time.time() - t0:.0f}s)")
    return overall, win_rows, last_model


def quick_stats(df: pd.DataFrame) -> dict:
    """不训练：特征与 label 的 Pearson 相关 + 单特征 AUC。"""
    out = {}
    for c in ABLATE:
        s = df[[c, "label"]].dropna()
        if len(s) < 100 or s["label"].nunique() < 2:
            continue
        out[f"corr_{c}"] = round(float(s[c].corr(s["label"], method="pearson")), 4)
        out[f"auc_{c}_alone"] = round(float(roc_auc_score(s["label"], s[c])), 4)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dms", default="15,35", help="逗号分隔 decision_min（现有数据集仅 15/35）")
    ap.add_argument("--groups", default="",
                    help="逗号分隔要跑的组（空=全跑）：full,ablated,momentum_only,deep_ablated")
    args = ap.parse_args()

    report = {}
    for dm in [int(x) for x in args.dms.split(",") if x.strip()]:
        if dm not in DATASETS:
            print(f"[!] dm{dm} 无数据集文件，跳过（现有：{sorted(DATASETS)}）")
            continue
        suffix = DATASETS[dm]
        df = pd.read_parquet(DATA_DIR / f"dataset{suffix}.parquet")
        manifest = json.loads(
            (DATA_DIR / f"manifest{suffix}.json").read_text(encoding="utf-8"))
        feats = [c for c in manifest["feature_cols"] if c in df.columns]
        keep_base = [c for c in feats if df[c].notna().mean() >= 0.5]
        df = df[df["dead_zone"] == 0].reset_index(drop=True)
        df["dt"] = pd.to_datetime(df["anchor_time_utc"], utc=True)
        print(f"\n===== dm{dm} | {len(df)} rows x {len(keep_base)} features "
              f"({df['dt'].iloc[0].date()} -> {df['dt'].iloc[-1].date()}) =====")

        entry = {"decision_min": dm, "n_features_base": len(keep_base),
                 "quick_stats": quick_stats(df), "groups": {}}
        groups = {
            "full": keep_base,
            "ablated": [c for c in keep_base if c not in ABLATE],
            "momentum_only": [c for c in ABLATE if c in keep_base],
            "deep_ablated": [c for c in keep_base if c not in DEEP_ABLATE],
        }
        want = [g.strip() for g in args.groups.split(",") if g.strip()]
        for gname, keep in groups.items():
            if want and gname not in want:
                continue
            if not keep:
                print(f"  [{gname}] no features left, skip")
                continue
            print(f"--[{gname}] {len(keep)} features--")
            overall, win_rows, last_model = run_wf(df, keep, gname)
            if overall is None:
                print(f"  [{gname}] no valid window")
                continue
            g = {"features": keep, "oos": overall, "windows": win_rows}
            if last_model is not None:
                imp = sorted(zip(keep, last_model.feature_importance("gain")),
                             key=lambda x: -x[1])
                g["top10_importance"] = [[k, float(v)] for k, v in imp[:10]]
                print(f"  [{gname}] top5 gain: " + ", ".join(
                    f"{k}={v:.0f}" for k, v in imp[:5]))
            entry["groups"][gname] = g
        report[f"dm{dm}"] = entry

    out_p = DATA_DIR / "ablation_report.json"
    out_p.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                     encoding="utf-8")
    print(f"\n[OK] -> {out_p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())