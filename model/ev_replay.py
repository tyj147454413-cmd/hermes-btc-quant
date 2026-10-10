#!/usr/bin/env python3
"""历史 EV 预演回测（TRAINING_PLAN 第 4 步预演）。

对每个小时：决策点特征(dataset_dmNN) -> 模型 P(up) -> 与决策时点 PM 盘口对比
-> 虚拟下单 -> 用真实结算算已实现 P&L。

数据源：
  - model/data/dataset{,_dm35}.parquet   决策点特征 + label（部署模型为最后
    一个 walk-forward 窗口，训练截止上月末，因此回测月为真 OOS）
  - data/pm_prices/pm_YYYYMM.jsonl       PM 盘口快照（约 5-15 分钟粒度）
  - model/registry/lgbm*_v1.txt + metadata*.json（含 isotonic 校准插值点）

口径说明（与 bot 一致）：
  - 方向 = 模型 P>=0.5 买 Up，否则买 Down；edge = 模型方向概率 - PM 价
  - edge >= 阈值 且 价格 <= cap 才下单；每单 $1 名义本金
  - 已实现 P&L: 赢 = 1-price，输 = -price；model_ev 用模型概率算（对比用）

用法：
    python model/ev_replay.py --decision-min 15
    python model/ev_replay.py --decision-min 35 --thresholds 0.03,0.05,0.10
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "model" / "data"
REG_DIR = ROOT / "model" / "registry"
PM_DIR = ROOT / "data" / "pm_prices"
HOUR_MS = 3_600_000


def load_pm_snapshots() -> dict[str, list[tuple[int, float, float]]]:
    """slug -> [(ts_ms, up, dn)] 按时间升序。"""
    out: dict[str, list[tuple[int, float, float]]] = {}
    for f in sorted(PM_DIR.glob("pm_*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = datetime.fromisoformat(r["ts_utc"]).timestamp() * 1000
            slug, up, dn = r["slug_cur"], r["up_cur"], r["dn_cur"]
            if slug and up is not None and dn is not None:
                out.setdefault(slug, []).append((int(ts), float(up), float(dn)))
    for v in out.values():
        v.sort()
    return out


def pm_at(snap: list[tuple[int, float, float]], t_dec: int) -> tuple[float, float, int] | None:
    """决策时点盘口：取 ts <= t_dec+60s 的最后一个快照。返回 (up, dn, age_min)。"""
    best = None
    for ts, up, dn in snap:
        if ts <= t_dec + 60_000:
            best = (ts, up, dn)
        else:
            break
    if best is None:
        return None
    return best[1], best[2], int((t_dec - best[0]) / 60_000)


def main() -> int:
    ap = argparse.ArgumentParser(description="历史 EV 预演回测")
    ap.add_argument("--decision-min", type=int, required=True, choices=(15, 35))
    ap.add_argument("--thresholds", default="0.02,0.03,0.05,0.08,0.10")
    ap.add_argument("--price-cap", type=float, default=0.80)
    args = ap.parse_args()
    dm = args.decision_min
    suffix = "" if dm == 15 else f"_dm{dm}"
    ths = [float(x) for x in args.thresholds.split(",")]

    ds_path = DATA_DIR / f"dataset{suffix}.parquet"
    df = pd.read_parquet(ds_path)
    meta = json.loads((REG_DIR / f"metadata{suffix}.json").read_text(encoding="utf-8"))
    booster = lgb.Booster(model_file=str(REG_DIR / f"lgbm{suffix}_v1.txt"))
    cal = meta.get("calibration")

    p_raw = booster.predict(df[meta["features"]])
    p_up = np.interp(p_raw, cal["x"], cal["y"]) if cal else p_raw
    df["p_up"] = p_up
    df["anchor_ms"] = (pd.to_datetime(df["anchor_time_utc"], utc=True)
                       .astype("datetime64[ms, UTC]").astype("int64"))

    snaps = load_pm_snapshots()
    rows = []
    for r in df.itertuples(index=False):
        t_dec = int(r.anchor_ms) + dm * 60_000
        slug = build_slug(int(r.anchor_ms))
        snap = snaps.get(slug)
        if not snap:
            continue
        pm = pm_at(snap, t_dec)
        if pm is None:
            continue
        up_pm, dn_pm, age = pm
        if not (0 < up_pm < 1 and 0 < dn_pm < 1):
            continue
        dir_up = bool(r.p_up >= 0.5)
        price = up_pm if dir_up else dn_pm
        prob = r.p_up if dir_up else 1.0 - r.p_up
        win = bool(r.label == 1) if dir_up else bool(r.label == 0)
        rows.append({
            "anchor_utc": r.anchor_time_utc, "slug": slug,
            "p_up": round(float(r.p_up), 4), "dir": "UP" if dir_up else "DOWN",
            "pm_up": up_pm, "pm_dn": dn_pm, "pm_age_min": age,
            "price": price, "edge": prob - price, "label": int(r.label),
            "win": win, "ret_hour": float(r.ret_hour),
            "realized": (1 - price) if win else -price,
            "model_ev": prob * (1 - price) - (1 - prob) * price,
        })

    rep = pd.DataFrame(rows)
    n_all = len(df)
    print(f"[ev_replay] dm={dm} dataset={ds_path.name} rows={n_all} "
          f"with_pm={len(rep)} ({len(rep)/n_all*100:.0f}%)")

    def line(tag: str, g: pd.DataFrame) -> str:
        if g.empty:
            return f"  {tag:<16} trades=0"
        hit = g["win"].mean() * 100
        return (f"  {tag:<16} trades={len(g):>4} hit={hit:5.1f}% "
                f"pnl={g['realized'].sum():+7.2f} avg={g['realized'].mean():+.4f} "
                f"model_ev_avg={g['model_ev'].mean():+.4f}")

    print(line("ALL (no edge)", rep))
    for th in ths:
        t = rep[rep["edge"] >= th]
        t = t[t["price"] <= args.price_cap]
        print(line(f"edge>={th:.2f}", t))

    # 最优阈值下分方向
    best = rep[(rep["edge"] >= ths[-1]) & (rep["price"] <= args.price_cap)]
    if not best.empty:
        for d in ("UP", "DOWN"):
            g = best[best["dir"] == d]
            if not g.empty:
                print(line(f"edge>={ths[-1]:.2f} {d}", g))
        worst = best.nsmallest(5, "realized")
        print("  worst-5:")
        for w in worst.itertuples(index=False):
            print(f"    {w.anchor_utc[:16]} {w.dir:<4} p={w.p_up:.3f} pm={w.price:.3f} "
                  f"ret={w.ret_hour:+.2f}% realized={w.realized:+.3f}")

    out = DATA_DIR / f"ev_replay_dm{dm}.json"
    out.write_text(json.dumps(rep.to_dict(orient="records"), ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"[ev_replay] detail -> {out.name}")
    return 0


def build_slug(anchor_ms: int) -> str:
    """与 predict_hourly.build_slug 相同（ET 月-日-年-小时-am/pm-et）。"""
    from zoneinfo import ZoneInfo
    dt = datetime.fromtimestamp(anchor_ms / 1000, tz=timezone.utc).astimezone(
        ZoneInfo("America/New_York"))
    hour12 = dt.hour % 12 or 12
    return (f"bitcoin-up-or-down-{dt.strftime('%B').lower()}-{dt.day}-"
            f"{dt.year}-{hour12}{'am' if dt.hour < 12 else 'pm'}-et")


if __name__ == "__main__":
    raise SystemExit(main())
