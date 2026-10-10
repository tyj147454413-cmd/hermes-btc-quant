#!/usr/bin/env python3
"""OI/基差 首版信号强度检验（alpha 候选·信息型，ROADMAP P1.3 → 9/15 裁决输入）。

数据源（HermesBTCOICollect 持续积累中）：
    data/oi_hist.jsonl       5m 粒度 OI（fapi openInterestHist，只回看 ~41h）
    data/premium_hist.jsonl  基差/funding 快照（每小时）

假设（每个都先写预期方向再检验，防数据窥探）：
    H1: 前一小时 OI 大幅上升（新仓涌入/拥挤）→ 本小时方向延续性变差（均值回归倾向）
    H2: 小时开盘时期现基差 extreme（升水=多头拥挤 / 贴水=空头拥挤）→ 反向信号
    H3: funding 极端时本小时方向可预测（多头拥挤 → 回调）

方法：逐小时对齐 OI/基差特征与 1h K线方向标签，
    - 分桶上涨率（H1/H2/H3）
    - 点二列相关 + t 统计量（|t|>2 显著）

纪律：当前数据 <30 天，本脚本结论只作探索性参考；n<30 的桶不下结论。

用法（生产机）：
    .venv/Scripts/python model/alpha_oi_test.py
"""
from __future__ import annotations

import json
import sys
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hermes_common import http_get_json  # noqa: E402

DATA = ROOT / "data"
OI_HIST = DATA / "oi_hist.jsonl"
PREMIUM = DATA / "premium_hist.jsonl"
OUT = ROOT / "model" / "data" / "alpha_oi_report.json"
MIRROR = "https://data-api.binance.vision"
HOUR_MS = 3600_000

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def fetch_1h_klines(start_ms: int, end_ms: int) -> list[list]:
    """拉 1h K线（[open_time, open, high, low, close, ...]）。"""
    out, cur = [], start_ms
    while cur < end_ms:
        data = http_get_json(f"{MIRROR}/api/v3/klines?symbol=BTCUSDT&interval=1h"
                             f"&startTime={cur}&endTime={end_ms}&limit=1000", timeout=15)
        if not data:
            break
        out.extend(data)
        cur = int(data[-1][0]) + HOUR_MS
        if len(data) < 1000:
            break
    return out


def point_biserial(xs: list[float], ys: list[int]) -> tuple[float, float]:
    """点二列相关 + t 统计量。"""
    n = len(xs)
    if n < 10:
        return float("nan"), float("nan")
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return float("nan"), float("nan")
    r = sxy / (sxx ** 0.5 * syy ** 0.5)
    t = r * ((n - 2) / max(1 - r * r, 1e-12)) ** 0.5
    return r, t


