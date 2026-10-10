#!/usr/bin/env python3
"""影子时序交叉验证 + 校准审计 + EV 结算审计。

消费 data/ml_shadow_predictions.jsonl（实时/补录混合），做三件事：
  1. v1(p_up) 与 gs(p_gs/p_model) 的样本外表现（AUC/ACC/Brier/hit@0.65），按天分组
  2. 校准审计：模型概率分桶 vs 真实命中率（概率是否说得准）
  3. EV 口径结算：仅正 EV 出手的 paper PnL（沿用 ev_watch 修正后的胜负判定）

结算标签：Binance data-api 镜像拉 anchor+1h 收盘价（label=1 表示 close>anchor_price）。
缓存 klines 到 tmp/klines_cache.json，离线可复跑。

用法：
    .venv/Scripts/python model/cross_validate_shadow.py --dm 35
    .venv/Scripts/python model/cross_validate_shadow.py --dm 35 --out model/data/cv_report.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA = ROOT / "model" / "data"
SHADOW_DEFAULT = ROOT / "data" / "ml_shadow_predictions.jsonl"
CACHE = ROOT / "tmp" / "klines_cache.json"
MIRROR = "https://data-api.binance.vision"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def http_json(url, timeout=15):
    import requests
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def load_cache():
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(c):
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(c), encoding="utf-8")


def settle(slug, anchor_time_utc, anchor_price, cache):
    """拉 anchor 小时的 1h kline，close 即下一整点结算价。"""
    key = f"{slug}|{anchor_time_utc}"
    if key in cache:
        return cache[key]
    try:
        anchor_ms = int(datetime.fromisoformat(anchor_time_utc)
                        .timestamp() * 1000)
    except Exception:
        cache[key] = None
        return None
    k = http_json(f"{MIRROR}/api/v3/klines?symbol=BTCUSDT&interval=1h"
                  f"&startTime={anchor_ms}&limit=1")
    if not k or len(k) < 1:
        cache[key] = None
        return None
    close = float(k[0][4])
    label = 1 if close > float(anchor_price) else 0
    cache[key] = {"close": close, "label": label}
    return cache[key]


def load_shadow(path: Path, dm: int):
    rows = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("anchor_price") is None:
            continue
        if r.get("decision_min") != dm:
            continue
        rows.append(r)
    # 每 slug 留一条：优先保留带 pm 价的实时行（补录行 pm=None 否则会覆盖掉盘口价）
    uniq = {}
    for r in rows:
        cur = uniq.get(r["slug"])
        if cur is None or (r.get("pm_up") is not None and cur.get("pm_up") is None):
            uniq[r["slug"]] = r
    return list(uniq.values())


def brier(ps, ys):
    if not ps:
        return None
    return round(sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ps), 4)


def acc(ps, ys):
    if not ps:
        return None
    hit = sum(1 for p, y in zip(ps, ys) if (p >= 0.5) == (y == 1))
    return round(hit / len(ps), 4)


def auc_roc(ps, ys):
    if not ps or len(set(ys)) < 2:
        return None
    try:
        from sklearn.metrics import roc_auc_score
        return round(roc_auc_score(ys, ps), 4)
    except Exception:
        return None


def hit_at(ps, ys, thr):
    idx = [i for i, p in enumerate(ps) if p >= thr]
    if not idx:
        return None, 0
    h = sum(1 for i in idx if ys[i] == 1) / len(idx)
    return round(h, 4), len(idx)


def main() -> int:
    ap = argparse.ArgumentParser(description="影子交叉验证/校准/EV审计")
    ap.add_argument("--shadow", default=str(SHADOW_DEFAULT))
    ap.add_argument("--dm", type=int, default=35)
    ap.add_argument("--out", default=str(DATA / "cv_report.json"))
    args = ap.parse_args()

    rows = load_shadow(Path(args.shadow), args.dm)
    if not rows:
        print("[X] 无影子样本")
        return 1
    print(f"load {len(rows)} 个窗口（dm={args.dm}）")

    cache = load_cache()
    labeled = []  # (row, label)
    for r in rows:
        s = settle(r["slug"], r["anchor_time_utc"], r["anchor_price"], cache)
        time.sleep(0.08)
        if s is None:
            continue
        labeled.append((r, s["label"]))
    save_cache(cache)
    print(f"settled: {len(labeled)} / {len(rows)}")

    rep = {"dm": args.dm, "n": len(labeled)}
    ys = [lb for _, lb in labeled]

    def eval_model(get_p, need=None):
        sub = [(r, lb) for r, lb in labeled
               if (need is None or need(r)) and get_p(r) is not None]
        ps = [get_p(r) for r, _ in sub]
        suby = [lb for _, lb in sub]
        return sub, ps, suby

    # —— v1 ——
    v1_sub, p_up, y_up = eval_model(lambda r: r.get("p_up"))
    rep["v1"] = {
        "n": len(p_up), "auc": auc_roc(p_up, y_up), "acc": acc(p_up, y_up),
        "brier": brier(p_up, y_up),
        "hit_at_065": dict(zip(("rate", "n"), hit_at(p_up, y_up, 0.65))),
    }
    # —— gs ——
    get_gs_p = lambda r: r.get("p_gs") or r.get("p_model")
    gs_sub, p_gs, y_gs = eval_model(get_gs_p)
    if p_gs:
        rep["gs"] = {
            "n": len(p_gs), "auc": auc_roc(p_gs, y_gs), "acc": acc(p_gs, y_gs),
            "brier": brier(p_gs, y_gs),
            "hit_at_065": dict(zip(("rate", "n"), hit_at(p_gs, y_gs, 0.65))),
        }
        # 同窗口 v1 vs gs 对比（BUG FIX: 原先用 p_gs[:len(common)] 位置切片，
        # 当 common 是 gs_sub 的真子集时概率与标签错位；现改为按行重建配对）
        common = [(r, lb) for r, lb in labeled
                  if r.get("p_up") is not None and get_gs_p(r) is not None]
        if common:
            pv = [r["p_up"] for r, _ in common]
            pg = [get_gs_p(r) for r, _ in common]
            gy = [lb for _, lb in common]
            rep["gs_vs_v1_same_window"] = {
                "n": len(common),
                "brier_v1": brier(pv, gy),
                "brier_gs": brier(pg, gy),
                "acc_v1": acc(pv, gy),
                "acc_gs": acc(pg, gy),
            }

    # —— gs_nomtm（A/B 对照：剔除动量特征版）——
    get_nm_p = lambda r: r.get("p_gs_nomtm")
    _, p_nm, y_nm = eval_model(get_nm_p)
    if p_nm:
        rep["gs_nomtm"] = {
            "n": len(p_nm), "auc": auc_roc(p_nm, y_nm), "acc": acc(p_nm, y_nm),
            "brier": brier(p_nm, y_nm),
            "hit_at_065": dict(zip(("rate", "n"), hit_at(p_nm, y_nm, 0.65))),
        }

    # —— 按天分组 ——
    by_day = defaultdict(list)
    for r, lb in labeled:
        if r.get("p_up") is None:
            continue
        by_day[r["anchor_time_utc"][:10]].append((r["p_up"], lb))
    rep["v1_by_day"] = {
        d: {"n": len(v), "acc": acc([p for p, _ in v], [l for _, l in v]),
            "brier": brier([p for p, _ in v], [l for _, l in v])}
        for d, v in sorted(by_day.items())}

    # —— 校准审计（v1 分桶）——
    bins = [(0.00, 0.50), (0.50, 0.55), (0.55, 0.60), (0.60, 0.65),
            (0.65, 0.70), (0.70, 1.01)]
    calib = []
    for lo, hi in bins:
        idx = [i for i, p in enumerate(p_up) if lo <= p < hi]
        if not idx:
            continue
        emp = sum(1 for i in idx if y_up[i] == 1) / len(idx)
        mid = sum(p_up[i] for i in idx) / len(idx)
        calib.append({"bin": f"{lo:.2f}-{hi:.2f}", "n": len(idx),
                      "mean_p": round(mid, 3), "empirical": round(emp, 3),
                      "bias": round(emp - mid, 3)})
    rep["calib_v1_bins"] = calib

    # —— EV 口径结算（仅正 EV 出手）——
    # 9/6 升级：双口径。mid = Gamma 盘口价（历史口径，偏乐观）；exec = 可成交价
    # （优先取影子行 clob_ask_*（9/6 起新记录），老行回退 join book_snapshots
    #  同 slug、minutes_in_hour <= decision_min 且 3 分钟内最近的 ask1）。
    book_idx: dict[tuple, tuple] = {}  # (slug, minutes) -> (up_ask1, dn_ask1)
    SNAPF = ROOT / "data" / "book_snapshots.jsonl"
    if SNAPF.exists():
        for line in SNAPF.read_text(encoding="utf-8-sig").splitlines():
            try:
                b = json.loads(line)
            except json.JSONDecodeError:
                continue
            m = b.get("minutes_in_hour")
            if m is None:
                continue
            ua = (b.get("up") or {}).get("asks") or []
            da = (b.get("down") or {}).get("asks") or []
            book_idx[(b.get("slug"), int(m))] = (
                float(ua[0][0]) if ua else None, float(da[0][0]) if da else None)

    def exec_ask(r: dict) -> tuple:
        """决策点可成交 ask：(up, down)。行内 clob_ask 优先，回退快照 join。"""
        if r.get("clob_ask_up") is not None or r.get("clob_ask_down") is not None:
            return r.get("clob_ask_up"), r.get("clob_ask_down")
        dm = int(r.get("decision_min") or 0)
        for m in range(dm, max(dm - 4, -1), -1):
            if (r.get("slug"), m) in book_idx:
                return book_idx[(r.get("slug"), m)]
        return None, None

    pnl_all = pnl_ev = 0.0
    ex_all = ex_ev = 0.0
    n_dec = n_exec = 0
    spreads: list[float] = []
    for r, lb in labeled:
        if r.get("pm_up") is None:
            continue
        # BUG FIX: 老记录无 direction_model 时回退历史字段 direction
        side = "UP" if (r.get("direction_model") or r.get("direction") or "UP") == "UP" \
            else "DOWN"
        pm = r.get("pm_up") if side == "UP" else r.get("pm_down")
        if pm is None:
            continue
        n_dec += 1
        won = (side == "UP" and lb == 1) or (side == "DOWN" and lb == 0)
        win = (1 - pm) if won else -pm
        pnl_all += win
        ev = r.get("ev_up") if side == "UP" else r.get("ev_down")
        if ev is not None and ev > 0:
            pnl_ev += win
        # exec 口径（买按 ask）
        au, ad = exec_ask(r)
        ask = au if side == "UP" else ad
        if ask is not None and 0 < ask < 1:
            n_exec += 1
            ex_all += (1 - ask) if won else -ask
            if ev is not None and ev > 0:
                ex_ev += (1 - ask) if won else -ask
            spreads.append(ask - pm)
    rep["ev_settlement"] = {"n_priced": n_dec,
                            "pnl_all_usd": round(pnl_all, 2),
                            "pnl_ev_only_usd": round(pnl_ev, 2),
                            "n_exec_priced": n_exec,
                            "pnl_exec_all_usd": round(ex_all, 2),
                            "pnl_exec_ev_only_usd": round(ex_ev, 2),
                            "mean_ask_minus_mid": round(sum(spreads) / len(spreads), 4)
                            if spreads else None}

    out = Path(args.out)
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                   encoding="utf-8")

    print("\n===== 时序交叉验证报告 =====")
    print(f"窗口数: {len(labeled)}（dm{args.dm}）")
    v = rep["v1"]
    print(f"[v1] n={v['n']} | AUC={v['auc']} | ACC={v['acc']} | Brier={v['brier']} "
          f"| hit@0.65={v['hit_at_065']}")
    if "gs" in rep:
        g = rep["gs"]
        print(f"[gs] n={g['n']} | AUC={g['auc']} | ACC={g['acc']} | Brier={g['brier']} "
              f"| hit@0.65={g['hit_at_065']}")
        c = rep["gs_vs_v1_same_window"]
        print(f"[同窗对比] n={c['n']} | Brier v1={c['brier_v1']} vs gs={c['brier_gs']} "
              f"| ACC v1={c['acc_v1']} vs gs={c['acc_gs']}")
    print("\n[按天] ")
    for d, x in rep["v1_by_day"].items():
        print(f"  {d}: n={x['n']} acc={x['acc']} brier={x['brier']}")
    print("\n[校准分桶 v1] 已写入报告文件")
    for c in calib:
        flag = "  <-- 偏差大" if abs(c["bias"]) > 0.1 else ""
        print(f"  {c['bin']}: n={c['n']} mean_p={c['mean_p']} "
              f"empirical={c['empirical']} bias={c['bias']}{flag}")
    ev = rep["ev_settlement"]
    print(f"\n[EV 结算·mid] n={ev['n_priced']} | PnL(全出手)={ev['pnl_all_usd']} "
          f"| PnL(仅正EV)={ev['pnl_ev_only_usd']}")
    if ev.get("n_exec_priced"):
        print(f"[EV 结算·exec] n={ev['n_exec_priced']} | PnL(全出手)={ev['pnl_exec_all_usd']} "
              f"| PnL(仅正EV)={ev['pnl_exec_ev_only_usd']} "
              f"| 均价差(ask-mid)={ev['mean_ask_minus_mid']}")
    print(f"\n[OK] -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())