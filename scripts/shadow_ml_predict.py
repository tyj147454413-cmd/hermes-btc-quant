#!/usr/bin/env python3
"""Phase 1 影子预测器：每小时对最新快照输出 LR 的 P(UP)，只记录不下单。

流程：
  1) ml_build_dataset.build_dataframe() 重建全量已结算数据集（expanding window）
  2) TimeSeriesSplit 选 C → 全量重训逻辑回归（特征=实验 A 的 23 列，fillna(0)）
  3) 对最新快照提取同一特征向量 → P(UP)
  4) 追加 data/ml_shadow_predictions.jsonl（同 slug+collect_time 去重）

设计约束：
- 失败只写 stderr/日志、非零退出，绝不影响交易链路（本脚本不写任何信号文件）。
- 模型版本可追溯：每行记录 n_train / C / 特征数 / strategy_version。

用法：.venv/bin/python scripts/shadow_ml_predict.py   （cron :33 每小时）
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss
from sklearn.model_selection import TimeSeriesSplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from ml_build_dataset import CONTINUOUS_KEYS, FACTOR_KEYS, build_dataframe  # noqa: E402
from signal_validator import extract_factors  # noqa: E402

OUT_JSONL = ROOT / "data" / "ml_shadow_predictions.jsonl"
STRATEGY_VERSION = "round44.1-shadow"

# 与 walkforward 实验A 同一特征集（PM 盘口列覆盖率 >50% 前不启用，避免分布漂移）
FEATURES = [
    "price_position", "taker_bs", "order_flow",
    "kline_15m", "kline_1h", "momentum_burst", "macro_trend",
    "seasonality", "short_bounce_factor", "wall_divergence", "open_micro",
    "dev_pct", "volatility_5m", "taker_bs_raw", "imb_5m", "imb_15m",
    "taker_1h_buy_pct", "rsi_4h", "bb_pos_4h",
    "macd_1h_hist", "macd_4h_hist", "hist_up_rate", "minutes_to_settle",
]
assert set(FEATURES) <= set(FACTOR_KEYS + [n for n, _, _ in CONTINUOUS_KEYS])


def _value_of(key: str, factors: dict, inp: dict, md: dict):
    """特征取值：因子列取 factors；其余从 inp / 原始嵌套路径取（与 ml_build_dataset 对齐）。"""
    if key in factors:
        return factors.get(key)
    if key == "taker_bs_raw":
        return (md.get("capital_flow") or {}).get("taker_bs_ratio")
    if key == "imb_5m":
        return ((md.get("capital_flow") or {}).get("order_flow") or {}).get("imbalance_5m")
    if key == "imb_15m":
        return ((md.get("capital_flow") or {}).get("order_flow") or {}).get("imbalance_15m")
    return inp.get(key)


def main() -> int:
    # ---- 1) 训练数据 ----
    df, skipped = build_dataframe()
    X = df[FEATURES].fillna(0.0).to_numpy(dtype=float)
    y = df["label"].to_numpy(dtype=int)
    mu, sd = X.mean(0), X.std(0)
    sd[sd == 0] = 1.0
    Z = (X - mu) / sd

    # ---- 2) TimeSeriesSplit 选 C → 全量拟合 ----
    best_c, best_ll = 0.1, float("inf")
    for C in (0.01, 0.03, 0.1, 0.3, 1.0, 3.0):
        lls = []
        tscv = TimeSeriesSplit(n_splits=4)
        for tr_i, va_i in tscv.split(Z):
            if len(va_i) < 5:
                continue
            clf = LogisticRegression(C=C, max_iter=2000).fit(Z[tr_i], y[tr_i])
            lls.append(log_loss(y[va_i], clf.predict_proba(Z[va_i])[:, 1], labels=[0, 1]))
        ll = float(np.mean(lls)) if lls else float("inf")
        if ll < best_ll:
            best_c, best_ll = C, ll
    clf = LogisticRegression(C=best_c, max_iter=2000).fit(Z, y)

    # ---- 3) 最新快照预测 ----
    snaps = sorted((ROOT / "data" / "snapshots").glob("*.json"))
    target = snaps[-1]
    md = json.loads(target.read_text())
    slug = md.get("slug") or target.name.split("_202")[0]
    collect_time = md.get("collect_time_utc")

    # 去重：同 slug 已预测过则跳过
    if OUT_JSONL.exists():
        seen = []
        for line in OUT_JSONL.read_text(encoding="utf-8").splitlines()[-400:]:
            try:
                seen.append(json.loads(line).get("slug"))
            except json.JSONDecodeError:
                pass
        if slug in seen:
            print(f"[skip] {slug} 已在影子记录中")
            return 0

    factors, inp, notes, mode = extract_factors(md)
    vec_vals = []
    for k in FEATURES:
        v = _value_of(k, factors, inp, md)
        vec_vals.append(float(v) if isinstance(v, (int, float)) else 0.0)
    z = (np.array([vec_vals], dtype=float) - mu) / sd
    p_up = float(clf.predict_proba(z)[0, 1])

    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "slug": slug,
        "collect_time": collect_time,
        "mode": mode,
        "p_up": round(p_up, 4),
        "pred_dir": "UP" if p_up >= 0.5 else "DOWN",
        "model": {"kind": "logreg", "C": best_c, "cv_logloss": round(best_ll, 4),
                  "n_train": int(len(y)), "n_features": len(FEATURES)},
        "strategy_version": STRATEGY_VERSION,
    }
    with OUT_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"[shadow] {slug}  P(UP)={p_up:.3f} → {rec['pred_dir']}  "
          f"(n_train={len(y)}, C={best_c}, cv_logloss={best_ll:.3f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
