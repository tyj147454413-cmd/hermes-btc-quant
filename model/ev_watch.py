#!/usr/bin/env python3
"""EV 仪表盘（模型可交易价值的裁决尺）。

消费 data/ml_shadow_predictions.jsonl（predict_hourly.py 影子输出），
回答唯一的问题：模型的校准概率 vs PM 盘口价的背离，能不能产生正 EV。

口径说明（与 compute_ev 完全一致）：
    fair_up = P(up)                                # 盈亏平衡买价
    ev_up   = P*(1-pm_up) - (1-P)*pm_up            # 每 $1 本金的期望收益
    ev_down = (1-P)*(1-pm_down) - P*pm_down
正 EV 才值得出手；累计 paper PnL 只在提供结算标签时计算。

用法：
    .venv/Scripts/python model/ev_watch.py                          # 汇总 EV 分布
    .venv/Scripts/python model/ev_watch.py --min-ev 0.02            # 只统计 |EV|>=0.02
    .venv/Scripts/python model/ev_watch.py --labels data/settle.jsonl   # + 命中率/paper PnL
    .venv/Scripts/python model/ev_watch.py --dm 35                  # 只看实盘口径决策点

labels 文件格式（每行一条，key 为 slug）：
    {"slug": "bitcoin-up-or-down-january-5-2026-3pm-et", "label": 1}
产出：
    model/data/ev_watch_report.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA_DIR = ROOT / "model" / "data"
SHADOW_DEFAULT = ROOT / "data" / "ml_shadow_predictions.jsonl"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def load_shadow(path: Path, dm: int | None, min_ev: float):
    """读影子记录，返回带 pm 盘口价的最新记录（按 slug 取最后一行，去重）。"""
    if not path.exists():
        return {}
    rows = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("pm_up") is None and r.get("pm_down") is None:
            continue
        if dm is not None and r.get("decision_min") != dm:
            continue
        # 同一 slug 按 decision_min 取最大（实时/补录行混合时也不依赖文件顺序）
        rdm = r.get("decision_min", -1)
        if r.get("slug") not in rows or rdm >= rows[r["slug"]].get("decision_min", -1):
            rows[r["slug"]] = r
    return rows


def load_labels(path: Path) -> dict[str, int]:
    if not path or not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        try:
            r = json.loads(line)
            if r.get("slug") is not None and r.get("label") in (0, 1):
                out[slug_lc(r["slug"])] = int(r["label"])
        except json.JSONDecodeError:
            continue
    return out


def slug_lc(s: str) -> str:
    return s.strip().lower()


def main() -> int:
    ap = argparse.ArgumentParser(description="EV 仪表盘裁决尺")
    ap.add_argument("--shadow", default=str(SHADOW_DEFAULT))
    ap.add_argument("--labels", default="", help="结算标签 jsonl（slug+label）")
    ap.add_argument("--dm", type=int, default=None,
                    help="只看某决策点（实盘口径=35）")
    ap.add_argument("--min-ev", type=float, default=0.0,
                    help="只统计 |EV|>=min_ev 的机会")
    args = ap.parse_args()

    rows = load_shadow(Path(args.shadow), args.dm, args.min_ev)
    if not rows:
        print(f"[X] 无可用影子样本（{args.shadow}）")
        return 1

    labels = load_labels(Path(args.labels)) if args.labels else {}
    n = len(rows)
    # BUG FIX: --min-ev 之前从未生效——过滤 |EV| < min_ev 的机会后再统计
    evs_up = [r["ev_up"] for r in rows.values()
              if r.get("ev_up") is not None and abs(r["ev_up"]) >= args.min_ev]
    evs_down = [r["ev_down"] for r in rows.values()
                if r.get("ev_down") is not None and abs(r["ev_down"]) >= args.min_ev]
    pos_up = sum(1 for e in evs_up if e > 0)
    pos_down = sum(1 for e in evs_down if e > 0)

    # EV 值与盘口价差（fair vs pm_up）分布
    gaps_up = [r["fair_price_up"] - r["pm_up"] for r in rows.values()
               if r.get("fair_price_up") is not None and r.get("pm_up") is not None]
    p_models = [r["p_model"] for r in rows.values() if r.get("p_model") is not None]

    rep = {
        "n_shadow_universe": n,
        "with_labels": bool(labels),
        "min_ev_filter": args.min_ev,
        "decision_min_filter": args.dm,
        "ev_up_mean": round(sum(evs_up) / len(evs_up), 4) if evs_up else None,
        "ev_up_pos_ratio": round(pos_up / len(evs_up), 4) if evs_up else None,
        "ev_down_mean": round(sum(evs_down) / len(evs_down), 4) if evs_down else None,
        "fair_minus_pm_mean": round(sum(gaps_up) / len(gaps_up), 4) if gaps_up else None,
        "p_model_mean": round(sum(p_models) / len(p_models), 4) if p_models else None,
    }

    print(f"\n===== EV 仪表盘 | 影子样本 {n}（决策点过滤={args.dm}，|EV|>={args.min_ev}）=====")
    print(f"买 UP 机会: 均值 EV={rep['ev_up_mean']} | 正 EV 占比 {rep['ev_up_pos_ratio']} "
          f"({pos_up}/{len(evs_up)})")
    print(f"买 DOWN 机会: 均值 EV={rep['ev_down_mean']} | 正 EV 占比 "
          f"{round(pos_down/len(evs_down),4) if evs_down else None} "
          f"({pos_down}/{len(evs_down)})")
    print(f"模型 fair - 盘口价 平均差 = {rep['fair_minus_pm_mean']} "
          f"（>0 = 模型认为盘口低估 UP）")
    print(f"p_model 均值 = {rep['p_model_mean']}（>0.5 视市场共识偏涨）")

    # —— 有结算标签：命中率 + paper PnL ——
    if labels:
        hit_bins: dict[str, list[int]] = defaultdict(list)
        pnl_all, pnl_ev = 0.0, 0.0
        n_hit, n_decided = 0, 0
        for r in rows.values():
            lb = labels.get(slug_lc(r["slug"]))
            if lb is None:
                continue
            # BUG FIX: 方向口径——老记录无 direction_model 时回退历史字段 direction
            dm_ = r.get("direction_model") or r.get("direction") or "UP"
            side = "UP" if dm_ == "UP" else "DOWN"
            up = r.get("pm_up")
            pm = up if side == "UP" else r.get("pm_down")
            if pm is None:
                continue
            n_decided += 1
            # paper PnL（按"买入 1 张、成本 pm"口径，与 compute_ev 的 EV 定义一致：
            # 赢 = 1-pm，输 = -pm，即 EV = p*(1-pm)-(1-p)*pm）
            # label=1 表示"该小时 UP 赢"：买 UP 时 label=1 为赢；
            # 买 DOWN 时 label=0（即 UP 输）才为赢。
            stake = 1.0
            won = (side == "UP" and lb == 1) or (side == "DOWN" and lb == 0)
            if won:
                win = stake * (1 - pm)
            else:
                win = -stake * pm
            pnl_all += win
            ev = r.get("ev_up") if side == "UP" else r.get("ev_down")
            if ev is not None and ev > 0:
                pnl_ev += win
            hit = 1 if won else 0
            n_hit += hit
            p_m = r.get("p_model") or 0
            bin_k = ">=0.65" if p_m >= 0.65 else (">=0.55" if p_m >= 0.55 else "<0.55")
            hit_bins[bin_k].append(hit)
        rep["paper_pnl_all_usd"] = round(pnl_all, 2)
        rep["paper_pnl_ev_only_usd"] = round(pnl_ev, 2)
        rep["decided_n"] = n_decided
        rep["hit_rate"] = round(n_hit / n_decided, 4) if n_decided else None
        rep["hit_by_conf"] = {k: round(sum(v) / len(v), 4) for k, v in hit_bins.items()}
        print(f"\n—— 结算口径（{n_decided} 个已结算窗口）——")
        print(f"命中率 = {rep['hit_rate']}")
        for k, v in rep["hit_by_conf"].items():
            print(f"  置信度 {k}: hit={v} (n={len(hit_bins[k])})")
        print(f"paper PnL（全出手, $1 本金/次）= {rep['paper_pnl_all_usd']}")
        print(f"paper PnL（仅正 EV 出手）      = {rep['paper_pnl_ev_only_usd']}")

    out = DATA_DIR / "ev_watch_report.json"
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[OK] -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())