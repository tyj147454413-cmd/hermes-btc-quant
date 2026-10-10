#!/usr/bin/env python3
"""历史数据回补脚本（TRAINING_PLAN.md 第 1 步）。

目标：
    从 Binance 公开 API 回补 18 个月以上 1h 级别数据，重建「决策时点已知」的
    特征矩阵 + 标签，产出 model/data/dataset.parquet（≥10,000 样本）。

设计原则（防泄漏）：
    - 决策时点统一为「目标小时 H 开始后 15 分钟」（对齐实盘采集窗口 :12-:17 ET）。
    - 所有特征只用 H+15min 之前已收盘的 K 线数据。
    - 标签 = 小时 H 的 close vs open 方向（与 PM hourly up/down 结算一致）。
    - hist_up_rate（同时段季节性）只用 H 之前的历史，expanding 且 shift(1)。

可回溯 vs 不可回溯：
    - 可回溯（本脚本重建）：量价类（K线 OHLCV+主动买卖量）、RSI/BB/MACD 各周期、
      24h 价格位置、上一小时惯性、宏观趋势族、资金费率、恐惧贪婪指数、季节性。
    - 不可回溯（缺省为 NaN，训练时忽略）：订单簿深度、OI 变化（fapi 仅留 30 天）、
      PM 盘口实时价、开盘 fakeout/V 反转等微观形态。这些只在快照数据集中存在。

用法：
    .venv/Scripts/python model/backfill_data.py                    # 默认 2025-03-01 起
    .venv/Scripts/python model/backfill_data.py --start 2025-01-01
    .venv/Scripts/python model/backfill_data.py --no-cache         # 强制重新下载

输出：
    model/data/dataset.parquet / dataset.csv
    model/data/manifest.json
    model/data/quality_report.md
    model/data/raw/*.parquet   # 原始 K 线缓存（重跑免下载）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hermes_common import http_get_json  # noqa: E402

BINANCE_API = "https://api.binance.com"
BINANCE_FUTURES_API = "https://fapi.binance.com"
FNG_API = "https://api.alternative.me/fng/?limit=0"
ET = ZoneInfo("America/New_York")

DATA_DIR = ROOT / "model" / "data"
RAW_DIR = DATA_DIR / "raw"

KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume",
              "close_time", "quote_vol", "trades", "taker_buy_base"]


# ---------------------------------------------------------------- HTTP 拉取
# spot 行情备用镜像（api.binance.com 被墙/超时时自动回退，接口路径完全一致）
_SPOT_HOSTS = (BINANCE_API, "https://data-api.binance.vision")


def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int,
                 proxy: str | None) -> pd.DataFrame:
    """分页拉取 Binance spot K线（含 taker_buy_base 字段），缓存到 raw 目录。"""
    cache = RAW_DIR / f"klines_{symbol}_{interval}_{start_ms}_{end_ms}.parquet"
    if cache.exists() and not getattr(fetch_klines, "_no_cache", False):
        return pd.read_parquet(cache)
    rows: list[list] = []
    cur = start_ms
    while cur < end_ms:
        data: list | None = None
        for host in _SPOT_HOSTS:
            url = (f"{host}/api/v3/klines?symbol={symbol}&interval={interval}"
                   f"&startTime={cur}&endTime={end_ms}&limit=1000")
            data = http_get_json(url, timeout=15, proxy=proxy)
            if data:
                break
        if not data:
            break
        rows.extend(data)
        last_open = int(data[-1][0])
        nxt = last_open + 1
        if nxt <= cur:
            break
        cur = nxt
        if len(data) < 1000:
            break
        time.sleep(0.12)
    df = pd.DataFrame(rows, columns=[str(i) for i in range(12)])
    if df.empty:
        return df
    df = df.rename(columns={"0": "open_time", "1": "open", "2": "high", "3": "low",
                            "4": "close", "5": "volume", "6": "close_time",
                            "7": "quote_vol", "8": "trades", "9": "taker_buy_base"})
    df = df[KLINE_COLS].astype(float)
    df["open_time"] = df["open_time"].astype(np.int64)
    df = df.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache, index=False)
    return df


def fetch_funding(start_ms: int, end_ms: int, proxy: str | None) -> pd.DataFrame:
    """资金费率历史（fapi，8h 一条，全历史可查）。"""
    cache = RAW_DIR / f"funding_BTCUSDT_{start_ms}.parquet"
    if cache.exists() and not getattr(fetch_klines, "_no_cache", False):
        return pd.read_parquet(cache)
    rows, cur = [], start_ms
    while cur < end_ms:
        url = (f"{BINANCE_FUTURES_API}/fapi/v1/fundingRate?symbol=BTCUSDT"
               f"&startTime={cur}&endTime={end_ms}&limit=1000")
        data = http_get_json(url, timeout=15, proxy=proxy) or []
        if not data:
            break
        rows.extend(data)
        if len(data) < 1000:
            break
        cur = int(data[-1]["fundingTime"]) + 1
        time.sleep(0.12)
    df = pd.DataFrame(rows)
    if not df.empty:
        df["fundingTime"] = df["fundingTime"].astype(np.int64)
        df["fundingRate"] = df["fundingRate"].astype(float)
        df = df.drop_duplicates("fundingTime").sort_values("fundingTime")
        df.to_parquet(cache, index=False)
    return df


def fetch_fng(proxy: str | None) -> pd.DataFrame:
    """恐惧贪婪指数全量历史（alternative.me，日频）。失败返回空表。"""
    cache = RAW_DIR / "fng_history.parquet"
    if cache.exists() and not getattr(fetch_klines, "_no_cache", False):
        return pd.read_parquet(cache)
    try:
        data = http_get_json(FNG_API, timeout=15, proxy=proxy) or {}
        rows = [{"date": d["timestamp"], "value": float(d["value"])}
                for d in data.get("data", [])]
        # timestamp 是 UTC 日期字符串 "dd-mm-yyyy"
        df = pd.DataFrame(rows)
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"], format="%d-%m-%Y")
            df = df.sort_values("date")
            df.to_parquet(cache, index=False)
        return df
    except Exception:
        return pd.DataFrame(columns=["date", "value"])


# ---------------------------------------------------------------- 指标（口径对齐 hermes_collect.py）
def wilder_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    ag = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    al = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = ag / al.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    out[al == 0] = 100.0
    return out


def bb_pos(closes: pd.Series, period: int = 20) -> pd.Series:
    mid = closes.rolling(period).mean()
    std = closes.rolling(period).std(ddof=1)
    upper = mid + 2 * std
    lower = mid - 2 * std
    rng = (upper - lower).replace(0, np.nan)
    return ((closes - lower) / rng * 100).fillna(50)


def macd_hist(closes: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.Series:
    ema_f = closes.ewm(span=fast, adjust=False).mean()
    ema_s = closes.ewm(span=slow, adjust=False).mean()
    macd_line = ema_f - ema_s
    sig = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line - sig


# ---------------------------------------------------------------- 特征构建
def _precompute(k1h: pd.DataFrame, k15: pd.DataFrame, k5: pd.DataFrame,
                k4h: pd.DataFrame, funding: pd.DataFrame, fng: pd.DataFrame) -> dict:
    """build（训练）与 predict_hourly（推理）共用的指标预计算。
    唯一实现，防止训练/推理特征口径漂移。"""
    k1h = k1h.copy()
    k1h["ret"] = k1h["close"].pct_change() * 100
    k1h["up"] = (k1h["close"] >= k1h["open"]).astype(np.int8)
    k1h["rsi_1h"] = wilder_rsi(k1h["close"])
    k1h["bb_pos_1h"] = bb_pos(k1h["close"])
    k1h["macd_1h_hist"] = macd_hist(k1h["close"])
    k1h["vr_1h"] = k1h["volume"] / k1h["volume"].rolling(20).mean()
    k1h["hour"] = pd.to_datetime(k1h["open_time"], unit="ms", utc=True).dt.hour
    # 同时段历史上涨率（expanding，shift(1) 防泄漏；分钟级对齐无意义，小时级即可）
    k1h["hist_up_rate"] = (k1h.groupby("hour")["up"]
                           .transform(lambda s: s.expanding(min_periods=30).mean().shift(1)))
    k1h["roll_up_6"] = k1h["up"].rolling(6).mean()          # 最近 6 小时阳线占比
    k1h["roll_ret_24"] = k1h["close"].pct_change(24) * 100  # 24h 涨跌幅

    k4h = k4h.copy()
    k4h["rsi_4h"] = wilder_rsi(k4h["close"])
    k4h["bb_pos_4h"] = bb_pos(k4h["close"])
    k4h["macd_4h_hist"] = macd_hist(k4h["close"])

    # 5m 精简列：taker 买占比与不平衡
    k5 = k5.copy()
    k5["taker_ratio"] = k5["taker_buy_base"] / k5["volume"].replace(0, np.nan)

    fund_t = funding["fundingTime"].to_numpy() if not funding.empty else np.array([], dtype=np.int64)
    fund_v = funding["fundingRate"].to_numpy() if not funding.empty else np.array([], dtype=float)
    fng_dates = fng["date"].to_numpy() if not fng.empty else np.array([], dtype="datetime64[ns]")
    fng_vals = fng["value"].to_numpy() if not fng.empty else np.array([], dtype=float)

    return dict(
        k1h=k1h, k4h=k4h, k5=k5, fund_t=fund_t, fund_v=fund_v,
        fng_dates=fng_dates, fng_vals=fng_vals,
        t1h=k1h["open_time"].to_numpy(), t15=k15["open_time"].to_numpy(),
        t5=k5["open_time"].to_numpy(), t4h=k4h["open_time"].to_numpy(),
        c5=k5["close"].to_numpy())


def build(k1h: pd.DataFrame, k15: pd.DataFrame, k5: pd.DataFrame, k4h: pd.DataFrame,
          funding: pd.DataFrame, fng: pd.DataFrame,
          start_utc: datetime, deadzone_pct: float,
          decision_min: int = 15) -> tuple[pd.DataFrame, list[str]]:
    """逐小时构建决策时点特征 + 标签。k* 均为已按 open_time 排序的 DataFrame。

    decision_min：决策时点 = H + decision_min 分钟（特征只用到该时刻已收盘 K线）。
    """
    pc = _precompute(k1h, k15, k5, k4h, funding, fng)
    k1h = pc["k1h"]
    k5, k4h = pc["k5"], pc["k4h"]
    fund_t, fund_v = pc["fund_t"], pc["fund_v"]
    fng_dates, fng_vals = pc["fng_dates"], pc["fng_vals"]
    t1h, t15, t5, t4h, c5 = pc["t1h"], pc["t15"], pc["t5"], pc["t4h"], pc["c5"]

    rows, skipped = [], {"short_history": 0, "missing_window": 0}
    # 目标小时锚点 = ET 整点对应的 1h K线（ET 偏移为整小时，锚点即该 UTC 小时）
    start_ms = int(start_utc.timestamp() * 1000)
    end_ms = int(k1h["open_time"].iloc[-1])
    hour_ms = 3600_000

    idx = int(np.searchsorted(t1h, start_ms))
    while idx < len(k1h):
        t_anchor = int(t1h[idx])
        if t_anchor > end_ms - hour_ms:
            break  # 最后一根未收盘
        t_dec = t_anchor + decision_min * 60_000   # 决策时点 H+decision_min
        t_close = t_anchor + hour_ms
        try:
            r = _row(k1h, idx, t_anchor, t_dec, t_close, k15, k5, k4h, k4h,
                     t1h, t15, t5, t4h, c5, fund_t, fund_v, fng_dates, fng_vals,
                     deadzone_pct)
        except (IndexError, KeyError):
            skipped["missing_window"] += 1
            idx += 1
            continue
        if r is None:
            skipped["short_history"] += 1
        else:
            rows.append(r)
        idx += 1

    df = pd.DataFrame(rows)
    return df, skipped


def _row(k1h, i, t_anchor, t_dec, t_close, k15, k5, k4h, _k4h,
         t1h, t15, t5, t4h, c5, fund_t, fund_v, fng_dates, fng_vals,
         deadzone_pct) -> dict | None:
    hour_ms = 3600_000
    anchor = float(k1h["open"].iloc[i])
    if anchor <= 0:
        return None

    # 决策价 = t_dec 收盘的最新 5m 蜡烛（opened t_dec-5min, closed t_dec）
    b5c = int(np.searchsorted(t5, t_dec, side="left"))
    if b5c < 1 or int(t5[b5c - 1]) != t_dec - 5 * 60_000:
        return None
    cur = float(c5[b5c - 1])
    if t_dec > t_close - 60_000:
        return None
    # 热身：1h 指标 ≥30 根、决策可用 15m 蜡烛 ≥30 根
    # 最后已收盘 15m 蜡烛 = opened ≤ t_dec-15min（收盘时刻 ≤ t_dec）
    j15 = int(np.searchsorted(t15, t_dec - 15 * 60_000, side="right")) - 1
    # 开盘形态蜡烛固定为 [H:00,:15]
    ja = int(np.searchsorted(t15, t_anchor, side="left"))
    if i < 30 or j15 < 30 or ja >= len(k15) or int(t15[ja]) != t_anchor:
        return None
    c15 = k15.iloc[j15]      # 决策窗 15m 蜡烛
    c_open = k15.iloc[ja]    # 开盘形态蜡烛

    ret_dec = (cur - anchor) / anchor * 100  # 决策时点对锚点偏差（dev_pct 语义）

    # 24h 价格位置（5m 收盘价窗口 [dec-24h, dec)）
    lo_ms, hi_ms = t_dec - 24 * hour_ms, t_dec
    a5 = int(np.searchsorted(t5, lo_ms, side="left"))
    b5 = int(np.searchsorted(t5, hi_ms, side="left"))
    if b5 - a5 < 200:
        return None
    w5 = c5[a5:b5]
    pos24 = float((w5 < cur).mean() * 100)
    rng24 = (w5.max() - w5.min()) / cur * 100 if cur else np.nan

    # 近 60 分钟 taker 买占比 / 5m、15m 不平衡
    a60 = int(np.searchsorted(t5, t_dec - hour_ms, side="left"))
    seg5 = k5.iloc[a60:b5]
    taker_1h_buy_pct = float(seg5["taker_buy_base"].sum() / seg5["volume"].sum() * 100)
    taker_5m_last = float(seg5["taker_ratio"].iloc[-1])
    taker_15m = float(c15["taker_buy_base"] / c15["volume"]) if float(c15["volume"]) > 0 else np.nan
    imb_5m = (2 * float(seg5["taker_buy_base"].iloc[-1]) - float(seg5["volume"].iloc[-1])) / float(seg5["volume"].iloc[-1])
    tb3 = float(seg5["taker_buy_base"].iloc[-3:].sum())
    v3 = float(seg5["volume"].iloc[-3:].sum())
    imb_15m = (2 * tb3 - v3) / v3 if v3 > 0 else np.nan

    # 波动率：近 12 根 5m 收盘 pct-change 标准差（%）
    vol_5m = float(np.std(np.diff(c5[max(0, b5 - 13):b5]) / c5[max(0, b5 - 13):b5 - 1]) * 100)

    # 15m/1h 方向（对齐 recent_dir 口径：已收盘 c[-1] vs c[-3]；H 本身未收盘不可用）
    c15s = k15["close"].to_numpy()
    kline_15m = 1.0 if (j15 >= 2 and c15s[j15] >= c15s[j15 - 2]) else -1.0
    c1h = k1h["close"].to_numpy()
    kline_1h = 1.0 if c1h[i - 1] >= c1h[i - 3] else -1.0

    # 动能爆发：15m 收益 / 其 96 根滚动标准差（z-score）
    r15 = pd.Series(k15["close"]).pct_change() * 100
    std15 = float(r15.iloc[max(0, j15 - 95):j15].std())
    mom_burst = float(r15.iloc[j15] / std15) if std15 and not np.isnan(std15) else np.nan

    # 宏观趋势族：决策时点前最近 6 根【已收盘】1h / 4h 阳线计数
    # （4h 蜡烛开盘时刻 T 需满足 T+4h <= t_dec 才算已收盘，否则其收盘价含 H 的未来信息）
    up1h = k1h["up"].to_numpy()
    up4h = (k4h["close"] >= k4h["open"]).astype(np.int8).to_numpy()
    j4h = int(np.searchsorted(t4h, t_dec - 4 * hour_ms, side="right")) - 1
    macro_up_1h = float(up1h[max(0, i - 6):i].sum())
    macro_up_4h = float(up4h[max(0, j4h - 5):j4h + 1].sum()) if j4h >= 5 else np.nan

    # 上一小时惯性（H-1 已收盘）
    prev = k1h.iloc[i - 1]
    prev_chg = float(prev["ret"])
    ups = up1h[max(0, i - 6):i]
    consec = 0
    for u in ups[::-1]:
        if u == ups[-1]:
            consec += 1
        else:
            break
    prev_consec = float(consec) if consec else 0.0

    # 开盘 15 分钟形态（固定 [H:00,:15] 蜡烛）
    o_dir = 1.0 if float(c_open["close"]) >= float(c_open["open"]) else -1.0
    o_range = (float(c_open["high"]) - float(c_open["low"])) / float(c_open["open"]) * 100
    v15mean = float(k15["volume"].iloc[max(0, ja - 20):ja].mean())
    o_vol_ratio = float(c_open["volume"]) / v15mean if v15mean > 0 else np.nan

    # 资金费率（决策时点前最近一条）
    kf = int(np.searchsorted(fund_t, t_dec, side="right")) - 1
    funding_last = float(fund_v[kf]) if kf >= 0 else np.nan

    # 恐惧贪婪（决策日前最近一条）
    dec_dt = np.datetime64(t_dec, "ms")
    kg = int(np.searchsorted(fng_dates, dec_dt.astype("datetime64[ns]"), side="right")) - 1
    fng_last = float(fng_vals[kg]) if kg >= 0 else np.nan

    # 小时级指标（取自 H-1 已收盘 1h K线）
    hour_et = datetime.fromtimestamp(t_anchor / 1000, tz=timezone.utc).astimezone(ET).hour
    row = {
        "anchor_time_utc": datetime.fromtimestamp(t_anchor / 1000, tz=timezone.utc).isoformat(),
        "hour_et": hour_et,
        "anchor_price": anchor,
        # 量价与偏差
        "dev_pct": ret_dec,
        "volatility_5m": vol_5m,
        "pos24h_percentile": pos24,
        "range_24h_pct": rng24,
        "taker_1h_buy_pct": taker_1h_buy_pct,
        "taker_5m_last": taker_5m_last,
        "taker_15m": taker_15m,
        "imb_5m": imb_5m,
        "imb_15m": imb_15m,
        # K线方向与动能
        "kline_15m": kline_15m,
        "kline_1h": kline_1h,
        "momentum_burst": mom_burst,
        "macro_up_1h": macro_up_1h,
        "macro_up_4h": macro_up_4h,
        # 周期指标（H-1 收盘值）
        "rsi_1h": float(k1h["rsi_1h"].iloc[i - 1]),
        "rsi_4h": float(k4h["rsi_4h"].iloc[j4h]) if j4h >= 0 else np.nan,
        "bb_pos_1h": float(k1h["bb_pos_1h"].iloc[i - 1]),
        "bb_pos_4h": float(k4h["bb_pos_4h"].iloc[j4h]) if j4h >= 0 else np.nan,
        "macd_1h_hist": float(k1h["macd_1h_hist"].iloc[i - 1]),
        "macd_4h_hist": float(k4h["macd_4h_hist"].iloc[j4h]) if j4h >= 0 else np.nan,
        "vol_ratio_1h": float(k1h["vr_1h"].iloc[i - 1]),
        # 上一小时惯性
        "prev_chg_pct": prev_chg,
        "prev_consec": prev_consec,
        "roll_up_6h": float(k1h["roll_up_6"].iloc[i - 1]),
        "ret_24h": float(k1h["roll_ret_24"].iloc[i - 1]),
        "hist_up_rate": float(k1h["hist_up_rate"].iloc[i]),
        # 环境
        "funding_rate": funding_last,
        "fear_greed": fng_last,
        # 季节性
        "hour_sin": float(np.sin(2 * np.pi * hour_et / 24)),
        "hour_cos": float(np.cos(2 * np.pi * hour_et / 24)),
        "open_dir": o_dir,
        "open_range_pct": o_range,
        "open_vol_ratio": o_vol_ratio,
    }
    # 标签（小时 H close vs open）
    ret_h = (float(k1h["close"].iloc[i]) - anchor) / anchor * 100
    row["ret_hour"] = ret_h
    row["label"] = 1 if ret_h >= 0 else 0
    row["dead_zone"] = 1 if abs(ret_h) < deadzone_pct else 0
    return row


def main() -> int:
    global fetch_klines
    ap = argparse.ArgumentParser(description="BTC 1h 涨跌数据集历史回补")
    ap.add_argument("--start", default="2025-03-01", help="数据集起始日期 UTC（默认 2025-03-01）")
    ap.add_argument("--end", default="", help="结束日期 UTC（默认=最新已收盘小时）")
    ap.add_argument("--deadzone", type=float, default=0.1, help="死区阈值%%（默认 0.1）")
    ap.add_argument("--decision-min", type=int, default=15,
                    help="决策时点=H+该分钟数（默认 15；bot :40 下单建议 35）")
    ap.add_argument("--dms", default="",
                    help="逗号分隔多决策点（如 0,5,10,...,55）：一次拉取循环构建各 dm 数据集，"
                         "设置后忽略 --decision-min（用于批量刷新，免 12 次重复下载）")
    ap.add_argument("--proxy", default="", help="HTTP 代理（如 http://127.0.0.1:7890）")
    ap.add_argument("--no-cache", action="store_true", help="忽略缓存重新下载")
    args = ap.parse_args()

    if args.no_cache:
        fetch_klines._no_cache = True
    from hermes_common import PROXY
    proxy = args.proxy or PROXY

    start_utc = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end_utc = (datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
               if args.end else datetime.now(timezone.utc) - timedelta(hours=1))
    end_ms = int(end_utc.timestamp() * 1000)

    # 拉取区间（含滚动窗口热身缓冲）
    print(f"[backfill] 数据集区间: {args.start} → {end_utc.date()} (UTC)，proxy={bool(proxy)}")
    k1h = fetch_klines("BTCUSDT", "1h", int((start_utc - timedelta(days=130)).timestamp() * 1000), end_ms, proxy)
    k15 = fetch_klines("BTCUSDT", "15m", int((start_utc - timedelta(days=45)).timestamp() * 1000), end_ms, proxy)
    k5 = fetch_klines("BTCUSDT", "5m", int((start_utc - timedelta(days=45)).timestamp() * 1000), end_ms, proxy)
    k4h = fetch_klines("BTCUSDT", "4h", int((start_utc - timedelta(days=75)).timestamp() * 1000), end_ms, proxy)
    for name, k in [("1h", k1h), ("15m", k15), ("5m", k5), ("4h", k4h)]:
        if k.empty:
            print(f"[backfill] [X] {name} K线拉取失败（检查网络/代理）")
            return 1
        print(f"[backfill] klines {name}: {len(k)} 根 ({k['open_time'].iloc[0]} → {k['open_time'].iloc[-1]})")

    funding = fetch_funding(int((start_utc - timedelta(days=60)).timestamp() * 1000), end_ms, proxy)
    print(f"[backfill] funding: {len(funding)} 条")
    fng = fetch_fng(proxy)
    print(f"[backfill] fear&greed: {len(fng)} 天")

    dms = [int(x) for x in args.dms.split(",") if x.strip()] if args.dms.strip() \
        else [args.decision_min]
    print(f"[backfill] 构建特征矩阵: dm={dms} ...")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for dm in dms:
        df, skipped = build(k1h, k15, k5, k4h, funding, fng, start_utc, args.deadzone,
                            decision_min=dm)
        if df.empty:
            print(f"[backfill] [X] dm={dm} 无样本产出，跳过")
            continue
        suffix = "" if dm == 15 else f"_dm{dm}"
        df.to_parquet(DATA_DIR / f"dataset{suffix}.parquet", index=False)
        df.to_csv(DATA_DIR / f"dataset{suffix}.csv", index=False)

        n_lab = len(df)
        n_dead = int(df["dead_zone"].sum())
        up_pct = df["label"].mean() * 100
        manifest = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "start": args.start, "end": end_utc.isoformat(),
            "decision_min": dm,
            "n_rows": n_lab, "n_features": len(df.columns) - 5,
            "dead_zone_pct": args.deadzone, "n_dead_zone": n_dead,
            "label_up_pct": round(up_pct, 2),
            "skipped": skipped,
            "feature_cols": [c for c in df.columns if c not in
                             ("anchor_time_utc", "hour_et", "anchor_price", "ret_hour", "label", "dead_zone")],
        }
        (DATA_DIR / f"manifest{suffix}.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        lines = [
            "# 回补数据质量报告", "",
            f"- 生成时间: {manifest['generated_at_utc']}",
            f"- 样本数: {n_lab}（死区剔除 {n_dead}，有效 {n_lab - n_dead}）",
            f"- UP 占比: {up_pct:.1f}%",
            f"- 时间范围: {df['anchor_time_utc'].iloc[0]} → {df['anchor_time_utc'].iloc[-1]}",
            f"- 跳过: {skipped}", "",
            "## 各列非缺失率", "", "| 列 | 非缺失率 |", "|---|---|",
        ]
        for c in manifest["feature_cols"]:
            lines.append(f"| {c} | {df[c].notna().mean() * 100:.1f}% |")
        (DATA_DIR / f"quality_report{suffix}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

        print(f"[backfill] [OK] dm={dm}: {n_lab} 行 × {len(manifest['feature_cols'])} 特征 "
              f"→ dataset{suffix}.parquet")
        print(f"[backfill] 标签分布 UP={up_pct:.1f}%，死区样本 {n_dead}，跳过 {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
