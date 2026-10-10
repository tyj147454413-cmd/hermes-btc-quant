#!/usr/bin/env python3
"""盘感代理特征试点训练（影子 A/B，不覆盖正式模型、不进交易链路）。

问题：CLOB 盘口快照（book_snapshots）刚起步、历史不可回溯，无法直接训练"真盘感"。
方案：用影子记录 ml_shadow_predictions.jsonl 里每个小时的 Polymarket 盘口价演变
      （pm_up/pm_down，每 5 分钟一个决策点）构造代理盘感特征：
        pm_up_dec   决策点最新 pm_up
        pm_delta    决策点 vs 该小时最早的 pm_up 演变（压价/拉升过程量化）
      这两列仅最近有 shadow 覆盖的小时非空，其余历史行特征缺失（NaN）。
      LightGBM 原生处理缺失：模型学到"有盘感信息时如何利用、缺失时退化为原逻辑"。

流程（每个 decision_min 独立）：
    1. 读 model/data/dataset{suffix}.parquet 最近 N 天样本（去死区）。
    2. 按行当小时 slug 索引 shadow 盘口演变 → 追加 pm_up_dec / pm_delta 两列。
    3. 随机分层切分 80/20（试点阶段非 walk-forward：盘感特征仅在最近 ~2 天有值，
       时间切分会让训练窗全 NaN 无法学习，故用随机切分验证特征预测力，明确标注局限）。
    4. LGBM 小样本参数训练 + 早停 + Platt 校准 → 落盘 registry/lgbm{suffix}_gs_v1.txt
       + metadata{suffix}_gs.json（不覆盖正式 v1）。
       （9/5 审计：isotonic 在小样本上阶梯化过拟合——15.9% 输出饱和到 1.0、
        7.7% 卡在 0.5，EV 过滤失真；已换成 Platt=LogisticRegression(raw_score)，
        2 参数天然抗过拟合，calibration 元数据带 kind 标记供 predict_hourly 还原）

   同时在同一验证子集上加载正式 lgbm{suffix}_v1.txt 对比：
        · 全体验证行 / pm 有值子集 / |pm_delta|>=0.03 强盘感情形 的 acc 与 hit@0.65
        · 输出 gs 相对 v1 的方向修正数（盘感与模型冲突时以谁为准）

用法：
    .venv/Scripts/python model/train_lgbm_gs.py                    # 默认 7 个主要 dm
    .venv/Scripts/python model/train_lgbm_gs.py --dms 35,40        # 指定决策点
    .venv/Scripts/python model/train_lgbm_gs.py --days 30 --seed 7
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import train_test_split

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "model"))

from hermes_common import ET, build_slug  # noqa: E402

DATA_DIR = ROOT / "model" / "data"
REG_DIR = ROOT / "model" / "registry"
SHADOW = ROOT / "data" / "ml_shadow_predictions.jsonl"

# 盘感代理特征列（末尾追加）
PM_FEATS = ["pm_up_dec", "pm_delta"]

# 小样本专用参数：相比 12 个月训练窗，60 天 ~1400 行必须收紧树复杂度防过拟合
GS_PARAMS = dict(
    objective="binary", metric="auc", verbosity=-1,
    num_leaves=16, max_depth=5,
    learning_rate=0.05, n_estimators=900,
    min_child_samples=40,
    feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1,
    reg_alpha=0.3, reg_lambda=2.0,
    seed=42,
)
EARLY_STOP = 100


def load_shadow_index() -> dict[str, dict[int, float]]:
    """slug → {decision_min: pm_up}（一次性加载索引，避免逐行全文件扫描）。

    同 slug+dm 多条记录取最后写入（决策点越晚越接近推理时盘口）。
    """
    idx: dict[str, dict[int, float]] = {}
    if not SHADOW.exists():
        return idx
    for line in SHADOW.read_text(encoding="utf-8-sig").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        pm = r.get("pm_up")
        if r.get("slug") is None or pm is None:
            continue
        dmv = r.get("decision_min")
        if dmv is None:
            continue
        idx.setdefault(r["slug"], {})[int(dmv)] = float(pm)
    return idx


def build_pm_row(slug: str, dm: int, pm_idx: dict[str, dict[int, float]]) -> dict:
    """按 fetch_pm_feats 同口径构造 pm 特征行（无数据 → NaN）。"""
    pts = pm_idx.get(slug) or {}
    rows = {k: v for k, v in pts.items() if k <= dm}
    if not rows:
        return {"pm_up_dec": np.nan, "pm_delta": np.nan}
    dm_dec = max(rows)
    dm_early = min(rows)
    return {"pm_up_dec": rows[dm_dec],
            "pm_delta": round(rows[dm_dec] - rows[dm_early], 4)}


def slot_suffix(dm: int) -> str:
    return "" if dm == 15 else f"_dm{dm}"


def brier(y, p) -> float:
    return float(np.mean((p - y) ** 2))


def block_stats(y, p) -> dict:
    out = {"n": int(len(y))}
    if len(y) == 0:
        return out
    out["acc"] = round(float(((p >= 0.5).astype(int) == y).mean()), 4)
    for th in (0.55, 0.60, 0.65):
        m = (p >= th) | (p <= 1 - th)
        n_th, hits = int(m.sum()), float((y[m] == (p[m] >= th)).sum())
        out[f"n@{th}"] = n_th
        out[f"hit@{th}"] = round(hits / n_th, 4) if n_th else None
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="盘感代理特征试点训练（影子 A/B）")
    ap.add_argument("--dms", default="15,35,40,45,50,55", help="决策点列表，逗号分隔")
    ap.add_argument("--days", type=int, default=60, help="训练取最近 N 天样本（默认 60）")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--skip-v1", action="store_true", help="跳过与正式 v1 的对比")
    ap.add_argument("--no-momentum", action="store_true",
                    help="A/B 变体：剔除 dev_pct/momentum_burst 训练 gs_nomtm（独立 alpha 对照组）")
    args = ap.parse_args()
    dms = [int(x) for x in args.dms.split(",") if x.strip()]
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)

    print(f"[gs] 盘感代理特征试点 | dms={dms} 近{args.days}天 seed={args.seed}")
    pm_idx = load_shadow_index()
    print(f"[gs] shadow 索引: {len(pm_idx)} 个 slug（pm_up 样本）")

    for dm in dms:
        suffix = slot_suffix(dm)
        tag = f"dm{dm}" if dm != 15 else "dm15"
        df = pd.read_parquet(DATA_DIR / f"dataset{suffix}.parquet")
        df["dt"] = pd.to_datetime(df["anchor_time_utc"], utc=True)
        df = df[df["dead_zone"] == 0].copy()
        df = df[df["dt"] >= cutoff].reset_index(drop=True)
        n0 = len(df)

        feats = [c for c in manifest_features(suffix) if c in df.columns]
        # pm 列强制保留（有值率仅 ~5%，不能走原高缺失过滤逻辑）
        keep = [c for c in feats if df[c].notna().mean() >= 0.5]
        keep = keep + PM_FEATS
        # A/B 变体：剔除决策点动量特征（市场已定价信号），保留其余结构
        if args.no_momentum:
            keep = [c for c in keep if c not in ("dev_pct", "momentum_burst")]
        dropped = [c for c in feats if c not in keep]

        # join 盘感代理特征
        # BUG FIX: anchor_time_utc 是 UTC 字符串，build_slug 要求 ET 时间，
        # 必须先转时区，否则 00:00-04:59 UTC 的窗口 slug 错一天/错小时 → pm join 丢失。
        def _join_pm(s: str) -> dict:
            try:
                dt = pd.Timestamp(s)
                if dt.tzinfo is None:
                    dt = dt.tz_localize("UTC")
                slug = build_slug(dt.to_pydatetime().astimezone(ET))
            except Exception:
                return {"pm_up_dec": np.nan, "pm_delta": np.nan}
            return build_pm_row(slug, dm, pm_idx)
        pm_cols = df["anchor_time_utc"].map(_join_pm)
        df["pm_up_dec"] = pm_cols.map(lambda d: d["pm_up_dec"]).astype(float)
        df["pm_delta"] = pm_cols.map(lambda d: d["pm_delta"]).astype(float)
        n_pm = int(df["pm_delta"].notna().sum())
        print(f"\n[gs] {tag}: {n0} 样本（近{args.days}天） × {len(keep)} 特征 "
              f"（pm 有值 {n_pm}）剔除 {dropped}")

        y = df["label"].to_numpy()
        if y.sum() == 0 or y.sum() == len(y):
            print(f"[gs] {tag}: 标签单边，跳过")
            continue

        # 分层切分：pm 有值行单独切片，保证验证集始终含盘感样本（随机切分可能全落训练集）
        def _split_part(pos: np.ndarray, global_y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
            if len(pos) == 0:
                return np.array([], dtype=int), np.array([], dtype=int)
            if len(pos) == 1:
                return pos, np.array([], dtype=int)      # 单样本全进训练
            s = global_y[pos]
            strat = s if s.min() != s.max() else None    # 标签单边时退化为随机
            a, b = train_test_split(pos, test_size=0.2, stratify=strat, random_state=args.seed)
            return a, b

        pm_row_m = ~np.isnan(df["pm_delta"].to_numpy())
        tr_pm, va_pm = _split_part(np.flatnonzero(pm_row_m), y)
        tr_na, va_na = _split_part(np.flatnonzero(~pm_row_m), y)
        tr_idx = np.concatenate([tr_pm, tr_na])
        va_idx = np.concatenate([va_pm, va_na])
        Xtr, Xva, ytr, yva = (df.loc[tr_idx, keep], df.loc[va_idx, keep],
                              df["label"].to_numpy()[tr_idx], df["label"].to_numpy()[va_idx])

        model = lgb.train(
            GS_PARAMS, lgb.Dataset(Xtr, ytr),
            valid_sets=[lgb.Dataset(Xva, yva)],
            callbacks=[lgb.early_stopping(EARLY_STOP, verbose=False)])
        pv = model.predict(Xva, num_iteration=model.best_iteration)
        # Platt 校准（9/5 审计替换 isotonic：小样本下 isotonic 阶梯化失真，
        # 输出 15.9% 饱和到 1.0）；逻辑回归 2 参数，正则化，抗尾部分布过拟合
        platt = LogisticRegression().fit(pv.reshape(-1, 1), yva)
        p_gs = pd.Series(platt.predict_proba(pv.reshape(-1, 1))[:, 1])
        # 校准器参数（predict_hourly 按 kind 还原：platt → sigmoid(coef*x+intercept)）
        cal_points = {"kind": "platt",
                      "coef": [float(platt.coef_[0][0])],
                      "intercept": float(platt.intercept_[0])}
        overall_gs = block_stats(pd.Series(yva), p_gs)

        # —— 与正式 v1 对比 ——
        va_delta = df.loc[va_idx, "pm_delta"].to_numpy()
        pm_mask = ~np.isnan(va_delta)                 # numpy 布尔（按位置过滤，避开 pandas 索引对齐坑）
        strong_mask = np.abs(va_delta) >= 0.03        # 强盘感情形：|pm_delta|>=0.03
        comp = None
        if not args.skip_v1 and (REG_DIR / f"lgbm{suffix}_v1.txt").exists():
            meta_v1 = json.loads((REG_DIR / f"metadata{suffix}.json").read_text(encoding="utf-8"))
            b1 = lgb.Booster(model_file=str(REG_DIR / f"lgbm{suffix}_v1.txt"))
            raw1 = b1.predict(df.loc[va_idx, meta_v1["features"]],
                              num_iteration=b1.num_trees())
            p_v1 = pd.Series(np.interp(raw1, meta_v1["calibration"]["x"],
                                       meta_v1["calibration"]["y"]))
            rows = pd.DataFrame({"y": yva, "p_v1": p_v1.values, "p_gs": p_gs.values})
            pos = np.flatnonzero(pm_mask)
            rows_pm = rows.iloc[pos]
            rows_strong = rows.iloc[np.flatnonzero(pm_mask & strong_mask)]
            flip = np.flatnonzero((rows["p_v1"].to_numpy() >= 0.5)
                                  != (rows["p_gs"].to_numpy() >= 0.5))
            flip_gs_wins = int(((rows["p_gs"].to_numpy()[flip] >= 0.5)
                                == rows["y"].to_numpy()[flip]).sum())
            flip_v1_wins = int(((rows["p_v1"].to_numpy()[flip] >= 0.5)
                                == rows["y"].to_numpy()[flip]).sum())
            def _blk(d: pd.DataFrame) -> dict:
                return {"v1": block_stats(pd.Series(d["y"].to_numpy()), pd.Series(d["p_v1"].to_numpy())),
                        "gs": block_stats(pd.Series(d["y"].to_numpy()), pd.Series(d["p_gs"].to_numpy()))}
            comp = {
                "v_all": _blk(rows),
                "v_pm": _blk(rows_pm),
                "n_pm_val": int(pm_mask.sum()),
                "flip_gs_wins": flip_gs_wins,
                "flip_v1_wins": flip_v1_wins,
            }
            comp["v_pm_strong"] = _blk(rows_strong) if len(rows_strong) else None

        # —— 落盘 ——
        vsx = "_nomtm" if args.no_momentum else ""   # A/B 变体名变后缀
        REG_DIR.mkdir(parents=True, exist_ok=True)
        model.save_model(str(REG_DIR / f"lgbm{suffix}_gs{vsx}_v1.txt"))
        imp = sorted(zip(keep, model.feature_importance("gain")), key=lambda x: -x[1])
        meta = {"trained_at": datetime.now(timezone.utc).isoformat(),
                "decision_min": dm, "tag": f"gs{vsx}", "model_basis": "random_split_80_20",
                "train_rows": int(len(tr_idx)), "pm_rows_train": int(pd.notna(
                    df.loc[tr_idx, "pm_delta"]).sum()),
                "days": args.days, "seed": args.seed,
                "params": GS_PARAMS, "n_boost_rounds": int(model.best_iteration),
                "calibration": cal_points, "features": keep,
                "validation": {"n": int(len(yva)), "pm_rows": int(pd.notna(
                    df.loc[va_idx, "pm_delta"]).sum()), "overall": overall_gs,
                               "compare_v1": comp},
                "top10_importance": imp[:10]}
        (REG_DIR / f"metadata{suffix}_gs{vsx}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        (DATA_DIR / f"gs_report{suffix}{vsx}.json").write_text(
            json.dumps({"dm": dm, "tag": tag, "generated_at": datetime.now(timezone.utc).isoformat(),
                        "validation_n": int(len(yva)), "pm_rows": n_pm,
                        "overall_gs": overall_gs, "compare_v1": comp, "top10": imp[:10]},
                       ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        print(f"[gs] {tag}: 验证 {overall_gs['n']} 行 | acc={overall_gs['acc']} "
              f"| hit@0.65={overall_gs.get('hit@0.65')}")
        if comp:
            v1a, gsa = comp["v_all"]["v1"]["acc"], comp["v_all"]["gs"]["acc"]
            p1, pg = comp["v_pm"]["v1"], comp["v_pm"]["gs"]
            if comp["n_pm_val"]:
                print(f"[gs] {tag}: 全体 acc v1={v1a} gs={gsa} | "
                      f"pm 子集 n={comp['n_pm_val']} v1 acc={p1['acc']}(hit@0.65={p1.get('hit@0.65')}) "
                      f"gs acc={pg['acc']}(hit@0.65={pg.get('hit@0.65')})")
                print(f"[gs] {tag}: 方向翻转 盘感胜/模型胜 = "
                      f"{comp['flip_gs_wins']}/{comp['flip_v1_wins']}")
            else:
                print(f"[gs] {tag}: 全体 acc v1={v1a} gs={gsa} | pm 子集无验证样本（n=0）")
        print(f"[gs] {tag}: Top5 gain = "
              + ", ".join(f"{k}={v:.0f}" for k, v in imp[:5]))
    print("\n[gs] 完成。产物: registry/lgbm*_gs_v1.txt + metadata*_gs.json + data/gs_report*.json")
    return 0


def manifest_features(suffix: str) -> list[str]:
    p = DATA_DIR / f"manifest{suffix}.json"
    return json.loads(p.read_text(encoding="utf-8")).get("feature_cols", [])


if __name__ == "__main__":
    sys.exit(main())