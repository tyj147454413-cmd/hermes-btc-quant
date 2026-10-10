#!/usr/bin/env python3
"""Binance→PM 传导延迟检验（alpha 候选·速度型，ROADMAP P0.3）。

假设：BTC 现货价格变动后，PM 小时合约做市商调价存在滞后窗口。
若滞后显著（分钟级），决策点「看 Binance 最新价 → 买 PM 未更新的报价」即速度型 alpha。

方法：
    1. 读 data/book_snapshots.jsonl（PM CLOB 快照，约每 2 分钟一条，含 UP/DOWN top3）。
    2. 拉 Binance 1m K 线覆盖同一时段（镜像回退，主域 451 时用 data-api.binance.vision）。
    3. 每条快照对齐「所在分钟的 1m 开盘价」→ 分钟级 Binance 价格序列。
    4. 小时内相邻快照对：Δy = PM UP mid 变化，Δx = Binance 价格变化(%)，
       小时固定效应（组内去均值）后：
       a) 互相关：corr(Δy_t, Δx_{t-k})，k=0..max_lag（每档 ≈2 分钟）
       b) OLS：Δy_t = Σ b_k·Δx_{t-k}
    5. 输出系数 + t 统计量 + 解读指引。

解读（|t|>2 视为显著）：
    - b0 主导、b1/b2≈0   → PM 在 2 分钟内消化信息，无分钟级传导延迟（本粒度下无速度 alpha）
    - b1/b2 显著为正     → 存在 2-6 分钟级慢消化 → 决策点速度 alpha 候选，值得升级采集粒度
    - 全部不显著         → 数据量不足或 PM 对 Binance 小波动不敏感，看 n 与 |Δx| 分布再定

用法（生产机）：
    .venv/Scripts/python model/transmission_lag.py
    .venv/Scripts/python model/transmission_lag.py --max-lag 5 --gap-tol 4
"""
from __future__ import annotations

import argparse
import json
import sys
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hermes_common import http_get_json  # noqa: E402

SNAP = ROOT / "data" / "book_snapshots.jsonl"
_SPOT_HOSTS = ("https://api.binance.com", "https://data-api.binance.vision")


def fetch_1m_klines(start_ms: int, end_ms: int) -> list[list]:
    """拉 1m K 线（用 open_time/open 两列），多页+镜像回退。"""
    out, cur = [], start_ms
    while cur < end_ms:
        data = None
        for host in _SPOT_HOSTS:
            data = http_get_json(
                f"{host}/api/v3/klines?symbol=BTCUSDT&interval=1m"
                f"&startTime={cur}&endTime={end_ms}&limit=1000", timeout=15)
            if data:
                break
        if not data:
            break
        out.extend(data)
        cur = int(data[-1][0]) + 60_000
        if len(data) < 1000:
            break
    return out


def load_snapshots() -> list[dict]:
    """读 PM 快照 → [{ts_ms, slug, up_mid}]，同 slug+分钟去重（保留最后）。"""
    if not SNAP.exists():
        print(f"[lag] [X] 缺 {SNAP}")
        return []
    best: dict[tuple, dict] = {}
    for line in SNAP.read_text(encoding="utf-8-sig").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        up, dn = r.get("up") or {}, r.get("down") or {}
        ub, ua = up.get("bids") or [], up.get("asks") or []
        db, da = dn.get("bids") or [], dn.get("asks") or []
        if ub and ua:
            mid = (float(ub[0][0]) + float(ua[0][0])) / 2
        elif db and da:  # UP 盘口空 → 1 - DOWN mid
            mid = 1 - (float(db[0][0]) + float(da[0][0])) / 2
        else:
            continue
        ts = datetime.fromisoformat(r["ts_utc"]).timestamp()
        best[(r["slug"], r.get("minutes_in_hour"))] = {
            "ts_ms": int(ts * 1000), "slug": r["slug"], "up_mid": mid}
    return sorted(best.values(), key=lambda x: x["ts_ms"])