def main() -> int:
    oi = sorted(load_jsonl(OI_HIST), key=lambda r: r["ts_utc"])
    prem = sorted(load_jsonl(PREMIUM), key=lambda r: r["ts_utc"])
    print(f"[oi-test] OI 行数 {len(oi)}，premium 行数 {len(prem)}")
    if len(oi) < 60:  # 至少 5 小时
        print("[oi-test] [X] OI 数据不足（<60 行 5m 记录），等 HermesBTCOICollect 积累")
        return 1

    oi_ts = [int(datetime.fromisoformat(r["ts_utc"]).timestamp() * 1000) for r in oi]
    oi_val = [r["oi_btc"] for r in oi]
    t0, t1 = oi_ts[0], oi_ts[-1]
    print(f"[oi-test] OI 覆盖: {datetime.fromtimestamp(t0/1000, tz=timezone.utc):%m-%d %H:%M} → "
          f"{datetime.fromtimestamp(t1/1000, tz=timezone.utc):%m-%d %H:%M} UTC")

    def oi_at(ts_ms: int) -> float | None:
        """ts_ms 时刻之前（含）最近的 OI 值。"""
        i = bisect_right(oi_ts, ts_ms) - 1
        if i < 0:
            return None
        # 允许 10 分钟内的插值容差（5m 网格）
        if ts_ms - oi_ts[i] > 10 * 60_000:
            return None
        return oi_val[i]

    def basis_at(ts_ms: int) -> float | None:
        """ts_ms 之前最近一条基差（容差 90 分钟）。"""
        if not prem:
            return None
        arr = [(int(datetime.fromisoformat(r["ts_utc"]).timestamp() * 1000), r)
               for r in prem]
        ts_list = [a[0] for a in arr]
        i = bisect_right(ts_list, ts_ms) - 1
        if i < 0 or ts_ms - ts_list[i] > 90 * 60_000:
            return None
        return arr[i][1].get("basis_bp")

    # ---- 逐小时样本：特征 = 小时开盘时刻可观测值，标签 = 该小时方向 ----
    kl = fetch_1h_klines(t0 - 2 * HOUR_MS, t1)
    print(f"[oi-test] 1h K线 {len(kl)} 根")
    samples = []
    for k in kl:
        a_ms = int(k[0])
        o, c = float(k[1]), float(k[4])
        oi_now, oi_prev = oi_at(a_ms), oi_at(a_ms - HOUR_MS)
        if oi_now is None or oi_prev is None or oi_prev == 0:
            continue
        samples.append({
            "anchor_ms": a_ms,
            "oi_chg_pct": (oi_now - oi_prev) / oi_prev * 100,
            "basis_bp": basis_at(a_ms),
            "label": 1 if c > o else 0,
        })
    n = len(samples)
    print(f"[oi-test] 有效小时样本 {n}")
    if n < 24:
        print("[oi-test] [X] 有效样本 < 24 小时，等积累后重跑")
        return 1

    rep = {"generated_at": datetime.now(timezone.utc).isoformat(), "n_hours": n}
    ys = [s["label"] for s in samples]
    up_rate = sum(ys) / n

    # ---- H1: 前一小时 OI 变化 → 本小时方向 ----
    xs = [s["oi_chg_pct"] for s in samples]
    r, t = point_biserial(xs, ys)
    print(f"\n===== H1: 前一小时 OI 变化 vs 本小时方向 =====")
    print(f"  corr = {r:+.3f} (t={t:+.1f})  |  全样本上涨率 {up_rate:.3f}")
    rep["H1_oi_chg"] = {"corr": round(r, 4), "t": round(t, 2)}
    buckets = [(-99, -1.0), (-1.0, -0.3), (-0.3, 0.3), (0.3, 1.0), (1.0, 99)]
    print(f"  {'OI前1h变化%':<14}{'n':>4}{'上涨率':>8}")
    for lo, hi in buckets:
        sub = [s for s in samples if lo <= s["oi_chg_pct"] < hi]
        if not sub:
            continue
        ur = sum(x["label"] for x in sub) / len(sub)
        rep["H1_oi_chg"][f"bucket_{lo}_{hi}"] = {"n": len(sub), "up_rate": round(ur, 4)}
        print(f"  {lo:+.1f}~{hi:+.1f}{'':<6}{len(sub):>4}{ur:>8.3f}")

    # ---- H2: 开盘基差 → 本小时方向 ----
    bx = [(s["basis_bp"], s["label"]) for s in samples if s["basis_bp"] is not None]
    if len(bx) >= 24:
        r2, t2 = point_biserial([x for x, _ in bx], [y for _, y in bx])
        print(f"\n===== H2: 开盘基差(bp) vs 本小时方向（n={len(bx)}）=====")
        print(f"  corr = {r2:+.3f} (t={t2:+.1f})")
        rep["H2_basis"] = {"n": len(bx), "corr": round(r2, 4), "t": round(t2, 2)}
        med = sorted(x for x, _ in bx)[len(bx) // 2]
        lo_s = [y for x, y in bx if x < med]
        hi_s = [y for x, y in bx if x >= med]
        if lo_s and hi_s:
            print(f"  低基差(<{med:.1f}bp) 上涨率 {sum(lo_s)/len(lo_s):.3f} (n={len(lo_s)}) | "
                  f"高基差 上涨率 {sum(hi_s)/len(hi_s):.3f} (n={len(hi_s)})")
            rep["H2_basis"]["low_up_rate"] = round(sum(lo_s) / len(lo_s), 4)
            rep["H2_basis"]["high_up_rate"] = round(sum(hi_s) / len(hi_s), 4)
    else:
        print(f"\n[H2] premium 样本不足（{len(bx)}），等积累")

    # ---- 汇总解读 ----
    print("\n===== 解读 =====")
    found = []
    if abs(t) > 2:
        found.append(f"H1 OI变化 (t={t:+.1f})")
    if len(bx) >= 24 and abs(t2) > 2:
        found.append(f"H2 基差 (t={t2:+.1f})")
    if found:
        print(f"  [+] 显著信号: {', '.join(found)} → 晋级影子验证候选")
        print("  注意: 当前数据跨度短，须在 9/15 裁决时用更长数据复核")
    else:
        print("  无显著信号（|t|≤2）。数据 <30 天属预期，持续积累后每两周复跑。")
    print(f"  纪律: n={n} 小时，仅作探索性参考。")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[oi-test] 报告 -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
