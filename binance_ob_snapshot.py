#!/usr/bin/env python3
"""C 线：Binance 微观结构旁路采集器（订单簿失衡 + CVD）——研究用，不动现有采集链路。

每小时在决策点（:00~:55，每 5 分钟网格）采集一次，落盘 data/binance_ob_snapshots.jsonl：
    Binance 现货 BTCUSDT 订单簿前 20 档 + 最近 5 分钟 aggTrades

构造 8 个"盘口/资金流"正交特征（与 dev_pct 路径无关，是盘口价外的增量信息源）：
    ob_imb_top5       前 5 档挂单量失衡 (Σbid-Σask)/(Σbid+Σask) ∈ [-1,1]
    ob_imb_top10      前 10 档失衡
    ob_bid_ask_gap_bp 买一卖一架差（基点）
    cvd_btc_5m        5 分钟 CVD（base 口径，主动买-主动卖 BTC 量）
    cvd_usdt_5m       5 分钟 CVD（USDT 口径）
    taker_buy_ratio   主动买量占比
    vwap_bias_bp      最新 mid 价相对 5 分钟 VWAP 偏离（基点）
    cvd_tail_slope    最近 1 分钟 CVD 增速 - 前 4 分钟增速（动量加速度）

调度建议（生产机，不动现有 hermes_collect）：
    Windows 计划任务，每 5 分钟触发一次（:01,:06,...,:56），
    命令行：python binance_ob_snapshot.py --decision-min <0|5|10|...|55>
    （decision-min 由任务触发分钟决定，与 predict_hourly.py 调度对齐）

用法：
    .venv/Scripts/python binance_ob_snapshot.py --decision-min 35            # 实时采集
    .venv/Scripts/python binance_ob_snapshot.py --decision-min 35 --dry-run  # 只测特征计算
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from hermes_common import (DATA_DIR, ET, build_slug,  # noqa: E402
                           http_get_json, log)

BINANCE_API = "https://api.binance.com"
BINANCE_MIRROR = "https://data-api.binance.vision"  # 大陆可达镜像（主域 451 时回退）
SNAP_PATH = DATA_DIR / "binance_ob_snapshots.jsonl"
HOUR_MS = 3600_000

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def _get_json_multi(path: str, timeout: int, proxy=None):
    """先主域后镜像（data-api.binance.vision），与 backfill_data 同风格回退。"""
    for base in (BINANCE_API, BINANCE_MIRROR):
        data = http_get_json(f"{base}{path}", timeout=timeout, proxy=proxy)
        if data:
            return data
    return None


def fetch_depth(symbol: str = "BTCUSDT", limit: int = 20, proxy=None):
    data = _get_json_multi(f"/api/v3/depth?symbol={symbol}&limit={limit}",
                           timeout=10, proxy=proxy)
    if not data:
        return None
    bids = [(float(p), float(q)) for p, q in data.get("bids", [])]
    asks = [(float(p), float(q)) for p, q in data.get("asks", [])]
    return bids, asks


def fetch_agg_trades(symbol: str, start_ms: int, end_ms: int, proxy=None):
    """分页拉 aggTrades（limit 1000/页，循环 fromId）。"""
    out = []
    from_id = None
    while True:
        path = (f"/api/v3/aggTrades?symbol={symbol}"
                f"&startTime={start_ms}&endTime={end_ms - 1}&limit=1000")
        if from_id:
            path += f"&fromId={from_id}"
        page = _get_json_multi(path, timeout=15, proxy=proxy)
        if not page:
            break
        out.extend(page)
        if len(page) < 1000:
            break
        from_id = max(int(t["a"]) for t in page) + 1
        if len(out) > 20000:  # 安全熔断：5 分钟正常 <8000 笔
            break
    return out


def calc_ob_feats(bids, asks, aggs, t_dec) -> dict:
    """订单簿 + 成交流 → 8 个微观结构特征（t_dec=决策点 ms，用于末分钟分界）。"""
    def imb(n):
        bq = sum(q for _, q in bids[:n])
        aq = sum(q for _, q in asks[:n])
        return round((bq - aq) / (bq + aq), 4) if (bq + aq) else None

    best_bid, best_ask = bids[0][0] if bids else 0.0, asks[0][0] if asks else 0.0
    mid = (best_bid + best_ask) / 2
    gap_bp = round((best_ask - best_bid) / mid * 10000, 1) if mid else None

    cvd_btc = cvd_usdt = 0.0
    qty_buy = qty_all = 0.0
    pv_sum = q_sum = 0.0
    early_ts = t_dec - 60_000  # 末 1 分钟分界（实时/回看口径一致，均锚定 t_dec）
    cvd_seg = 0.0  # 最后 1 分钟 CVD
    for t in aggs:
        price, qty = float(t["p"]), float(t["q"])
        is_maker_buy = not bool(t["m"])  # m=True 表示买方是 maker → 主动卖
        sgn = 1.0 if is_maker_buy else -1.0
        cvd_btc += sgn * qty
        cvd_usdt += sgn * qty * price
        if is_maker_buy:
            qty_buy += qty
        qty_all += qty
        pv_sum += price * qty
        q_sum += qty
        if int(t["T"]) >= early_ts:
            cvd_seg += sgn * qty * price
    cvd_prev = cvd_usdt - cvd_seg  # 前 4 分钟 USDT CVD
    return {
        "ob_imb_top5": imb(5),
        "ob_imb_top10": imb(10),
        "ob_bid_ask_gap_bp": gap_bp,
        "cvd_btc_5m": round(cvd_btc, 4),
        "cvd_usdt_5m": round(cvd_usdt, 1),
        "taker_buy_ratio": round(qty_buy / qty_all, 4) if qty_all else None,
        "vwap_bias_bp": round((mid - pv_sum / q_sum) / (pv_sum / q_sum) * 10000, 1)
        if q_sum else None,
        "cvd_tail_slope": round(cvd_seg - cvd_prev / 4, 1),  # 末分钟 vs 前4分钟均值
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Binance 微观结构旁路采集")
    ap.add_argument("--decision-min", type=int,
                    choices=(0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55))
    ap.add_argument("--hours-ago", type=int, default=0, help="回看小时数（用于补采复盘）")
    ap.add_argument("--dry-run", action="store_true", help="只测网络与特征计算，不落盘")
    args = ap.parse_args()

    if args.decision_min is None:
        now = datetime.now(timezone.utc).astimezone(ET)
        args.decision_min = (now.minute // 5) * 5
        log(f"[ob] 未指定 --decision-min，按当前分钟取 {args.decision_min}")

    dm = args.decision_min
    now_ms = int(time.time() * 1000)
    anchor_ms = (now_ms - args.hours_ago * HOUR_MS) // HOUR_MS * HOUR_MS
    t_dec = anchor_ms + dm * 60_000

    res = fetch_depth(proxy=None)
    if res is None:
        log("[ob] [X] 订单簿拉取失败（网络/代理不通）", level="E")
        if args.dry_run:
            print("[dry-run] depth fetch FAILED — 检查网络/代理")
            return 1
        return 1
    bids, asks = res

    aggs = fetch_agg_trades("BTCUSDT", t_dec - 5 * 60_000, t_dec, proxy=None)
    if not aggs:
        log("[ob] [X] aggTrades 拉取失败", level="E")
        if args.dry_run:
            print("[dry-run] agg fetch FAILED")
            return 1
        return 1

    feats = calc_ob_feats(bids, asks, aggs, t_dec)
    slug = build_slug(datetime.fromtimestamp(anchor_ms / 1000, tz=timezone.utc)
                      .astimezone(ET))
    rec = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "slug": slug, "decision_min": dm, "anchor_ms": anchor_ms,
        "n_aggs": len(aggs), **feats,
    }

    print(f"[ob] {slug} | dm={dm} | n_aggs={len(aggs)}")
    for k, v in feats.items():
        print(f"    {k:20s} = {v}")

    if not args.dry_run:
        with open(SNAP_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"[ob] [OK] -> {SNAP_PATH}")
    else:
        print("[dry-run] OK — 未落盘")
    return 0


if __name__ == "__main__":
    sys.exit(main())