def ols_tstats(X: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """最小二乘 + t 统计量（X 已去均值，无截距）。"""
    XtX_inv = np.linalg.inv(X.T @ X)
    beta = XtX_inv @ X.T @ y
    resid = y - X @ beta
    dof = max(len(y) - X.shape[1], 1)
    sigma2 = float(resid @ resid) / dof
    se = np.sqrt(np.maximum(np.diag(XtX_inv) * sigma2, 0))
    t = np.divide(beta, se, out=np.zeros_like(beta), where=se > 0)
    return beta, t


def main() -> int:
    ap = argparse.ArgumentParser(description="Binance→PM 传导延迟检验")
    ap.add_argument("--max-lag", type=int, default=3, help="回归滞后阶数（默认 3，每阶≈2分钟）")
    ap.add_argument("--gap-tol", type=int, default=4,
                    help="相邻快照最大分钟间隔（默认 4，快照正常间隔 2 分钟）")
    args = ap.parse_args()

    snaps = load_snapshots()
    if len(snaps) < 50:
        print(f"[lag] [X] 快照太少（{len(snaps)} 条），等积累后再跑")
        return 1
    t0, t1 = snaps[0]["ts_ms"], snaps[-1]["ts_ms"]
    print(f"[lag] PM 快照 {len(snaps)} 条: "
          f"{datetime.fromtimestamp(t0/1000, tz=timezone.utc):%m-%d %H:%M} → "
          f"{datetime.fromtimestamp(t1/1000, tz=timezone.utc):%m-%d %H:%M} UTC")

    kl = fetch_1m_klines(t0 - 120_000, t1 + 60_000)
    if not kl:
        print("[lag] [X] 1m K线拉取失败")
        return 1
    k_open_ms = [int(k[0]) for k in kl]
    k_price = [float(k[1]) for k in kl]
    print(f"[lag] 1m K线 {len(kl)} 根")

    def px_at(ts_ms: int) -> float | None:
        """快照所在分钟的 1m 开盘价（快照统一落在整 2 分钟后 ~30s，偏置一致）。"""
        m = ts_ms // 60_000 * 60_000
        i = bisect_right(k_open_ms, m) - 1
        if i < 0 or k_open_ms[i] != m:
            return None
        return k_price[i]

    # ---- 小时内相邻快照对 → 展平（同 slug 连续段为同一组，用于小时固定效应） ----
    rows2: list[dict] = []
    gids2: list[int] = []
    cur_gid, prev_slug = -1, None
    for a, b in zip(snaps, snaps[1:]):
        if a["slug"] != b["slug"] or not (1 <= (b["ts_ms"] - a["ts_ms"]) / 60_000 <= args.gap_tol):
            continue
        pa, pb = px_at(a["ts_ms"]), px_at(b["ts_ms"])
        if not pa or not pb:
            continue
        if a["slug"] != prev_slug:
            cur_gid += 1
            prev_slug = a["slug"]
        rows2.append({"dy": b["up_mid"] - a["up_mid"], "dx": (pb - pa) / pa * 100})
        gids2.append(cur_gid)

    n_hours = cur_gid + 1
    n = len(rows2)
    if n < 100:
        print(f"[lag] [X] 有效观测对太少（{n}），等积累后再跑")
        return 1
    g = np.array(gids2)
    dy = np.array([r["dy"] for r in rows2])
    dx = np.array([r["dx"] for r in rows2])
    print(f"[lag] 观测对 {n}（{n_hours} 个小时），|Δx| 中位 {np.median(np.abs(dx)):.3f}%，"
          f"P90 {np.percentile(np.abs(dx), 90):.3f}%")

    def demean(v: np.ndarray) -> np.ndarray:
        v = v.copy()
        for gid in np.unique(g):
            m = g == gid
            if m.sum() > 1:
                v[m] -= v[m].mean()
        return v

    dy_d, dx_d = demean(dy), demean(dx)

    # ---- a) 互相关（组内去均值） ----
    print("\n[lag] 互相关 corr(ΔPM_t, ΔBinance_{t-k})（小时固定效应后）:")
    thresh = 2 / np.sqrt(n)
    for k in range(args.max_lag + 1):
        if k == 0:
            c = np.corrcoef(dy_d, dx_d)[0, 1]
            star = " *" if abs(c) > thresh else ""
        else:
            lag_dx = np.full(n, np.nan)
            for j in range(n):
                if j - k >= 0 and g[j] == g[j - k]:  # 同组才有滞后
                    lag_dx[j] = dx[j - k]
            mask = ~np.isnan(lag_dx)
            if mask.sum() > 30:
                ld = demean(np.where(mask, lag_dx, 0.0))
                c = np.corrcoef(dy_d[mask], ld[mask])[0, 1]
                star = " *" if abs(c) > 2 / np.sqrt(mask.sum()) else ""
            else:
                c, star = float("nan"), ""
        print(f"    lag={k} (≈{(k*2):2d}-{k*2+2:2d}min前): r = {c:+.3f}{star}")

    # ---- b) 分布滞后 OLS（滞后缺失位置填组均值=去均值后填 0） ----
    cols = [dx_d]
    for k in range(1, args.max_lag + 1):
        lag_dx = np.zeros(n)
        for j in range(n):
            if j - k >= 0 and g[j] == g[j - k]:
                lag_dx[j] = dx[j - k]
            else:
                lag_dx[j] = np.nan
        # 组内用可得值去均值后再填 0（缺失≈组均值）
        filled = demean(np.where(np.isnan(lag_dx), 0.0, lag_dx))
        cols.append(filled)
    M = np.column_stack(cols)
    beta, t = ols_tstats(M, dy_d)
    print("\n[lag] 分布滞后回归 ΔPM = Σ b_k·ΔBinance_{t-k}（小时固定效应，无截距）:")
    for k in range(args.max_lag + 1):
        print(f"    b{k} (当期/滞后{k*2:2d}min): {beta[k]:+.5f}  (t={t[k]:+.1f})")
    print(f"    合计响应 Σb = {beta.sum():+.5f}")

    # ---- 解读 ----
    sig = [k for k in range(1, args.max_lag + 1) if abs(t[k]) > 2]
    print("\n[lag] ===== 解读 =====")
    if not sig and abs(t[0]) > 2:
        print("  当期响应显著、滞后项不显著 → PM 在 2 分钟内消化 Binance 信息。")
        print("  本粒度下无分钟级传导延迟 alpha；若要更快窗口需升级秒级采集。")
    elif sig:
        ks = ", ".join(f"滞后{k*2}-{k*2+2}min(t={t[k]:+.1f})" for k in sig)
        print(f"  [+] 发现显著滞后响应: {ks}")
        print("  → PM 对 Binance 信息存在分钟级慢消化，速度型 alpha 候选成立！")
        print("  下一步: 用更细粒度快照定位窗口宽度，并在影子模式验证 EV。")
    else:
        print("  当期与滞后均不显著 → 样本量不足或 PM 对小幅波动不敏感。")
        print(f"  当前 n={n}；建议数据翻倍后重跑。")

    (ROOT / "model" / "data").mkdir(parents=True, exist_ok=True)
    out = {"generated_at": datetime.now(timezone.utc).isoformat(),
           "n_obs": int(n), "n_hours": int(n_hours),
           "beta": beta.tolist(), "t": t.tolist(),
           "total_response": float(beta.sum())}
    (ROOT / "model" / "data" / "transmission_lag_report.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\n[lag] 报告 → model/data/transmission_lag_report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
