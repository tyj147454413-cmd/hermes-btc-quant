#!/usr/bin/env python3
"""ML 走前验证评估器：逻辑回归 vs 现行规则引擎 vs 朴素基线（同一时间段公平对账）。

方法学要点：
- 严格按时间切分 train(60%)/val(20%)/test(20%)，绝不随机打散（时序数据随机切=泄漏）。
- 标准化只 fit 训练段，防泄漏。
- 正则强度 C 用验证段选，测试段只碰一次。
- 引擎基线 = replay_judge_full 两遍 extreme_wall 重放（与实盘校验同源）。
诚实声明：n≈330 的样本下 ±5pt 差异不显著；本脚本产出的是方法论与基础设施，
结论要等样本翻倍后再下。

用法：python3 scripts/ml_walkforward_eval.py [--use-pm]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from replay_common import replay_judge_full, wilson  # noqa: E402

DATASET = ROOT / "data" / "ml_dataset.csv"

# 全时段高可用特征（主实验）；PM 类后期特征在 --use-pm 子集实验启用
BASE_FEATURES = [
    "price_position", "taker_bs", "order_flow",
    "kline_15m", "kline_1h", "momentum_burst", "macro_trend",
    "seasonality", "short_bounce_factor", "wall_divergence", "open_micro",
    "dev_pct", "volatility_5m", "taker_bs_raw", "imb_5m", "imb_15m",
    "taker_1h_buy_pct", "rsi_4h", "bb_pos_4h",
    "macd_1h_hist", "macd_4h_hist", "hist_up_rate", "minutes_to_settle",
]
PM_FEATURES = ["pm_book_up", "up_price_delta", "book_imbalance", "pm_momentum"]


def engine_baseline(df_test: pd.DataFrame) -> tuple[np.ndarray, int]:
    """对 test 场逐个重放现行引擎，返回 (预测UP概率数组, 可重放数)。"""
    snaps_dir = ROOT / "data" / "snapshots"
    slugs = {}
    for p in sorted(snaps_dir.glob("*.json")):
        slugs.setdefault(p.name.split("_202")[0], []).append(p)
    latest = {s: sorted(ps, key=lambda x: x.stat().st_mtime)[-1] for s, ps in slugs.items()}

    preds, done = [], 0
    for slug in df_test.slug:
        p = latest.get(slug)
        pred = np.nan
        if p:
            try:
                md = json.loads(p.read_text())
                r = replay_judge_full(md)
                if r and r[0].final_direction:
                    pred = 1.0 if r[0].final_direction == "UP" else 0.0
                    done += 1
            except Exception:
                pass
        preds.append(pred)
    arr = np.array(preds)
    if np.isnan(arr).any():
        arr[np.isnan(arr)] = 0.5  # 少数无法重放的场按无信息 0.5
    return arr, done


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--use-pm", action="store_true", help="仅用含 PM 盘口的近期子集并加入 PM 特征")
    args = ap.parse_args()

    df = pd.read_csv(DATASET)
    df = df.sort_values("collect_time").reset_index(drop=True)

    if args.use_pm:
        need = BASE_FEATURES[:11] + PM_FEATURES + ["dev_pct", "volatility_5m", "imb_5m", "imb_15m"]
        df = df.dropna(subset=["pm_book_up"]).reset_index(drop=True)
        features = [f for f in dict.fromkeys(BASE_FEATURES + PM_FEATURES) if f not in ("taker_15m_raw",)]
        label_note = f"近期子集（有 PM 盘口）n={len(df)}"
    else:
        features = list(BASE_FEATURES)
        label_note = f"全量 n={len(df)}"

    X = df[features].fillna(0.0).to_numpy(dtype=float)
    y = df["label"].to_numpy(dtype=int)

    n = len(df)
    i_tr, i_va = int(n * 0.6), int(n * 0.8)
    tr, va, te = slice(0, i_tr), slice(i_tr, i_va), slice(i_va, n)
    print(f"=== 实验{'B' if args.use_pm else 'A'}: {label_note} | 特征 {len(features)} 个 ===")
    print(f"切分: train={i_tr} / val={i_va - i_tr} / test={n - i_va}（按时间顺序，不打散）")
    print(f"标签占比(train/val/test): {y[tr].mean():.2f} / {y[va].mean():.2f} / {y[te].mean():.2f}")

    mu, sd = X[tr].mean(0), X[tr].std(0)
    sd[sd == 0] = 1.0
    Z = (X - mu) / sd

    # C 网格在验证段选
    best = None
    for C in (0.01, 0.03, 0.1, 0.3, 1.0, 3.0, 10.0):
        clf = LogisticRegression(C=C, max_iter=2000).fit(Z[tr], y[tr])
        ll = log_loss(y[va], clf.predict_proba(Z[va])[:, 1], labels=[0, 1])
        acc = clf.score(Z[va], y[va])
        if best is None or ll < best[1]:
            best = (C, ll, acc, clf)
    C, vll, vacc, clf = best
    print(f"\n验证段选 C={C}（val logloss={vll:.3f}, val acc={vacc*100:.1f}%）")

    # 测试段
    p_lr = clf.predict_proba(Z[te])[:, 1]
    yhat_lr = (p_lr >= 0.5).astype(int)
    lr_acc = (yhat_lr == y[te]).mean()
    lr_ll = log_loss(y[te], p_lr, labels=[0, 1])

    # 基线1：测试段多数类
    majority = int(y[tr].mean() >= 0.5)
    maj_acc = (y[te] == majority).mean()

    # 基线2：引擎重放（同 test 场）
    eng_p, eng_done = engine_baseline(df.iloc[te].reset_index(drop=True))
    eng_acc = ((eng_p >= 0.5).astype(int) == y[te]).mean()

    lo, hi = wilson(int((yhat_lr == y[te]).sum()), len(y[te]))

    print(f"\n=== 测试段结果（{len(y[te])} 场）===")
    print(f"LR(round44.x 因子版) : acc={lr_acc*100:.1f}%  Wilson[{lo:.1f},{hi:.1f}]  logloss={lr_ll:.3f}")
    print(f"规则引擎(现行)       : acc={eng_acc*100:.1f}%  （可重放 {eng_done}/{len(y[te])}）")
    print(f"多数类基线           : acc={maj_acc*100:.1f}%")
    print(f"LR vs 引擎差异       : {(lr_acc-eng_acc)*100:+.1f}pt")

    # 系数表（标准化空间，可解释为「每 +1σ 对 log-odds 的贡献」）
    coefs = sorted(zip(features, clf.coef_[0]), key=lambda t: -abs(t[1]))
    print("\n=== LR 系数（|值|降序，前 12）===")
    for name, w in coefs[:12]:
        print(f"  {name:<20} {w:+.3f}")

    summary = {
        "experiment": "B" if args.use_pm else "A",
        "n": int(n), "n_test": int(len(y[te])),
        "C": C,
        "lr_test_acc": round(float(lr_acc), 4),
        "lr_wilson": [round(lo, 1), round(hi, 1)],
        "engine_test_acc": round(float(eng_acc), 4),
        "majority_acc": round(float(maj_acc), 4),
        "lr_logloss": round(float(lr_ll), 4),
        "coef_top": [[k, round(float(w), 4)] for k, w in coefs],
    }
    out = ROOT / "data" / f"ml_eval_summary{'_pm' if args.use_pm else ''}.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n摘要已存: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
