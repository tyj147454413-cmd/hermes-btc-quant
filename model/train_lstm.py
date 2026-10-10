#!/usr/bin/env python3
"""LSTM 辅助模型 walk-forward 训练（TRAINING_PLAN.md 第三章/第 2 步）。

输入设计：
    - 序列：决策时点前 168 根已收盘 1h K线 × 8 特征
      (ret, up, vr_1h, rsi_1h, bb_pos_1h, macd_1h_hist, roll_up_6, roll_ret_24)
      —— 由 backfill_data._precompute 统一口径计算，不含进行中 K线（无泄漏）。
    - 静态：dev_pct, momentum_burst（决策点对锚点偏离/动量爆发，来自 dataset）。
    结构：LSTM(8→64, 2层, dropout0.3) 末隐状态 ⊕ 静态2维 → FC(66→32) → 1 (logit)。

流程与 train_lgbm.py 相同：训练窗 12 个月 → 验证窗 2 个月（早停）
→ 测试窗 1 个月逐月滚动；标准化（逐特征 8+2 维）只在训练窗 fit（防泄漏）。

用法：
    .venv/Scripts/python model/train_lstm.py --tag dm35
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "model"))

import torch  # noqa: E402
import torch.nn as nn  # noqa: E402
from sklearn.metrics import roc_auc_score  # noqa: E402

from backfill_data import _precompute, fetch_klines  # noqa: E402
from train_lgbm import (DATA_DIR, REG_DIR, THRESHOLDS,  # noqa: E402
                        eval_block, month_add, m_start)

SEQ_FEATS = ["ret", "up", "vr_1h", "rsi_1h", "bb_pos_1h",
             "macd_1h_hist", "roll_up_6", "roll_ret_24"]
STATIC_FEATS = ["dev_pct", "momentum_burst"]
SEQ_LEN = 168
TRAIN_MONTHS, VAL_MONTHS = 12, 2
BS = 256


class BtcLstm(nn.Module):
    def __init__(self, n_seq: int = len(SEQ_FEATS), n_static: int = len(STATIC_FEATS),
                 hidden: int = 64, layers: int = 2, dropout: float = 0.3):
        super().__init__()
        self.lstm = nn.LSTM(n_seq, hidden, num_layers=layers, dropout=dropout,
                            batch_first=True)
        self.head = nn.Sequential(
            nn.Linear(hidden + n_static, 32), nn.ReLU(), nn.Dropout(dropout),
            nn.Linear(32, 1))

    def forward(self, seq, static):
        out, _ = self.lstm(seq)
        h = out[:, -1, :]                       # 末时刻隐状态
        return self.head(torch.cat([h, static], dim=1)).squeeze(-1)


def build_tensors(df: pd.DataFrame, k1h_feat: pd.DataFrame) -> dict:
    """从 1h 特征帧构建 N×168×8 序列张量 + 静态特征 + 标签（按 dataset 行对齐）。"""
    t1h = k1h_feat["open_time"].to_numpy()
    mat = np.column_stack([k1h_feat[f].to_numpy() for f in SEQ_FEATS]).astype(np.float32)
    anchors = (pd.to_datetime(df["anchor_time_utc"], utc=True).astype("int64") // 10**6).to_numpy()
    seq_idx = np.searchsorted(t1h, anchors)
    n = len(df)
    seqs = np.zeros((n, SEQ_LEN, len(SEQ_FEATS)), dtype=np.float32)
    for j, i in enumerate(seq_idx):
        lo = i - SEQ_LEN
        if lo >= 0:
            seqs[j] = mat[lo:i]
    static = np.nan_to_num(df[STATIC_FEATS].to_numpy(dtype=np.float32), nan=0.0)
    return {"seq": np.nan_to_num(seqs), "static": static, "y": df["label"].to_numpy(np.float32),
            "dead": df["dead_zone"].to_numpy(bool)}


def train_one(seq: np.ndarray, stat: np.ndarray, y: np.ndarray, tr_i, va_i,
              epochs: int, patience: int, seed: int, device,
              use_best: bool = True) -> tuple["BtcLstm", int, float]:
    """训练一个窗口。标准化已在外部完成。返回 (模型, best_epoch, best_val_auc)。"""
    torch.manual_seed(seed)
    model = BtcLstm().to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)
    lossf = nn.BCEWithLogitsLoss()
    seq_t = torch.from_numpy(seq); stat_t = torch.from_numpy(stat); y_t = torch.from_numpy(y)
    best_auc, best_state, best_ep, bad = -1.0, None, 0, 0
    for ep in range(1, epochs + 1):
        model.train()
        perm = torch.randperm(len(tr_i))
        for s in range(0, len(tr_i), BS):
            b = tr_i[perm[s:s + BS]]
            opt.zero_grad()
            with torch.autocast(device.type, enabled=device.type == "cuda"):
                loss = lossf(model(seq_t[b].to(device), stat_t[b].to(device)), y_t[b].to(device))
            loss.backward()
            opt.step()
        sched.step()
        model.eval()
        with torch.no_grad():
            pv = torch.sigmoid(model(seq_t[va_i].to(device), stat_t[va_i].to(device))
                               ).cpu().numpy()
        auc = roc_auc_score(y[va_i], pv)
        if auc > best_auc + 1e-4:
            best_auc, best_ep, bad = auc, ep, 0
            if use_best:
                best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
        else:
            bad += 1
            if bad >= patience:
                break
    if use_best and best_state:
        model.load_state_dict(best_state)
    return model, best_ep, best_auc


def predict(model, seq: np.ndarray, stat: np.ndarray, idx, device) -> np.ndarray:
    model.eval()
    with torch.no_grad():
        return torch.sigmoid(model(torch.from_numpy(seq[idx]).to(device),
                                   torch.from_numpy(stat[idx]).to(device))).cpu().numpy()


def main() -> int:
    ap = argparse.ArgumentParser(description="LSTM walk-forward 训练评估")
    ap.add_argument("--tag", default="dm35", help="数据集后缀（dataset_<tag>.parquet）")
    ap.add_argument("--epochs", type=int, default=50)
    ap.add_argument("--patience", type=int, default=8)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    suffix = f"_{args.tag}" if args.tag else ""
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"[lstm] device={device} tag={args.tag}")

    df = pd.read_parquet(DATA_DIR / f"dataset{suffix}.parquet").reset_index(drop=True)
    n = len(df)
    a0 = pd.Timestamp(df["anchor_time_utc"].iloc[0])
    start_ms = int((a0 - pd.Timedelta(days=30)).timestamp() * 1000)
    end_ms = int((pd.Timestamp(df["anchor_time_utc"].iloc[-1]) + pd.Timedelta(hours=1))
                 .timestamp() * 1000)
    k1h = fetch_klines("BTCUSDT", "1h", start_ms, end_ms, None)
    if k1h.empty:
        print("[lstm] [X] 1h K线拉取失败")
        return 1
    pc = _precompute(k1h, k1h, k1h, k1h, pd.DataFrame(), pd.DataFrame())
    ds = build_tensors(df, pc["k1h"])
    print(f"[lstm] 样本 {n}（死区 {int(ds['dead'].sum())}），序列 {ds['seq'].shape}")

    months = pd.to_datetime(df["anchor_time_utc"], utc=True)
    start, end = months.iloc[0], months.iloc[-1]
    first_test = month_add(start, TRAIN_MONTHS + VAL_MONTHS)
    win_rows, all_preds, best_eps = [], [], []
    t0 = datetime.now()
    w = 0
    test_m = first_test
    while test_m <= m_start(end):
        nxt = month_add(test_m, 1)
        tr = np.where((months >= month_add(test_m, -TRAIN_MONTHS - VAL_MONTHS))
                      & (months < month_add(test_m, -VAL_MONTHS)) & ~ds["dead"])[0]
        va = np.where((months >= month_add(test_m, -VAL_MONTHS)) & (months < test_m)
                      & ~ds["dead"])[0]
        te = np.where((months >= test_m) & (months < nxt) & ~ds["dead"])[0]
        if len(tr) < 1000 or len(va) < 100 or len(te) < 50:
            print(f"[lstm] {test_m.date()} 窗口样本不足，跳过")
            test_m = nxt
            continue
        w += 1
        # 标准化只在训练窗 fit：序列逐特征(8维，跨时间共享)、静态(2维)
        flat = ds["seq"][tr].reshape(-1, len(SEQ_FEATS))
        mu_s, sd_s = flat.mean(axis=0), flat.std(axis=0) + 1e-8
        mu_q, sd_q = ds["static"][tr].mean(axis=0), ds["static"][tr].std(axis=0) + 1e-8
        seq_w = np.nan_to_num((ds["seq"] - mu_s) / sd_s).astype(np.float32)
        stat_w = ((ds["static"] - mu_q) / sd_q).astype(np.float32)

        model, best_ep, _ = train_one(seq_w, stat_w, ds["y"], tr, va,
                                      args.epochs, args.patience, args.seed, device)
        best_eps.append(best_ep)
        pt = predict(model, seq_w, stat_w, te, device)
        yte = ds["y"][te]
        res = eval_block(pd.Series(yte), pd.Series(pt))
        res["window"] = str(test_m.date()); res["best_epoch"] = best_ep
        win_rows.append(res)
        all_preds.append(pd.DataFrame({"window": test_m.date(),
                                       "dt": df["anchor_time_utc"].iloc[te].values,
                                       "label": yte, "p_lstm": pt}))
        print(f"[lstm] 窗口 {test_m.date()}: n={res['n']} AUC={res['auc']:.3f} "
              f"Brier={res['brier']:.4f} acc={res['acc']:.3f} "
              f"hit@0.65={res['hit@0.65']:.3f}(n={res['n@0.65']}) ep={best_ep}")
        test_m = nxt

    if not win_rows:
        print("[lstm] [X] 无可评估窗口")
        return 1

    oos = pd.concat(all_preds, ignore_index=True)
    overall = eval_block(oos["label"], oos["p_lstm"])
    base = float(oos["label"].mean())
    print("\n========== LSTM OOS 汇总 ==========")
    print(f"样本 {overall['n']}（{w} 窗，耗时 {(datetime.now() - t0).total_seconds():.0f}s）")
    print(f"AUC {overall['auc']:.3f} | Brier {overall['brier']:.4f} "
          f"(基准 {base * (1 - base):.4f}) | acc {overall['acc']:.3f}")
    for th in THRESHOLDS:
        print(f"阈值 {th}: 出手 {overall[f'n@{th}']}，hit {overall[f'hit@{th}']:.3f}")

    oos.to_csv(DATA_DIR / f"wf_predictions_lstm{suffix}.csv", index=False)
    (DATA_DIR / f"wf_report_lstm{suffix}.json").write_text(json.dumps(
        {"generated_at": datetime.now(timezone.utc).isoformat(), "tag": args.tag,
         "seq_feats": SEQ_FEATS, "static_feats": STATIC_FEATS, "seq_len": SEQ_LEN,
         "windows": win_rows, "oos": overall, "base_rate": base,
         "best_epochs": best_eps}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")

    # 最终模型：全量（剔死区）重训，轮数=各窗 best_epoch 中位数，不用早停 best-state
    REG_DIR.mkdir(parents=True, exist_ok=True)
    n_ep = int(np.median(best_eps))
    tr_all = np.where(~ds["dead"])[0]
    mu_s = ds["seq"][tr_all].reshape(-1, len(SEQ_FEATS)).mean(axis=0)
    sd_s = ds["seq"][tr_all].reshape(-1, len(SEQ_FEATS)).std(axis=0) + 1e-8
    mu_q = ds["static"][tr_all].mean(axis=0)
    sd_q = ds["static"][tr_all].std(axis=0) + 1e-8
    seq_w = np.nan_to_num((ds["seq"] - mu_s) / sd_s).astype(np.float32)
    stat_w = ((ds["static"] - mu_q) / sd_q).astype(np.float32)
    model, _, _ = train_one(seq_w, stat_w, ds["y"], tr_all, tr_all[:BS],
                            n_ep, 10**6, args.seed, device, use_best=False)
    torch.save({"state_dict": model.state_dict(),
                "config": {"seq_feats": SEQ_FEATS, "static_feats": STATIC_FEATS,
                           "seq_len": SEQ_LEN, "tag": args.tag, "epochs": n_ep,
                           "scaler_seq_mean": mu_s.tolist(), "scaler_seq_std": sd_s.tolist(),
                           "scaler_static_mean": mu_q.tolist(),
                           "scaler_static_std": sd_q.tolist()},
                "oos_auc": overall["auc"],
                "trained_at": datetime.now(timezone.utc).isoformat()},
               REG_DIR / f"lstm{suffix}_v1.pt")
    print(f"\n[lstm] 最终模型已保存 → {REG_DIR / f'lstm{suffix}_v1.pt'}（{n_ep} epochs）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
