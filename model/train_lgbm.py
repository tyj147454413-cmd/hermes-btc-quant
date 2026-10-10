#!/usr/bin/env python3
"""LightGBM walk-forward 训练与评估（TRAINING_PLAN.md 第 2 步）。

流程：
    1. 加载 model/data/dataset.parquet，剔除死区样本（|ret_hour| < deadzone）。
    2. Walk-forward 切分：训练窗 12 个月 → 验证窗 2 个月（早停+isotonic 校准）
       → 测试窗 1 个月，测试窗逐月滚动，拼接全部测试窗得真实 OOS 评估。
    3. 指标：AUC、Brier、准确率、分方向准确率、决策阈值 hit rate（0.60/0.65/0.70）。
    4. 全量数据重训最终模型 → model/registry/（模型 + metadata）。
    5. OOS 逐窗预测落盘 model/data/wf_predictions.csv（供后续分析/融合）。

验收门槛（TRAINING_PLAN.md 第五章）：
    OOS AUC ≥0.58 接受，≥0.62 良好；>0.65 需查泄漏。

用法：
    .venv/Scripts/python model/train_lgbm.py                 # 默认死区 0.1
    .venv/Scripts/python model/train_lgbm.py --keep-dead     # 保留死区样本
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import roc_auc_score

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "model" / "data"
REG_DIR = ROOT / "model" / "registry"

PARAMS = dict(
    objective="binary", metric="auc", verbosity=-1,
    num_leaves=31, max_depth=6,
    learning_rate=0.03, n_estimators=2000,
    min_child_samples=150,
    feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1,
    reg_alpha=0.1, reg_lambda=1.0,
    seed=42,
)
EARLY_STOP = 200
TRAIN_MONTHS, VAL_MONTHS = 12, 2
THRESHOLDS = (0.60, 0.65, 0.70)


def month_add(d: pd.Timestamp, n: int) -> pd.Timestamp:
    y, m = d.year + (d.month - 1 + n) // 12, (d.month - 1 + n) % 12 + 1
    return pd.Timestamp(year=y, month=m, day=1, tz="UTC")


def m_start(d: pd.Timestamp) -> pd.Timestamp:
    """所在月第一天（UTC）。"""
    return pd.Timestamp(year=d.year, month=d.month, day=1, tz="UTC")


def brier(y, p) -> float:
    return float(np.mean((p - y) ** 2))


def eval_block(y, p) -> dict:
    """单窗评估：AUC + 校准后 Brier + 阈值决策指标。"""
    out = {"n": int(len(y)), "auc": float(roc_auc_score(y, p)) if y.nunique() > 1 else np.nan,
           "brier": brier(y, p), "acc": float(((p >= 0.5).astype(int) == y).mean())}
    up, dn = y[p > 0.5], y[p <= 0.5]
    out["acc_up"] = float(up.mean()) if len(up) else np.nan
    out["acc_down"] = float((dn == 0).mean()) if len(dn) else np.nan
    for th in THRESHOLDS:
        mask = (p >= th) | (p <= 1 - th)
        n_th, hits = int(mask.sum()), float((y[mask] == (p[mask] >= th)).sum())
        out[f"n@{th}"] = n_th
        out[f"hit@{th}"] = hits / n_th if n_th else np.nan
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="LightGBM walk-forward 训练评估")
    ap.add_argument("--keep-dead", action="store_true", help="保留死区样本")
    ap.add_argument("--tag", default="", help="产出后缀（如 dm35 → dataset_dm35.parquet, lgbm_dm35_v1.txt）")
    args = ap.parse_args()

    suffix = f"_{args.tag}" if args.tag else ""
    df = pd.read_parquet(DATA_DIR / f"dataset{suffix}.parquet")
    manifest = json.loads((DATA_DIR / f"manifest{suffix}.json").read_text(encoding="utf-8"))
    decision_min = manifest.get("decision_min", 15)
    print(f"[lgbm] 决策时点: H+{decision_min}min")
    feats = [c for c in manifest["feature_cols"] if c in df.columns]
    # 自动剔除高缺失列（如 fear_greed 直连不可达时全 NaN）
    keep = [c for c in feats if df[c].notna().mean() >= 0.5]
    dropped = [c for c in feats if c not in keep]
    if dropped:
        print(f"[lgbm] 剔除高缺失特征: {dropped}")

    if not args.keep_dead:
        df = df[df["dead_zone"] == 0].reset_index(drop=True)
    df["dt"] = pd.to_datetime(df["anchor_time_utc"], utc=True)
    print(f"[lgbm] 样本 {len(df)} 行 × {len(keep)} 特征 "
          f"({df['dt'].iloc[0].date()} → {df['dt'].iloc[-1].date()})")

    # ---------------- walk-forward ----------------
    start, end = df["dt"].iloc[0], df["dt"].iloc[-1]
    first_test = month_add(start, TRAIN_MONTHS + VAL_MONTHS)
    all_preds, win_rows = [], []
    t0 = datetime.now()
    w = 0
    test_m = first_test
    while test_m <= m_start(end):
        next_m = month_add(test_m, 1)
        tr_end, va_end, te_end = (month_add(test_m, -VAL_MONTHS), test_m, next_m)
        tr = df[(df["dt"] >= month_add(test_m, -TRAIN_MONTHS - VAL_MONTHS)) & (df["dt"] < tr_end)]
        va = df[(df["dt"] >= tr_end) & (df["dt"] < va_end)]
        te = df[(df["dt"] >= va_end) & (df["dt"] < te_end)]
        if len(tr) < 1000 or len(va) < 100 or len(te) < 50:
            print(f"[lgbm] {test_m.date()} 窗口样本不足，跳过 (tr={len(tr)} va={len(va)} te={len(te)})")
            test_m = next_m
            continue
        w += 1
        model = lgb.train(
            PARAMS, lgb.Dataset(tr[keep], tr["label"]),
            valid_sets=[lgb.Dataset(va[keep], va["label"])],
            callbacks=[lgb.early_stopping(EARLY_STOP, verbose=False)])
        # isotonic 校准（在验证窗拟合）
        pv = model.predict(va[keep], num_iteration=model.best_iteration)
        iso = IsotonicRegression(out_of_bounds="clip").fit(pv, va["label"])
        raw_te = model.predict(te[keep], num_iteration=model.best_iteration)
        pt = iso.predict(raw_te)
        # 部署物 = 最后一个 walk-forward 窗口的 (模型, 校准器)：与评估口径完全同源，
        # 保证推理端概率与 wf 评估逐位一致；模型新鲜度由每月滚动重训维持
        last_model, last_iso, last_train_rows, last_best_iter = model, iso, len(tr), model.best_iteration
        res = eval_block(te["label"].reset_index(drop=True), pd.Series(pt))
        res["window"] = str(test_m.date()); res["best_iter"] = model.best_iteration
        win_rows.append(res)
        all_preds.append(pd.DataFrame({"window": test_m.date(), "dt": te["dt"].values,
                                       "label": te["label"].values,
                                       "p_raw": raw_te, "p_cal": pt}))
        print(f"[lgbm] 窗口 {test_m.date()}: n={res['n']} AUC={res['auc']:.3f} "
              f"Brier={res['brier']:.4f} acc={res['acc']:.3f} "
              f"hit@0.65={res['hit@0.65'] if res['hit@0.65'] == res['hit@0.65'] else float('nan'):.3f}"
              f"(n={res['n@0.65']}) iter={model.best_iteration}")
        test_m = next_m

    if not win_rows:
        print("[lgbm] [X] 无可评估窗口（数据范围不足 15 个月）")
        return 1

    oos = pd.concat(all_preds, ignore_index=True)
    overall = eval_block(oos["label"], oos["p_cal"])
    base_rate = float(oos["label"].mean())
    # 部署校准器 = 最后窗口的验证窗 isotonic（与部署模型同源），落盘供 predict_hourly 插值
    cal_points = {"x": [round(v, 6) for v in last_iso.X_thresholds_],
                  "y": [round(v, 6) for v in last_iso.y_thresholds_]}
    elapsed = (datetime.now() - t0).total_seconds()

    print("\n========== OOS 汇总（全部测试窗拼接） ==========")
    print(f"样本 {overall['n']}（{w} 个窗口，训练耗时 {elapsed:.0f}s）")
    print(f"AUC {overall['auc']:.3f} | Brier {overall['brier']:.4f} "
          f"(基准频率 Brier {base_rate * (1 - base_rate):.4f}) | acc {overall['acc']:.3f}")
    print(f"分方向 acc: UP {overall['acc_up']:.3f} / DOWN {overall['acc_down']:.3f}")
    for th in THRESHOLDS:
        print(f"阈值 {th}: 出手 {overall[f'n@{th}']} 次，hit {overall[f'hit@{th}']:.3f}")
    verdict = ("良好" if overall["auc"] >= 0.62 else
               "接受" if overall["auc"] >= 0.58 else "未达标")
    leak_warn = " [!] AUC>0.65 建议排查泄漏" if overall["auc"] > 0.65 else ""
    print(f"结论: AUC {overall['auc']:.3f} → {verdict}{leak_warn}")

    oos.to_csv(DATA_DIR / f"wf_predictions{suffix}.csv", index=False)
    (DATA_DIR / f"wf_report{suffix}.json").write_text(json.dumps(
        {"generated_at": datetime.now(timezone.utc).isoformat(),
         "decision_min": decision_min, "tag": args.tag,
         "params": PARAMS, "early_stop": EARLY_STOP, "train_months": TRAIN_MONTHS,
         "val_months": VAL_MONTHS, "windows": win_rows, "oos": overall,
         "base_rate": base_rate, "features": keep, "dropped_features": dropped,
         "keep_dead": args.keep_dead}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")

    # ---------------- 最终模型 = 最后一个 walk-forward 窗口的 (模型, 校准器) ----------------
    REG_DIR.mkdir(parents=True, exist_ok=True)
    final, n_boost = last_model, last_best_iter
    final.save_model(str(REG_DIR / f"lgbm{suffix}_v1.txt"))
    imp = sorted(zip(keep, final.feature_importance("gain")), key=lambda x: -x[1])
    meta = {"trained_at": datetime.now(timezone.utc).isoformat(),
            "decision_min": decision_min, "tag": args.tag,
            "model_basis": "last_walkforward_window",
            "train_rows": int(last_train_rows),
            "calibration": cal_points,
            "n_boost_rounds": n_boost,
            "features": keep,
            "params": PARAMS, "oos_auc": overall["auc"],
            "top10_importance": imp[:10]}
    (REG_DIR / f"metadata{suffix}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[lgbm] 最终模型已保存 → {REG_DIR / f'lgbm{suffix}_v1.txt'}"
          f"（最后窗口模型，{last_train_rows} 行训练，{n_boost} 轮）")
    print("[lgbm] Top10 特征重要性(gain): " +
          ", ".join(f"{k}={v:.0f}" for k, v in imp[:10]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
