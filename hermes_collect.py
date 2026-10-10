import os
from pathlib import Path
HERMES_DIR = Path(os.environ.get('HERMES_DIR', str(Path(__file__).resolve().parent)))
_TV_CLI = os.environ.get('TV_CLI', '')
_TV_MCP_DIR = os.environ.get('TV_MCP_DIR', '')
_TV_NODE = os.environ.get('TV_NODE', 'node')  # default to system PATH
# If TV_CLI empty, auto-detect ../../tradingview-mcp/src/cli/index.js
if not _TV_CLI:
    candidate = HERMES_DIR.parent / 'tradingview-mcp' / 'src' / 'cli' / 'index.js'
    if candidate.exists():
        _TV_CLI = str(candidate)
#!/usr/bin/env python3
"""
Hermes 数据采集脚本 — 整点 :00 运行，只拉数据+算指标，不做判断

输出: data/hermes_market_data.json（OUTPUT=DATA_DIR/...，勿用 /tmp）
由 launchd 每整点触发，AI 会话在 :02 读取本脚本输出做深度分析
"""
# pyright: reportAny=false, reportExplicitAny=false, reportUnknownVariableType=false, reportUnknownParameterType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnnecessaryIsInstance=false, reportUnreachable=false

import sys
sys.path = [p for p in sys.path if ".hermes" not in p]

import json, statistics, os, time
from typing import Any
from hermes_common import now_et, build_slug, get_et_offset, http_get_json, log, PROXY, BINANCE_API, BINANCE_FUTURES_API, tz_offset_str
from datetime import datetime, timezone
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
DATA_DIR = THIS_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

OUTPUT = DATA_DIR / "hermes_market_data.json"  # 使用项目data目录，避免/tmp权限问题

# B6(2026-08-23): 采集输出 schema 版本。字段结构变更(增删/改语义)时 +1，供回测按版本追溯。
# round41.8.10(2026-08-25): +1 → 3，polymarket 新增 up_price_delta（PM 动量）
# round41.8.11(2026-08-25): +1 → 4，polymarket 新增 book_up_bid/book_up_ask/book_imbalance（PM 挂单失衡影子采集）
# round41.8.16(2026-08-25): +1 -> 5, polymarket 新增 up_price_delta_source(PM 动量口径源 book|mark),
#   up_price_delta 语义改为同源优先真实订单簿(book-book 优先, 任一侧缺 book 整体退 mark-mark)
# round44(2026-08-26): +1 -> 6, macro_trend_1h_4h 计算挪进采集主体(collect 步骤11.1)——
#   全量快照归档自此也含该字段(此前只在 _apply_slim 计算, 全量快照恒缺, validator 从快照读取
#   恒走 consecutive_up 降级口径); 字段结构不变, 仅产出层扩展
# round44.1(2026-08-26): +1 -> 7, tf_summary 方向字段已收盘口径(recent_dir/consecutive_up/down
#   剔除最后一根进行中 K 线, 与 MACD/K线序列/41.8.5 口径统一)——字段语义变更, 回测按版本追溯
# round53(2026-09-03): +1 -> 8, polymarket 新增 up_price_delta_prev(上一小时末快照的 delta,
#   供 signal_validator reversal_intent 深跌反转意向因子[单边+1] 判据), 字段纯新增
SCHEMA_VERSION = 8
LOG = DATA_DIR / "hermes_collect.log"           # ./data/: 日志持久化
BINANCE = BINANCE_API
BINANCE_FUTURES = BINANCE_FUTURES_API
# round41.1 数据源统一迁移到 Binance 域（2026-08-22 用户指令）:
#   - 价格类（anchor/ticker/K线 1m/5m/15m/1H/4H/1D/订单簿/aggTrades）: Binance spot(api.binance.com via proxy)
#   - 资金面（Taker B/S / funding rate / OI / 多空比）: Binance fapi(fapi.binance.com via proxy)
#   - OKX 保留: 仅在 fapi 不可达(连续超时/连接失败)的最后兜底,默认不调用
# 历史背景: round8 因 Binance futures 被国内 451 封禁启用 OKX,round41.1 改回默认 Binance 域
# 历史背景二次备注: hermes_collect.py 走 PROXY=http://127.0.0.1:7890,可达 fapi.binance.com
OKX = "https://www.okx.com"
OKX_SWAP = "BTC-USDT-SWAP"
FAPI = f"{BINANCE_FUTURES}"

def fetch(url: str, use_proxy: bool = False, timeout: int = 15, retries: int = 5) -> Any | None:
    """HTTP GET JSON — 带重试，失败打印完整 URL 和异常类型。
    Binance/Gamma API 偶发抖动时单次失败就放弃会导致整小时无数据。
    8/3 BOSS 决定:重试 5 次(2s/4s/6s/8s/10s 指数 backoff),避免 SSL EOF ~10% 失败率。
    B5-fix 2026-08-23:上层的 418(封禁)/429(限流)/403/451(地域封锁) 属服务端业务判定，
    重试只会加剧封禁/白打无效请求 → 识别后立即放弃，不再盲目重试 5 次。
    """
    import urllib.error
    proxy = PROXY if use_proxy else None
    last_err: Exception | None = None
    # 封禁/限流/地域封锁码:重试无意义,直接跳过 backoff
    _FATAL_HTTP = {403, 418, 429, 451}
    for attempt in range(retries + 1):
        data, err = http_get_json(url, timeout=timeout, proxy=proxy, user_agent="hermes-collect/1.0", return_error=True)
        if data is not None:
            return data
        last_err = err
        if isinstance(err, urllib.error.HTTPError) and err.code in _FATAL_HTTP:
            log(f"  🚫 fetch 放弃({err.code} 封禁/限流/地域封锁,重试无意义) {url}")
            return None
        if attempt < retries:
            time.sleep(2 * (attempt + 1))  # 2s, 4s, 6s, 8s, 10s 指数 backoff
    err_name = type(last_err).__name__ if last_err else "unknown"
    err_msg = str(last_err)[:200] if last_err else ""
    log(f"  ⚠️ fetch 失败 (重试{retries}次仍失败) {url} | {err_name}: {err_msg}")
    return None


# B3-fix 2026-08-23: 删除 `_probe_source_fapi` 死探测。
#   1) 其产物 `_use_fapi` 在代码中从未被读取（OKX 资金面数据路径已按 round41.1
#      用户指令「数据源都使用 Binance」整体删除，无任何降级分支）；
#   2) 用被封禁(418)的 /fapi/v1/premiumIndex 探活必假阴性，且会打印误导性的
#      「资金面数据源: OKX(兜底)」谎言日志。
#   现资金面字段一律直接走 Binance fapi（Taker/funding/OI/多空比），
#   单个字段失败仅留空 + ⚠️ 标记，不再依赖探活开关。


# ==================== TV MCP 兜底（HTTP 数据源失败时用 TradingView 取 K 线）====================
_TV_CLI = os.environ.get("TV_CLI", _TV_CLI)
_TV_MCP_DIR = os.environ.get("TV_MCP_DIR", _TV_MCP_DIR)
_TV_TF_MAP = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "1H": "60", "4h": "240", "4H": "240", "1d": "1D", "1D": "1D"}
# node 完整路径（nvm 安装，launchd/PATH 不包含时直接用）
_TV_NODE = os.environ.get("TV_NODE", _TV_NODE)


def _tv_ohlcv(tf: str, count: int = 120) -> list[list[float]] | None:
    """通过 TV MCP CLI 从 TradingView 桌面应用取 K 线（HTTP 数据源兜底）。

    返回 Binance 兼容格式 [[t_ms, o, h, l, c, v], ...]（t 为毫秒时间戳），
    parse_klines 可直接消费。失败返回 None。
    """
    tv_tf = _TV_TF_MAP.get(tf)
    if not tv_tf:
        return None
    import subprocess
    try:
        env = {"TV_CDP_PORT": "9333", "PATH": f"{_TV_NODE.rsplit('/', 1)[0]}:/usr/local/bin:/usr/bin:/bin"}
        # 1. 切到目标周期
        r = subprocess.run(
            [_TV_NODE, _TV_CLI, "timeframe", tv_tf],
            capture_output=True, text=True, timeout=25, cwd=_TV_MCP_DIR, env=env,
        )
        if r.returncode != 0:
            log(f"  ⚠️ TV MCP 切周期失败({tf}): {r.stderr[:100]}")
            return None
        # 2. 取 OHLCV
        r = subprocess.run(
            [_TV_NODE, _TV_CLI, "ohlcv", "--count", str(count)],
            capture_output=True, text=True, timeout=25, cwd=_TV_MCP_DIR, env=env,
        )
        if r.returncode != 0:
            log(f"  ⚠️ TV MCP 取K线失败({tf}): {r.stderr[:100]}")
            return None
        out = json.loads(r.stdout)
        if not out.get("success") or not out.get("bars"):
            log(f"  ⚠️ TV MCP 无K线数据({tf})")
            return None
        bars: list[list[float]] = []
        for b in out["bars"]:
            try:
                # TradingView time 是秒级 → 转毫秒，兼容 parse_klines 的 t/1000 逻辑
                bars.append([
                    float(b["time"]) * 1000,
                    float(b["open"]), float(b["high"]),
                    float(b["low"]), float(b["close"]),
                    float(b["volume"]),
                ])
            except (KeyError, ValueError, TypeError):
                continue
        if bars:
            log(f"  📺 TV MCP 兜底: {tf} 取到 {len(bars)} 根 K 线")
        return bars if bars else None
    except Exception as e:
        log(f"  ⚠️ TV MCP 调用异常: {type(e).__name__}: {str(e)[:100]}")
        return None

# ==================== 技术指标 ====================

def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100
    return round(100 - 100 / (1 + avg_gain / avg_loss), 1)

def ema(data: list[float], period: int) -> float | None:
    if len(data) < period:
        return None
    k = 2 / (period + 1)
    result = sum(data[:period]) / period
    for x in data[period:]:
        result = x * k + result * (1 - k)
    return result

def _ema_series(data: list[float], period: int) -> list[float]:
    """计算 EMA 序列，返回 list"""
    if len(data) < period:
        return []
    k = 2 / (period + 1)
    ema = sum(data[:period]) / period
    result = [ema]
    for x in data[period:]:
        ema = x * k + ema * (1 - k)
        result.append(ema)
    return result

def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float | None, float | None, float | None, float | None]:
    """返回 (macd_line, signal_line, histogram, hist_prev)
    hist_prev = 前一根 histogram，用于计算加速度（动能衰竭判定）"""
    if len(closes) < slow + signal:
        return None, None, None, None
    ef = _ema_series(closes, fast)
    es = _ema_series(closes, slow)
    # 对齐两个序列（es 比 ef 短）
    offset = len(ef) - len(es)
    macd_series = [ef[i + offset] - es[i] for i in range(len(es))]
    macd_line = macd_series[-1]
    if len(macd_series) < signal:
        return round(macd_line, 2), None, None, None
    sig_series = _ema_series(macd_series, signal)
    signal_line = sig_series[-1] if sig_series else None
    hist = macd_line - signal_line if signal_line is not None else None
    # 前一根 hist（加速度计算用：hist - hist_prev > 0 = 动能增强，< 0 = 动能衰竭）
    hist_prev = None
    if len(sig_series) >= 2 and len(macd_series) >= 2:
        hist_prev = macd_series[-2] - sig_series[-2]
    return round(macd_line, 2), round(signal_line, 2) if signal_line is not None else None, round(hist, 2) if hist is not None else None, round(hist_prev, 2) if hist_prev is not None else None

def bb(closes: list[float], period: int = 20) -> tuple[float | None, float | None, float | None, float | None]:
    """返回 (middle, upper, lower, position%)"""
    if len(closes) < period:
        return None, None, None, None
    mid = sum(closes[-period:]) / period
    std = statistics.stdev(closes[-period:]) if len(closes[-period:]) > 1 else 0
    upper = mid + 2 * std
    lower = mid - 2 * std
    pos = (closes[-1] - lower) / (upper - lower) * 100 if upper != lower else 50
    return round(mid, 1), round(upper, 1), round(lower, 1), round(pos, 0)

def vol_ratio(volumes: list[float], period: int = 20) -> float | None:
    """上一根已完成 K 线的成交量 / 前 N 根均量；数据不足返回 None"""
    if len(volumes) < period + 1:
        return None
    # 上一根已完成 K 线是 volumes[-2]，它之前的 period 根
    window = volumes[-period - 1:-1]
    if len(window) < period:
        return None
    avg = sum(window) / period
    return round(volumes[-2] / avg, 2) if avg > 0 else None

# ==================== K 线解析 ====================

def parse_klines(klines: list[list[Any]]) -> list[dict[str, float]]:
    """解析 K 线，返回 OHLCV + taker_buy_base_volume 列表；跳过格式异常的行

    Binance K线 API 返回 12 个字段，第 9 个是 taker_buy_base_volume（主动买入量）。
    保留该字段用于计算订单流不平衡特征：(2*taker_buy - volume) / volume。
    """
    if not isinstance(klines, list):
        return []
    result = []
    for k in klines:
        if not isinstance(k, (list, tuple)) or len(k) < 6:
            continue
        try:
            entry: dict[str, float] = {
                "t": k[0],
                "o": float(k[1]), "h": float(k[2]),
                "l": float(k[3]), "c": float(k[4]),
                "v": float(k[5])
            }
            # 第 10 个字段（索引 9）：taker_buy_base_volume（主动买入量）
            # Binance K线字段顺序：0=t,1=o,2=h,3=l,4=c,5=v,6=close_time,7=quote_vol,8=trades,9=taker_buy_base,10=taker_buy_quote,11=ignore
            if len(k) >= 10:
                tb = float(k[9])
                entry["tb"] = tb if tb >= 0 else 0.0
            else:
                entry["tb"] = 0.0
            result.append(entry)
        except (ValueError, TypeError, IndexError):
            continue
    return result

def tf_summary(klines_parsed: list[dict[str, float]], name: str) -> dict[str, object]:
    """单周期指标摘要"""
    closes = [k["c"] for k in klines_parsed]
    opens = [k["o"] for k in klines_parsed]
    volumes = [k["v"] for k in klines_parsed]

    r = rsi(closes)
    vr = vol_ratio(volumes)
    _bmid, bupper, blower, bpos = bb(closes)
    # 8/16 修复 Bug#4：趋势类指标（MACD 系列）改用"已收盘蜡烛" closes[:-1] 计算，
    # 剔除当前未收盘蜡烛（Binance klines 最后一根是进行中的 K 线），与形态检测（[-2]）口径统一。
    # 未收盘 1H/4H hist 在 :01-:15 极不稳定，若用于 G5.1/H12/F1/BB3 深势判定会误判方向。
    # round44.1（2026-08-26 用户决策「瑕疵②现在就修」）：方向类字段全面已收盘化——
    # recent_dir / consecutive_up-down 此前仍用完整 closes（含最后一根「进行中」K 线），
    # 是 41.8.5 修复的同类残留（8/24 假末阴事故同族暴露面）。现与 MACD/形态/K线序列口径统一，
    # 仅价格展示字段（last_close/last_open）保留完整 closes。影响面=short_bounce 因子、
    # macro_trend 旧快照兜底、feature_store 特征行（仅未来行生效，历史快照存值不变）。
    _closes_closed = closes[:-1] if len(closes) >= 3 else closes
    mcdl, sigl, hist, hist_prev = macd(_closes_closed)
    # MACD 加速度 = 当前 hist - 前一根 hist（>0 动能增强，<0 动能衰竭，用于步骤7.5 大趋势健康度判定）
    macd_accel = round(hist - hist_prev, 2) if hist is not None and hist_prev is not None else None

    # 最近 3 根蜡烛方向（round41.8.5 修复：只用已收盘蜡烛，剔除 Binance 返回的最后一根
    # 「进行中」K 线，与 MACD closes[:-1] / 形态检测[-2] / macro_trend_1h_4h 口径统一。
    # 8/24 8AM ET 事故：进行中 1H 开盘仅 16 分钟暂时收阴，被当「末阴」计 -1（读数 阳阳阴），
    # 该小时最终收 +0.91% 大阳线，误导净分-6→DOWN 成交亏损 -$10。
    # 边界：<4 根时退回全列表（循环内 abs(i)<=len 自保护），与旧版行为一致不抛错。
    _closed_c = closes[:-1] if len(closes) >= 4 else closes
    _closed_o = opens[:-1] if len(opens) >= 4 else opens
    # round44.1：recent_dir 改用已收盘蜡烛比较（原 closes[-1]>=closes[-3] 含进行中 K 线）
    recent_dir = "UP" if len(_closed_c) >= 3 and _closed_c[-1] >= _closed_c[-3] else "DOWN"
    # round42.2 K线序列 3根→4根（用户 8/25 指令）：last_4 取已收盘最后 4 根；last_3 保留兼容旧快照。
    last_3 = []
    last_4 = []
    for i in range(-3, 0):
        if abs(i) <= len(_closed_c):
            last_3.append("阳" if _closed_c[i] >= _closed_o[i] else "阴")
    for i in range(-4, 0):
        if abs(i) <= len(_closed_c):
            last_4.append("阳" if _closed_c[i] >= _closed_o[i] else "阴")

    # 形态检测（用已完成 K 线 [-2]）
    last_candle = klines_parsed[-2] if len(klines_parsed) >= 2 else klines_parsed[-1]
    o, h, l, c = last_candle["o"], last_candle["h"], last_candle["l"], last_candle["c"]
    body = abs(c - o)
    is_hammer = False
    is_shooting_star = False
    if body > 0:
        lower_shadow = min(o, c) - l
        upper_shadow = h - max(o, c)
        if lower_shadow >= body * 3 and upper_shadow < body * 0.5:
            is_hammer = True
        if upper_shadow >= body * 3 and lower_shadow < body * 1.5:
            is_shooting_star = True

    # 连续阴阳线（round44.1 起只数已收盘 6 根，剔除进行中 K 线——与 recent_dir/K线序列口径统一）
    # 9/3 P1-7 修复: 原实现是「近6根中阳/阴根数」而非「连续根数」（实证 9/3 8am 快照
    # consecutive_up=4 与 consecutive_down=2 同时非零——真连续不可能），消费方
    # short_bounce「连续≥2」/exhaustion「连续同色≥5」判据被大幅放宽误触发。
    # 现改为从最后一根往前数真连续同色根数，与 prev_hour.consecutive_* 口径统一。
    up_count = 0
    for i in range(min(6, len(_closed_c))):
        if _closed_c[-1 - i] >= _closed_o[-1 - i]:
            up_count += 1
        else:
            break
    dn_count = 0
    for i in range(min(6, len(_closed_c))):
        if _closed_c[-1 - i] < _closed_o[-1 - i]:
            dn_count += 1
        else:
            break

    # 1m taker 买占比趋势（round32 新增：修复采集器从不输出 1m taker 趋势的设计缺陷）
    # prompt_exec.md 步骤7 贴线专列评分表 ×3 / 尾盘操纵否决 / 贴线模式核心信号 均依赖该字段，
    # 此前 AI 需手动用原始 tb/v 计算，且 slim 模式下 v 被 drop 无法独立算出。
    # 口径 = 买占比 = tb/v（0-1 小数），round 3 位，与 prompt_exec 的 >60%/<40%/>70% 阈值直接对应。
    # 取最近 6 根（覆盖"末 3 根连续/末根单点"判断），v=0 时用中性 0.5 防除零。
    taker_trend: list[float] = []
    if name == "1m" and klines_parsed:
        taker_ratios = [
            round(k["tb"] / k["v"], 3) if k["v"] > 0 else 0.5
            for k in klines_parsed[-6:]
        ]
        taker_trend = taker_ratios

    return {
        "name": name,
        "rsi": r,
        "bb_pos": bpos,
        "bb_upper": bupper,
        "bb_lower": blower,
        "vol_ratio": vr,
        "macd_line": mcdl,
        "macd_signal": sigl,
        "macd_hist": hist,
        "macd_hist_prev": hist_prev,
        "macd_acceleration": macd_accel,
        "last_3_candles": "".join(last_3),
        "last_4_candles": "".join(last_4),
        "recent_dir": recent_dir,
        "last_close": round(closes[-1], 1),
        "last_open": round(opens[-1], 1),
        "is_hammer": is_hammer,
        "is_shooting_star": is_shooting_star,
        "consecutive_up": up_count,
        "consecutive_down": dn_count,
        "taker_trend": taker_trend,
        "taker_pct_last": round(taker_trend[-1], 3) if taker_trend else None,
    }

# ==================== 主采集逻辑 ====================

def _apply_slim(data: dict[str, object]) -> dict[str, object]:
    """精简输出：砍掉 AI 分析不用的冗余原始数据，保留所有 prompt.md 需要的字段。

    砍掉的内容（均已由 tf_summary/key_levels/prev_hour 等算好）：
    - klines_1h 原始数组（120根，13611字符）
    - 1m recent_candles：10根→5根，只留 t/o/c/tb（round32 起 1m taker 趋势已由 tf_summary 预聚合为 timeframes.1m.taker_trend，此处保留 tb 仅为兼容冗余，AI 无需再用原始 tb 自算）
    - 5m recent_candles：全删（规则4排除5m指标）
    - 15m/1H/4H/1D recent_candles：全删（last_3_candles/consecutive 已算好）
    - order_book.bids_top10/asks_top10：删（bid_wall/ask_wall 已算好）
    - funding_history：删（AI 只看当前 funding_rate）

    预计 42526 → ~5900 字符，压缩 86%。
    """
    slim = json.loads(json.dumps(data))  # 深拷贝

    # 0. round41 新增：宏观趋势族字段（最近 4 根已收 1H 方向序列）
    # 数据源：klines_1h 原始数组（已 parse_klines），算完即删原数组（保留 slim 精简）。
    # AI 评分项"1H 4H 方向序列"读此字段（macro_trend 族，权重 ×1，涨≥3 +1 / 跌≥3 -1 / 2涨2跌 0）。
    # round41.2 审计修复：k1h[-4:] 含未收盘当前小时 K 线 → 改 k1h[-5:-1] 取已收盘 4 根
    # （核心规则5 未收盘不参考；与 MACD closes[:-1]、prev_hour done_candles[:-1] 口径统一）。
    # round44（用户 8/26 指令）：该字段计算已挪进采集主体（collect() 步骤11.1，全量快照也含），
    # 此处仅在字段缺失时兜底重算（兼容绕过主体流程直接调 _apply_slim 的旧路径），已有则不覆盖。
    if "macro_trend_1h_4h" not in slim and "klines_1h" in slim and slim["klines_1h"]:
        k1h = slim["klines_1h"]
        last4 = k1h[-5:-1]
        dirs = ["UP" if k["c"] >= k["o"] else "DOWN" for k in last4]
        slim["macro_trend_1h_4h"] = {
            "dirs": dirs,
            "up_count": sum(1 for d in dirs if d == "UP"),
            "down_count": sum(1 for d in dirs if d == "DOWN"),
            "first_ts": last4[0]["t"] if last4 else None,
            "last_ts": last4[-1]["t"] if last4 else None,
        }

    # 1. 删除 klines_1h
    if "klines_1h" in slim:
        del slim["klines_1h"]

    # 2. timeframes 内 recent_candles 处理
    for tf_name in list(slim.get("timeframes", {}).keys()):
        tf = slim["timeframes"][tf_name]
        candles = tf.get("recent_candles", [])
        if tf_name == "1m":
            # 1m taker 趋势已由 tf_summary 预聚合（timeframes.1m.taker_trend），此处保留 tb 仅为兼容冗余。
            # 只保留最近5根，只留 t/o/c/tb。
            slim_candles = [
                {"t": c["t"], "o": c["o"], "c": c["c"], "tb": c.get("tb", 0)}
                for c in candles[-5:]
            ]
            tf["recent_candles"] = slim_candles
        else:
            # 5m/15m/1H/4H/1D: 删除 recent_candles（last_3_candles 已有）
            if "recent_candles" in tf:
                del tf["recent_candles"]

    # 3. capital_flow 精简
    cf = slim.get("capital_flow", {})
    if "order_book" in cf:
        ob = cf["order_book"]
        for k in ["bids_top10", "asks_top10"]:
            if k in ob:
                del ob[k]
    if "funding_history" in cf:
        del cf["funding_history"]

    return slim


def collect(force: bool = False, slim: bool = False) -> bool:
    # 时钟校准（8/22 事故根因修复）：Claude 桌面端沙箱 VM 时钟曾落后宿主机 1:26:32，
    # 导致 slug 恒定滞后、每小时都在分析已结算的旧合约。
    # 用 Binance serverTime 做权威时钟：偏差 >60s 采用服务器时间算 slug 并告警。
    et = now_et()
    clock_skew_sec = 0.0
    try:
        st = http_get_json(f"{BINANCE_API}/api/v3/time")
        if isinstance(st, dict) and "serverTime" in st:
            skew = st["serverTime"] / 1000 - datetime.now(timezone.utc).timestamp()
            clock_skew_sec = skew
            if abs(skew) > 60:
                from zoneinfo import ZoneInfo as _ZI
                et = datetime.fromtimestamp(st["serverTime"] / 1000, tz=timezone.utc).astimezone(_ZI("America/New_York"))
                log(f"  ⚠️ 本地时钟偏差 {skew:+.0f}s（疑似沙箱 VM 时钟漂移），已采用 Binance 服务器时间计算 slug", "W")
    except Exception:
        pass  # 校准失败退回本地时钟（与旧行为一致）
    current_et = et.replace(minute=0, second=0, microsecond=0)
    slug = build_slug(current_et)

    # 幂等:如果数据已是当前小时 slug,跳过(:00 成功后 :05 补跑自动退出)
    # :00 失败时 OUTPUT 保留上一小时 slug,:05 补跑 slug 不匹配 → 正常执行
    # 2026-08-03 修复:加 --force 参数,允许同小时强制重拉(给 cron 每整点 :15 跑用)
    if OUTPUT.exists() and not force:
        try:
            existing = json.loads(OUTPUT.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and existing.get("slug") == slug:
                log(f"⏭️ 数据已是当前小时 ({slug}),跳过采集")
                return True
        except (json.JSONDecodeError, OSError):
            pass  # 文件损坏,继续采集
    elif force:
        log(f"🔄 --force 强制重拉(跳过去重),slug={slug}")

    log(f"=== 采集 {slug} | ET {current_et.strftime('%Y-%m-%d %H:00')} ===")

    data: dict[str, object] = {
        "timestamp": current_et.strftime(f"%Y-%m-%dT%H:%M:00{tz_offset_str()}"),
        "slug": slug,
        "collect_time_utc": et.astimezone(timezone.utc).isoformat(),
        "clock_skew_sec": round(clock_skew_sec, 1),
    }

    # ===== 1. K 线数据（多周期，8/9 起 HTTP 失败时 TV MCP 兜底）=====
    log("  拉取 K 线数据...")
    k1m_raw = fetch(f"{BINANCE}/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=120", use_proxy=True)
    k5m_raw = fetch(f"{BINANCE}/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=60", use_proxy=True)
    k15m_raw = fetch(f"{BINANCE}/api/v3/klines?symbol=BTCUSDT&interval=15m&limit=40", use_proxy=True)
    k1h_raw = fetch(f"{BINANCE}/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=120", use_proxy=True)
    k4h_raw = fetch(f"{BINANCE}/api/v3/klines?symbol=BTCUSDT&interval=4h&limit=50", use_proxy=True)
    k1d_raw = fetch(f"{BINANCE}/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=50", use_proxy=True)

    # 关键周期缺失时用 TV MCP(TradingView) 兜底，避免整小时无数据
    if not k1h_raw:
        log("  ⚠️ Binance 1H K线失败，尝试 TV MCP 兜底...")
        k1h_raw = _tv_ohlcv("1H", 120)
    if not k15m_raw:
        log("  ⚠️ Binance 15m K线失败，尝试 TV MCP 兜底...")
        k15m_raw = _tv_ohlcv("15m", 60)
    if not k5m_raw:
        log("  ⚠️ Binance 5m K线失败，尝试 TV MCP 兜底...")
        k5m_raw = _tv_ohlcv("5m", 60)
    if not k1m_raw:
        log("  ⚠️ Binance 1m K线失败，尝试 TV MCP 兜底...")
        k1m_raw = _tv_ohlcv("1m", 120)
    if not k4h_raw:
        k4h_raw = _tv_ohlcv("4H", 50)
    if not k1d_raw:
        k1d_raw = _tv_ohlcv("1D", 50)

    if not k1h_raw or not k15m_raw or not k5m_raw or not k1m_raw:
        log("❌ 关键 K 线数据缺失（含 TV MCP 兜底后仍缺），无法继续")
        return False

    timeframes = {}
    for name, k_raw in [("1m", k1m_raw), ("5m", k5m_raw), ("15m", k15m_raw),
                         ("1H", k1h_raw), ("4H", k4h_raw), ("1D", k1d_raw)]:
        if k_raw:
            parsed = parse_klines(k_raw)
            tf_data = tf_summary(parsed, name)
            # 保留最近 10 根 K 线原始数据供 AI 细看
            tf_data["recent_candles"] = parsed[-10:]
            timeframes[name] = tf_data
            log(f"  {name}: RSI={tf_data['rsi']} BB={tf_data['bb_pos']}% Vol={tf_data['vol_ratio']}x MACD={tf_data['macd_hist']}")

    data["timeframes"] = timeframes
    # round42.3(2026-08-25 用户决策):1m taker 顶层 alias, AI 直读 taker_pct_last/taker_trend
    tf_1m = timeframes.get("1m", {}) if isinstance(timeframes, dict) else {}
    data["taker_pct_last"] = tf_1m.get("taker_pct_last")
    data["taker_trend"] = tf_1m.get("taker_trend")
    # 原始 1h K线序列（机构模型特征计算用，训练/推理同一函数）
    data["klines_1h"] = parse_klines(k1h_raw)

    # 5m 波动率（prompt 步骤4 用）：近12根5m收盘价收益率的样本std × √12 换算为1h波动率，单位%
    k5m_parsed_for_vol = parse_klines(k5m_raw)
    if len(k5m_parsed_for_vol) >= 12:
        closes_5m = [k["c"] for k in k5m_parsed_for_vol[-12:]]
        if all(c > 0 for c in closes_5m):
            rets_5m = [closes_5m[i] / closes_5m[i - 1] - 1 for i in range(1, len(closes_5m))]
            data["volatility_5m"] = round(statistics.stdev(rets_5m) * (12 ** 0.5) * 100, 4)
            log(f"  5m波动率(1h换算): {data['volatility_5m']}%")

    # ===== 2. 锚定价 & 当前价格 =====
    k1m_parsed_for_price = parse_klines(k1m_raw)
    if not k1m_parsed_for_price:
        log("❌ k1m 解析后为空", "E")
        return False
    current_price = k1m_parsed_for_price[-1]["c"]
    # B5: 优先使用 Polymarket 官方 priceToBeat，比 Binance.US K 线开盘价更准确
    ptb_url = f"https://gamma-api.polymarket.com/markets?slug={slug}"
    ptb_raw = fetch(ptb_url, use_proxy=True)
    anchor_price = None
    if ptb_raw and len(ptb_raw) > 0:
        ptb_events = ptb_raw[0].get("events") or [{}]
        if ptb_events:
            ptb_meta = ptb_events[0].get("eventMetadata", {})
            ptb_val = ptb_meta.get("priceToBeat")
            if ptb_val:
                anchor_price = float(ptb_val)
                log(f"  锚定价(priceToBeat): ${anchor_price}")
    if anchor_price is None:
        anchor_price = float(k1h_raw[-1][1])  # 回退到 Binance.com 1H 开盘价
        log(f"  锚定价(Binance.com): ${anchor_price}")
    data["anchor_price"] = round(anchor_price, 2)
    data["current_price"] = round(current_price, 2)
    data["deviation"] = round(current_price - anchor_price, 2)

    # 偏差趋势（贴线尾盘反手条款用：取最近3分钟偏差序列，判断扩大/稳定/缩小）
    # 单次采集时只有当前点，需依赖 k1m 历史推算
    if len(k1m_parsed_for_price) >= 3:
        dev_series = []
        for k in k1m_parsed_for_price[-3:]:
            dev_series.append(round(k["c"] - anchor_price, 2))
        dev_pct_series = [round(d / anchor_price * 100, 4) for d in dev_series] if anchor_price else []
        if len(dev_pct_series) >= 3:
            # 阈值式判定（替代严格单调，避免噪声抖动导致 expanding 漏报 / stable 滥用）：
            # expanding: 末值绝对值较首值放大 >=30% 且末值超过 0.01% 噪声地板，且首尾同向（跨锚定翻转不算单边扩大）
            # shrinking: 末值绝对值较首值缩小 >=30%
            # 其余为 stable
            abs_first = abs(dev_pct_series[0])
            abs_last = abs(dev_pct_series[-1])
            same_dir = dev_pct_series[0] == 0 or (dev_pct_series[0] > 0) == (dev_pct_series[-1] > 0)
            expanding = same_dir and abs_last > max(abs_first * 1.3, 0.01)
            shrinking = abs_last < abs_first * 0.7
            stable = not expanding and not shrinking
            data["deviation_trend"] = {
                "last_3min_dev_pct": dev_pct_series,
                "expanding": expanding,
                "stable": stable,
                "shrinking": shrinking,
            }

    # ===== 3. 24h Ticker =====
    # B4-fix 2026-08-23: 接口异常时可能返回 dict/错结构，缺 isinstance dict 守卫会让
    #   `ticker["highPrice"]` KeyError/TypeError 崩掉整轮采集 → 本轮零快照不可逆。
    ticker = fetch(f"{BINANCE}/api/v3/ticker/24hr?symbol=BTCUSDT", use_proxy=True)
    if isinstance(ticker, dict):
        try:
            high_24h = float(ticker["highPrice"])
            low_24h = float(ticker["lowPrice"])
        except (KeyError, TypeError, ValueError):
            high_24h = low_24h = None
            log("  ⚠️ 24h Ticker: 返回结构缺 highPrice/lowPrice,字段留空")
        if high_24h is not None and low_24h is not None:
            data["ticker_24h"] = {
                "high": high_24h,
                "low": low_24h,
                "volume": float(ticker.get("volume", 0) or 0),
                "quote_volume": float(ticker.get("quoteVolume", 0) or 0),
                "change_pct": float(ticker.get("priceChangePercent", 0) or 0),
            }
            # 24h 价格位置（庄家派发区/吸筹区判定用：>50% 为上半区=派发风险，<50% 为下半区=吸筹风险）
            if high_24h > low_24h:
                percentile = (current_price - low_24h) / (high_24h - low_24h) * 100
                data["price_position_24h"] = {
                    "high_24h": round(high_24h, 2),
                    "low_24h": round(low_24h, 2),
                    "current_percentile": round(percentile, 1),
                    "in_upper_half": percentile > 50,
                    "in_lower_half": percentile <= 50,
                }
                log(f"  24h位置: {percentile:.1f}% ({'上半区-派发风险' if percentile > 50 else '下半区-吸筹风险'})")
    else:
        log("  ⚠️ 24h Ticker: 返回非 dict(Binance 限流/错误),字段留空,不中断采集(round41.1 Binance 域)")

    # ===== 4. 资金面数据 =====
    # round41.1(B3-fix 2026-08-23): 数据源统一 Binance 域,无探活开关、无 OKX 降级。
    #   资金面全部直接走 fapi(Binance 期货)。单个字段失败 → 留空 + ⚠️ 标缺失
    #   (用户 8/22 12:28 BJT 明确指令"数据源都使用 Binance 的数据")
    log("  拉取资金面数据...")
    capital_flow: dict[str, object] = {}

    # round42.3-fix(2026-08-25 审核发现 bug 1):fapi 失败时 alias key 必须预初始化 None
    # 否则 AI 层 data["taker_5m_ratio"] 直读会 KeyError 崩溃(sand fapi 假阴性已实证)
    data["taker_5m_ratio"] = None
    data["taker_15m_ratio"] = None
    data["taker_1H_ratio"] = None
    data["taker_1H_avg"] = None

    # 4.1 Taker 买卖比（5m 周期，最近 12 根）
    #   Binance fapi 格式: {"buySellRatio":x,"buyVol":y,"sellVol":z}
    taker_f = fetch(f"{FAPI}/futures/data/takerlongshortRatio?symbol=BTCUSDT&period=5m&limit=12", timeout=10)
    if taker_f and isinstance(taker_f, list) and taker_f:
        ratios = []
        for r in taker_f:
            try:
                ratios.append(float(r.get("buySellRatio", 0)))
            except (ValueError, TypeError):
                continue
        if ratios:
            capital_flow["taker_bs_ratio"] = round(ratios[-1], 3)
            capital_flow["taker_bs_trend"] = [round(r, 2) for r in ratios[-6:]]  # 最近 30 分钟趋势
            capital_flow["taker_bs_avg_1h"] = round(sum(ratios[-12:]) / 12, 3)
            # round42.3(2026-08-25 用户决策):AI 层直读顶层键 alias, 兼容嵌套路径同时减少路径错误
            # 8/25 8AM ET S₀=-4 vs 引擎 praw=0.846 偏差事故, AI 把嵌套字段读成 None=0
            data["taker_5m_ratio"] = capital_flow["taker_bs_ratio"]
            data["taker_15m_ratio"] = round(ratios[-3], 3) if len(ratios) >= 3 else None  # 15min 前的 5m 末根近似
            data["taker_1H_ratio"] = capital_flow["taker_bs_avg_1h"]
            data["taker_1H_avg"] = capital_flow["taker_bs_avg_1h"]
            log(f"  Taker B/S: 当前={ratios[-1]:.2f} 1H均={sum(ratios[-12:])/12:.2f} (Binance fapi)")
        else:
            log("  ⚠️ Taker B/S: fapi 返回数据无法解析,字段留空(round41.1 统一 Binance 域,不降级 OKX)")
    else:
        log("  ⚠️ Taker B/S: fapi 不可达,字段留空(round41.1 统一 Binance 域,不降级 OKX)")

    # 4.2 资金费率 + 历史（B1-fix 2026-08-23: 不再依赖被封禁的 premiumIndex 端点。
    #   改用独立可用的 /fapi/v1/fundingRate?limit=40,一次拿到历史序列,
    #   最新一根即当前资金费率。premiumIndex 因 418 封禁在真实环境必失败,
    #   旧逻辑导致 funding_rate/funding_history 双双永远留空。）
    funding_hist_f = fetch(f"{FAPI}/fapi/v1/fundingRate?symbol=BTCUSDT&limit=40", timeout=10)
    if funding_hist_f and isinstance(funding_hist_f, list) and funding_hist_f:
        # 解析容错:非 dict 行 / fundingRate 无法转 float 的行跳过,不因单行崩掉整轮采集
        funding_history = []
        for _x in funding_hist_f:
            if not isinstance(_x, dict):
                continue
            try:
                _fr = float(_x.get("fundingRate"))
            except (TypeError, ValueError):
                continue
            funding_history.append({"fundingRate": _fr, "fundingTime": _x.get("fundingTime", "")})
        if funding_history:
            capital_flow["funding_history"] = funding_history
            capital_flow["funding_rate"] = float(funding_history[-1]["fundingRate"])
            log(f"  资金费率: {capital_flow['funding_rate']*100:.4f}% 历史{len(funding_history)}根 (Binance fapi fundingRate)")
        else:
            log("  ⚠️ 资金费率: fundingRate 返回无法解析(0 条有效),字段留空")
    else:
        log("  ⚠️ 资金费率: fapi fundingRate 不可达,字段留空(round41.1 统一 Binance 域,不降级 OKX)")

    # 4.3 持仓量（趋势，Binance fapi openInterestHist [sumOpenInterest]）
    oi_hist = None
    oi_f = fetch(f"{FAPI}/futures/data/openInterestHist?symbol=BTCUSDT&period=1h&limit=24", timeout=10)
    if oi_f and isinstance(oi_f, list) and oi_f:
        try:
            oi_values = [float(x.get("sumOpenInterest", 0)) for x in oi_f]
            if oi_values[0] > 0 and len(oi_values) >= 2:
                capital_flow["open_interest"] = oi_values[-1]
                capital_flow["oi_change_24h_pct"] = round((oi_values[-1] - oi_values[0]) / oi_values[0] * 100, 2)
                log(f"  持仓量 24h 变化: {capital_flow['oi_change_24h_pct']:+.2f}% (Binance fapi)")
            oi_hist = [[ts, oi] for ts, oi in [(int(x.get("timestamp", 0)), float(x.get("sumOpenInterest", 0))) for x in oi_f]]
        except (ValueError, TypeError):
            oi_hist = None
    else:
        log("  ⚠️ 持仓量: fapi 不可达,字段留空(round41.1 统一 Binance 域,不降级 OKX)")

    # 4.4 多空比（Binance fapi globalLongShortAccountRatio [longShortRatio]）
    lsr_f = fetch(f"{FAPI}/futures/data/globalLongShortAccountRatio?symbol=BTCUSDT&period=5m&limit=12", timeout=10)
    if lsr_f and isinstance(lsr_f, list) and lsr_f:
        ratios = []
        for r in lsr_f:
            try:
                ratios.append(float(r.get("longShortRatio", 0)))
            except (ValueError, TypeError):
                continue
        if ratios:
            capital_flow["long_short_ratio"] = round(ratios[-1], 3)
            capital_flow["long_short_ratio_trend"] = [round(r, 3) for r in ratios[-6:]]
            log(f"  多空比: {ratios[-1]:.3f} (Binance fapi)")
        else:
            log("  ⚠️ 多空比: fapi 返回数据无法解析,字段留空(round41.1 统一 Binance 域,不降级 OKX)")
    else:
        log("  ⚠️ 多空比: fapi 不可达,字段留空(round41.1 统一 Binance 域,不降级 OKX)")

    # 4.5 大户多空比（OKX 无现货接口，置空由分析端降级处理）

    # 4.6 订单簿深度
    depth = fetch(f"{BINANCE}/api/v3/depth?symbol=BTCUSDT&limit=20", use_proxy=True)
    if depth and isinstance(depth.get("bids"), list) and isinstance(depth.get("asks"), list):
        bids: list[list[float]] = []
        asks: list[list[float]] = []
        try:
            bids = [[float(p), float(q)] for p, q in depth["bids"][:10]]
            asks = [[float(p), float(q)] for p, q in depth["asks"][:10]]
        except (ValueError, TypeError, IndexError) as e:
            log(f"  ⚠️ 订单簿数据格式异常: {e}")
            depth = None  # 降级为无订单簿数据
        bid_vol = round(sum(q for _, q in bids), 2)
        ask_vol = round(sum(q for _, q in asks), 2)
        capital_flow["order_book"] = {
            "bids_top10": bids,
            "asks_top10": asks,
            "bid_wall": max(bids, key=lambda x: x[1])[0] if bids else None,
            "ask_wall": max(asks, key=lambda x: x[1])[0] if asks else None,
            "bid_volume_top10": bid_vol,
            "ask_volume_top10": ask_vol,
        }
        log(f"  订单簿: 买墙={capital_flow['order_book']['bid_wall']} 卖墙={capital_flow['order_book']['ask_wall']} 买量/卖量={bid_vol/ask_vol:.2f}" if ask_vol > 0 else "  订单簿: 解析完成")

    # 4.7 订单流不平衡（aggTrades 主动买卖量差）—— 预测力最强的微观结构特征
    # round41.4（C2 方案C）：分页拉满 15 分钟真实窗口。旧逻辑只取最近 1000 笔，活跃时段仅覆盖
    # 1-3 分钟，且 5m/15m 两窗口退化为同一批数据（历史 76% 场数值完全相同），"双窗口共振"失真。
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - 15 * 60 * 1000
    end_ms = now_ms
    agg: list = []
    # round42.3-fix(2026-08-25 审核发现 bug 2):aggTrades 失败时 alias key 必须预初始化 None
    # 否则 AI 层 data["order_flow_5m"] 直读会 KeyError 崩溃(proxy 假阴性已实证)
    data["order_flow_5m"] = None
    data["order_flow_15m"] = None
    cursor = start_ms
    _seen_ids: set = set()
    for _page in range(40):  # 安全上限 40 页 ≈ 4 万笔，足够最活跃的 15 分钟
        batch = fetch(f"{BINANCE}/api/v3/aggTrades?symbol=BTCUSDT&startTime={cursor}&endTime={end_ms}&limit=1000", use_proxy=True)
        if not batch or not isinstance(batch, list):
            break
        for t in batch:
            tid = t.get("a") if isinstance(t, dict) else None
            if tid is not None:
                if tid in _seen_ids:
                    continue  # 同毫秒游标翻页可能重复返回，按聚合成交ID去重
                _seen_ids.add(tid)
            agg.append(t)
        if len(batch) < 1000:
            break  # 已拉到 endTime，取全
        last_t = int(batch[-1].get("T", 0) or 0)
        if last_t <= cursor or last_t >= end_ms - 1:
            break
        cursor = last_t
    if agg:
        order_flow: dict[str, object] = {}
        earliest_ts = min((int(t.get("T", 0)) for t in agg if isinstance(t, dict)), default=0)
        for window_min in (5, 15):
            cutoff = now_ms - window_min * 60 * 1000
            buy_vol = 0.0
            sell_vol = 0.0
            count = 0
            for t in agg:
                if not isinstance(t, dict) or int(t.get("T", 0)) < cutoff:
                    continue
                qty = float(t.get("q", 0))
                if t.get("m"):  # m=true → 买方是 maker → 主动卖
                    sell_vol += qty
                else:
                    buy_vol += qty
                count += 1
            total = buy_vol + sell_vol
            order_flow[f"imbalance_{window_min}m"] = round((buy_vol - sell_vol) / total, 4) if total > 0 else 0.0
            order_flow[f"trades_{window_min}m"] = count
            # round50(8/28 用户决策,影子采集):Binance 大单监控(>=50 BTC 单笔视为大单)
            # 当前 `agg` 列表已含单笔明细, 直接聚合, 不重拉。三档输出:
            #   large_volume_*m = 大单总成交 BTC 量, large_count_*m = 大单笔数
            #   large_imbalance_*m = (大单买量 - 大单卖量) / 大单总量, [-1, +1], 0 = 大单均衡
            LARGE_TRADE_THRESHOLD_BTC = 50.0  # round50 用户决策阈值
            large_buy_vol = 0.0
            large_sell_vol = 0.0
            large_count = 0
            for t in agg:
                if not isinstance(t, dict) or int(t.get("T", 0)) < cutoff:
                    continue
                qty = float(t.get("q", 0))
                if qty >= LARGE_TRADE_THRESHOLD_BTC:
                    large_count += 1
                    if t.get("m"):
                        large_sell_vol += qty
                    else:
                        large_buy_vol += qty
            large_total = large_buy_vol + large_sell_vol
            order_flow[f"large_volume_{window_min}m"] = round(large_total, 2)
            order_flow[f"large_count_{window_min}m"] = large_count
            order_flow[f"large_imbalance_{window_min}m"] = round((large_buy_vol - large_sell_vol) / large_total, 4) if large_total > 0 else 0.0
        # 数据完整性（round41.4 新口径）：实际覆盖 <90%×15min 视为 partial；输出真实覆盖分钟数
        covered_ms = max(0, now_ms - earliest_ts) if earliest_ts else 0
        order_flow["effective_window_min"] = round(covered_ms / 60000, 1)
        order_flow["partial"] = covered_ms < 0.9 * 15 * 60 * 1000
        capital_flow["order_flow"] = order_flow
        # round42.3(2026-08-25 用户决策):订单流顶层 alias, AI 直读 order_flow_5m/order_flow_15m
        data["order_flow_5m"] = order_flow["imbalance_5m"]
        data["order_flow_15m"] = order_flow["imbalance_15m"]
        # round50(8/28 用户决策,影子采集):大单监控顶层 alias, 不进 S0, 仅供 round50 回测识别
        data["large_trade_5m"] = order_flow["large_imbalance_5m"]
        data["large_trade_15m"] = order_flow["large_imbalance_15m"]
        data["large_trade_volume_5m"] = order_flow["large_volume_5m"]
        data["large_trade_volume_15m"] = order_flow["large_volume_15m"]
        data["large_trade_count_5m"] = order_flow["large_count_5m"]
        data["large_trade_count_15m"] = order_flow["large_count_15m"]
        log(f"  订单流: 5m不平衡={order_flow['imbalance_5m']} 15m={order_flow['imbalance_15m']} ({len(agg)}笔/{order_flow['effective_window_min']}min, partial={order_flow['partial']})")
        log(f"  大单监控: 5m imb={order_flow['large_imbalance_5m']} 笔={order_flow['large_count_5m']} | 15m imb={order_flow['large_imbalance_15m']} 笔={order_flow['large_count_15m']} (>=50 BTC)")

    data["capital_flow"] = capital_flow

    # ===== 5. 情绪面（拉 4 天数据用于 3 天趋势）=====
    log("  拉取情绪面数据...")
    fng = fetch("https://api.alternative.me/fng/?limit=4", timeout=10)
    if fng and "data" in fng and len(fng["data"]) > 0:
        try:
            trend = [int(x["value"]) for x in fng["data"]]  # [今天, 昨天, 前天, 大前天]
        except (KeyError, TypeError, ValueError) as e:
            log(f"  ⚠️ 恐贪指数趋势解析失败: {e}", "W")
            data["sentiment"] = None
        else:
            d = fng["data"][0]
            try:
                data["sentiment"] = {
                    "fear_greed_index": int(d["value"]),
                    "fear_greed_label": d.get("value_classification", ""),
                    "fear_greed_trend": trend,
                }
                log(f"  恐贪指数: {d['value']} ({d.get('value_classification', '')}) | 3天趋势: {trend}")
            except (KeyError, TypeError, ValueError) as e:
                log(f"  ⚠️ 恐贪指数解析失败: {e}", "W")
                data["sentiment"] = None
    else:
        data["sentiment"] = None

    # ===== 6. 历史统计 =====
    log("  计算历史统计...")
    target_hour_utc = (current_et.hour + get_et_offset()) % 24
    k1h_hist = fetch(f"{BINANCE}/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=1000", use_proxy=True)
    if k1h_hist:
        k1h_hist_parsed = parse_klines(k1h_hist)
        hist_up = 0; hist_total = 0; hist_changes = []
        for k in k1h_hist_parsed:
            t = datetime.fromtimestamp(k["t"]/1000, tz=timezone.utc)
            if t.hour == target_hour_utc:
                oc, cc = k["o"], k["c"]
                if cc >= oc:
                    hist_up += 1
                hist_total += 1
                hist_changes.append((cc - oc) / oc * 100)
        if hist_total > 0:
            # 跨平台 12 小时制格式（避免 %-I 在 Windows/macOS 旧版不兼容）
            hour_12 = current_et.hour % 12 or 12
            ampm = "AM" if current_et.hour < 12 else "PM"
            hour_label = f"{hour_12}{ampm} ET"
            data["historical"] = {
                "target_hour": hour_label,
                "up_rate": round(hist_up / hist_total, 3),
                "total_samples": hist_total,
                "avg_change_pct": round(sum(hist_changes) / len(hist_changes), 3) if hist_changes else 0,
                "std_change_pct": round(statistics.stdev(hist_changes), 3) if len(hist_changes) > 1 else 0,
            }
            log(f"  历史 {hour_label}: UP率={hist_up/hist_total*100:.1f}% ({hist_up}/{hist_total}) 均值={sum(hist_changes)/len(hist_changes):+.2f}%")

    # ===== 7. Polymarket 数据（复用锚定价时已拉取的 ptb_raw）=====
    log("  处理 Polymarket 数据...")
    gamma = ptb_raw or fetch(f"https://gamma-api.polymarket.com/markets?slug={slug}", use_proxy=True)
    if gamma and len(gamma) > 0:
        m = gamma[0]
        prices_raw = m.get("outcomePrices", "[]")
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        up_price = float(prices[0]) if len(prices) > 0 else 0.5
        data["polymarket"] = {
            "up_price": up_price,
            "down_price": float(prices[1]) if len(prices) > 1 else 0.5,
            "volume": float(m.get("volume", 0)),
            "liquidity": float(m.get("liquidity", 0)),
            "active": m.get("active", False),
            "closed": m.get("closed", False),
        }
        # round41.8.11 PM 挂单失衡（影子采集，不进评分）：拉 clob 订单簿，算 book 指标攒历史。
        # ⚠️ clob 无历史接口，只能从现在起逐小时积累，攒 ≥2 周（~300 笔）后用 factor_candidate_backtest 回测。
        try:
            _cids = m.get("clobTokenIds")
            _cids = json.loads(_cids) if isinstance(_cids, str) else _cids
            if _cids and m.get("enableOrderBook"):
                _book = fetch(f"https://clob.polymarket.com/book?token_id={_cids[0]}", use_proxy=True, timeout=15)
                if _book:
                    _bids = [(float(b["price"]), float(b["size"])) for b in _book.get("bids", [])]
                    _asks = [(float(a["price"]), float(a["size"])) for a in _book.get("asks", [])]
                    _bb = max((p for p, _ in _bids), default=None)   # 最高买价（不依赖排序）
                    _ba = min((p for p, _ in _asks), default=None)   # 最低卖价
                    _bid_mid = sum(s for p, s in _bids if 0.3 <= p <= 0.7)   # 中间价区间买盘量
                    _ask_mid = sum(s for p, s in _asks if 0.3 <= p <= 0.7)   # 中间价区间卖盘量
                    _imb = round((_bid_mid - _ask_mid) / (_bid_mid + _ask_mid), 4) if (_bid_mid + _ask_mid) > 0 else None
                    _bm = round((_bb + _ba) / 2, 4) if (_bb is not None and _ba is not None) else None
                    data["polymarket"]["book_up_bid"] = _bb
                    data["polymarket"]["book_up_ask"] = _ba
                    data["polymarket"]["book_up_mid"] = _bm
                    data["polymarket"]["book_imbalance"] = _imb
                    log(f"  PM 订单簿: best bid={_bb} ask={_ba} mid={_bm} imbalance={_imb}")
        except Exception as _e:
            log(f"  PM 订单簿: 拉取失败(不影响采集): {_e}", "W")
        # round50(8/28 用户决策, 影子采集): Polymarket 实际成交流水(全市场视角, 不分聪明钱)
        # 数据源 data-api.polymarket.com/trades, 按 conditionId 拉近 15 分钟所有 taker 成交流水
        # 聚合 BUY/SELL × UP(0)/DOWN(1) 四象限, 输出 net_up_imbalance = (buy_up - sell_up) / total_up
        # net_up_imbalance ∈ [-1, +1]: +1=全买UP, -1=全卖UP, 0=UP侧平衡
        # fail-open: 拉取失败/conditionId 缺失/响应非 list 全部降级 None, 不影响主流程
        try:
            _cid_trades = m.get("conditionId")
            if _cid_trades:
                _now_ts = int(datetime.now(timezone.utc).timestamp())
                _start_ts = _now_ts - 15 * 60
                _trades_url = (
                    f"https://data-api.polymarket.com/trades"
                    f"?market={_cid_trades}&takerOnly=true&limit=200"
                    f"&start={_start_ts}&end={_now_ts}"
                )
                _trades = fetch(_trades_url, use_proxy=True, timeout=15)
                if _trades and isinstance(_trades, list):
                    _buy_up = 0.0
                    _sell_up = 0.0
                    _buy_down = 0.0
                    _sell_down = 0.0
                    for _t in _trades:
                        if not isinstance(_t, dict):
                            continue
                        _sz = float(_t.get("size", 0) or 0)
                        _side = _t.get("side")
                        _oi = _t.get("outcomeIndex")
                        if _oi == 0:  # UP
                            if _side == "BUY":
                                _buy_up += _sz
                            elif _side == "SELL":
                                _sell_up += _sz
                        elif _oi == 1:  # DOWN
                            if _side == "BUY":
                                _buy_down += _sz
                            elif _side == "SELL":
                                _sell_down += _sz
                    _total_up = _buy_up + _sell_up
                    _total_down = _buy_down + _sell_down
                    data["polymarket"]["trades_15m"] = {
                        "buy_up_size": round(_buy_up, 2),
                        "sell_up_size": round(_sell_up, 2),
                        "buy_down_size": round(_buy_down, 2),
                        "sell_down_size": round(_sell_down, 2),
                        "net_up_imbalance": round((_buy_up - _sell_up) / _total_up, 4) if _total_up > 0 else None,
                        "net_down_imbalance": round((_buy_down - _sell_down) / _total_down, 4) if _total_down > 0 else None,
                        "trade_count": len(_trades),
                    }
                    log(f"  PM 实际成交流水: UP侧净买入={data['polymarket']['trades_15m']['net_up_imbalance']} ({len(_trades)}笔)")
        except Exception as _e:
            log(f"  PM 实际成交流水: 拉取失败(影子采集不影响主流程): {_e}", "W")
        # round50 v2(8/28 用户决策, 影子采集): 聪明钱共识 - 改用 /holders 端点
        # 取当前 slug 持仓金额前 N 的钱包, 而非全平台 leaderboard。
        # 原因: 全平台盈利榜(政治/长线)与 BTC 1H 玩家池几乎无重叠 (实测 0/50 命中);
        # 当前 slug 持仓大户 = 真在该市场下注的人, 信号才靠谱。
        # 端点: data-api.polymarket.com/holders?market={cid}&limit=20 (limit 每 token 各 20)
        # 返回结构: [{token, holders: [{proxyWallet, amount, outcomeIndex, name}, ...]}, ...]
        #   - outcomeIndex=0 -> UP 侧持仓榜; outcomeIndex=1 -> DOWN 侧持仓榜
        # 第二步: UP/DOWN 两侧持仓大户各查 /trades 近 1h 成交, 聚合 BUY vs SELL 净方向
        # 性能: 1 次 holders + 40 次 trades × 0.1s sleep ≈ 8s (vs v1 leaderboard 50×0.2s ≈ 41s)
        # fail-open: 任一环节失败整体降级 None, 不影响主流程
        # round50 v3(8/28 用户决策, 影子采集强化): 聪明钱钱包账本三件套
        #   ① DOWN 侧大户也查 trades(v2 只查 UP 侧 = 口径盲区, 大户叙事只看半池)
        #   ② 本小时 holders 明细(UP20+DOWN20)全量落盘 data/hermes_smart_money_wallets.jsonl
        #      -> 结算后 scripts/smart_money_settle.py 算账 -> scripts/smart_money_pool.py 滚动筛池
        #   ③ 读 data/hermes_smart_money_pool.json 标记池内钱包持仓方向(文件不存在 fail-open)
        #   池子需 24-72h 账本积累才成型, smart_money_pool_direction 仅影子不进评分, round51 回测裁决
        try:
            _cid_sm = m.get("conditionId")
            if _cid_sm:
                _now_ts = int(datetime.now(timezone.utc).timestamp())
                _start_ts_sm = _now_ts - 60 * 60  # 近 1 小时
                _holders_url = (
                    f"https://data-api.polymarket.com/holders"
                    f"?market={_cid_sm}&limit=20"
                )
                _holders_resp = fetch(_holders_url, use_proxy=True, timeout=15)
                # 提取 UP/DOWN 两侧持仓前 20 钱包
                _top_holders_up = []
                _top_holders_down = []
                _top_holders_volume_up = 0.0
                _top_holders_volume_down = 0.0
                if _holders_resp and isinstance(_holders_resp, list):
                    for _tok_entry in _holders_resp:
                        if not isinstance(_tok_entry, dict):
                            continue
                        _holders_list = _tok_entry.get("holders") or []
                        if not isinstance(_holders_list, list):
                            continue
                        # round50 v2 fix: outcomeIndex 在内层 holder 上, 不是外层 token_entry
                        for _h in _holders_list:
                            if not isinstance(_h, dict):
                                continue
                            _amt = float(_h.get("amount", 0) or 0)
                            _h_oi = _h.get("outcomeIndex")
                            if _h_oi == 0:  # UP 侧
                                _top_holders_volume_up += _amt
                                _top_holders_up.append(_h)
                            elif _h_oi == 1:  # DOWN 侧
                                _top_holders_volume_down += _amt
                                _top_holders_down.append(_h)
                # 按 amount 降序, 每侧限 20
                _top_holders_up.sort(key=lambda x: float(x.get("amount", 0) or 0), reverse=True)
                _top_holders_down.sort(key=lambda x: float(x.get("amount", 0) or 0), reverse=True)
                _max_holders = 20
                _top_holders_up = _top_holders_up[:_max_holders]
                _top_holders_down = _top_holders_down[:_max_holders]

                # ===== v3 ② 钱包明细落盘(账本原料): UP20+DOWN20 全量追加 jsonl =====
                # 重复采集同 slug 会重复落盘, 结算脚本按 (slug, wallet) 去重取最新, 此处不去重保简单
                _all_holders_sm = [(_h, 0) for _h in _top_holders_up] + [(_h, 1) for _h in _top_holders_down]
                try:
                    _wj_path = DATA_DIR / "hermes_smart_money_wallets.jsonl"
                    with _wj_path.open("a", encoding="utf-8") as _f:
                        for _h, _side_oi in _all_holders_sm:
                            _f.write(json.dumps({
                                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                "et_hour": current_et.strftime("%Y-%m-%dT%H:00"),
                                "slug": slug,
                                "cid": _cid_sm,
                                "wallet": _h.get("proxyWallet") or _h.get("wallet"),
                                "side": "UP" if _side_oi == 0 else "DOWN",
                                "outcome_index": _side_oi,
                                "amount": float(_h.get("amount", 0) or 0),
                                "name": _h.get("name") or "",
                                "up_price": data["polymarket"].get("up_price"),
                                "down_price": data["polymarket"].get("down_price"),
                            }, ensure_ascii=False) + "\n")
                except Exception as _we:
                    log(f"  聪明钱钱包明细落盘失败(影子不影响主流程): {_we}", "W")

                # ===== 第二步: UP/DOWN 两侧持仓大户各查近 1h trades(v3 起 DOWN 侧也查) =====
                def _agg_wallet_trades(holder_list, target_oi):
                    _buy = 0.0
                    _sell = 0.0
                    _hits = 0
                    _misses = 0
                    for _h in holder_list:
                        _wallet = _h.get("wallet") or _h.get("proxyWallet")
                        if not _wallet or not isinstance(_wallet, str):
                            _misses += 1
                            continue
                        _ut_url = (
                            f"https://data-api.polymarket.com/trades"
                            f"?user={_wallet}&market={_cid_sm}&takerOnly=true&limit=20"
                            f"&start={_start_ts_sm}&end={_now_ts}"
                        )
                        _ut = fetch(_ut_url, use_proxy=True, timeout=10)
                        if not _ut or not isinstance(_ut, list):
                            _misses += 1
                            continue
                        _hits += 1
                        for _t in _ut:
                            if not isinstance(_t, dict):
                                continue
                            if _t.get("outcomeIndex") != target_oi:  # 只看目标侧方向
                                continue
                            _sz = float(_t.get("size", 0) or 0)
                            if _t.get("side") == "BUY":
                                _buy += _sz
                            elif _t.get("side") == "SELL":
                                _sell += _sz
                        time.sleep(0.1)  # 防 40 钱包触发 rate limit
                    return _buy, _sell, _hits, _misses

                _smart_buy_up, _smart_sell_up, _smart_hits, _smart_misses = _agg_wallet_trades(_top_holders_up, 0)
                _smart_buy_down, _smart_sell_down, _down_hits, _down_misses = _agg_wallet_trades(_top_holders_down, 1)

                # ===== v3 ③ 池内钱包标记(读 smart_money_pool.json, 不存在 fail-open) =====
                # smart_money_pool_direction ∈ [-1,+1]: 正=池内钱包持仓偏 UP, 池子未成型时为 None
                _pool_hits = 0
                _pool_up_vol = 0.0
                _pool_down_vol = 0.0
                _pool_direction = None
                try:
                    _pool_path = DATA_DIR / "hermes_smart_money_pool.json"
                    if _pool_path.exists():
                        _pool = json.loads(_pool_path.read_text(encoding="utf-8"))
                        _pool_set = {p.get("wallet") for p in (_pool.get("pool") or []) if isinstance(p, dict) and p.get("wallet")}
                        if _pool_set:
                            for _h, _side_oi in _all_holders_sm:
                                _w = _h.get("proxyWallet") or _h.get("wallet")
                                if _w in _pool_set:
                                    _pool_hits += 1
                                    if _side_oi == 0:
                                        _pool_up_vol += float(_h.get("amount", 0) or 0)
                                    else:
                                        _pool_down_vol += float(_h.get("amount", 0) or 0)
                            _pool_total_vol = _pool_up_vol + _pool_down_vol
                            _pool_direction = round((_pool_up_vol - _pool_down_vol) / _pool_total_vol, 4) if _pool_total_vol > 0 else None
                except Exception as _pe:
                    log(f"  聪明钱池子标记失败(影子不影响主流程): {_pe}", "W")

                _smart_total = _smart_buy_up + _smart_sell_up
                _smart_total_down = _smart_buy_down + _smart_sell_down
                data["polymarket"]["smart_money_consensus_1h"] = {
                    "buy_up_size": round(_smart_buy_up, 2),
                    "sell_up_size": round(_smart_sell_up, 2),
                    "net_up_imbalance": round((_smart_buy_up - _smart_sell_up) / _smart_total, 4) if _smart_total > 0 else None,
                    "buy_down_size": round(_smart_buy_down, 2),      # v3: DOWN 侧大户近1h成交
                    "sell_down_size": round(_smart_sell_down, 2),    # v3
                    "net_down_imbalance": round((_smart_buy_down - _smart_sell_down) / _smart_total_down, 4) if _smart_total_down > 0 else None,  # v3
                    "wallet_hits": _smart_hits,
                    "wallet_misses": _smart_misses,
                    "wallets_total": len(_top_holders_up),
                    "down_wallet_hits": _down_hits,                  # v3
                    "down_wallets_total": len(_top_holders_down),    # v3
                    "top_holders_volume_up": round(_top_holders_volume_up, 2),
                    "top_holders_volume_down": round(_top_holders_volume_down, 2),
                    "top_holders_count_down": len(_top_holders_down),
                    "pool_wallet_hits": _pool_hits,                  # v3: 池内钱包出现数
                    "pool_up_volume": round(_pool_up_vol, 2),        # v3: 池内钱包 UP 持仓量
                    "pool_down_volume": round(_pool_down_vol, 2),    # v3: 池内钱包 DOWN 持仓量
                    "smart_money_pool_direction": _pool_direction,   # v3: 池子方向, 未成型=None
                    "data_source": "data-api.polymarket.com/holders",
                }
                log(f"  PM 聪明钱共识(holders): UP买={_smart_buy_up:.0f}/卖={_smart_sell_up:.0f} 命中={_smart_hits}/{len(_top_holders_up)} | DOWN买={_smart_buy_down:.0f}/卖={_smart_sell_down:.0f} 命中={_down_hits}/{len(_top_holders_down)} | 池内={_pool_hits} 方向={_pool_direction}")
        except Exception as _e:
            log(f"  PM 聪明钱共识: 拉取失败(影子采集不影响主流程): {_e}", "W")
        # round41.8.16(8/25 用户决策): PM 动量 delta 同源优先真实订单簿。
        # 旧口径 mark-mark(up_price 差)会被标记价失真污染--8/24 3pm 场标记价 0.705(假强共识)
        # vs 盘口 0.44, 4pm 场 mark delta=0.44-0.705=-0.265 假暴跌触发 pm_momentum -1;
        # 真实盘口径 book-book=0.415-0.44=-0.025。修复: 当前与上一快照 book_up_mid 均有效 ->
        # book-book; 任一侧缺失整体退回 mark-mark(同源); 绝不混合口径(book_cur - mark_prev
        # 会把标记价失真直接注入 delta)。执行顺序在订单簿采集之后(当前 book_up_mid 此刻才可用)。
        # up_price_delta_source(book|mark)供消费端掉头检测同源 + 复盘归因。
        # 用 current_et(时钟校准后)而非 now_et, 保证 :05 补跑时上一 slug 定位仍正确。
        try:
            from datetime import timedelta as _td
            _prev_slug = build_slug(current_et - _td(hours=1))
            _prev_snaps = sorted((DATA_DIR / "snapshots").glob(f"{_prev_slug}_*.json"))
            if _prev_snaps:
                _prev_pm = (json.loads(_prev_snaps[-1].read_text(encoding="utf-8")).get("polymarket") or {})
                _cur_book = data["polymarket"].get("book_up_mid")
                _prev_book = _prev_pm.get("book_up_mid")
                _prev_mark = _prev_pm.get("up_price")
                _book_ok = (isinstance(_cur_book, (int, float)) and 0.0 < _cur_book < 1.0
                            and isinstance(_prev_book, (int, float)) and 0.0 < _prev_book < 1.0)
                if _book_ok:
                    data["polymarket"]["up_price_delta"] = round(_cur_book - _prev_book, 4)
                    data["polymarket"]["up_price_delta_source"] = "book"
                    # 9/3 reversal_intent 因子（round53）：prev_delta = 上小时末快照已算好的 delta
                    # （上小时末 vs 上上小时末），供 validator 深跌反转判据（单边+1）。
                    _prev_delta = _prev_pm.get("up_price_delta")
                    if isinstance(_prev_delta, (int, float)):
                        data["polymarket"]["up_price_delta_prev"] = round(_prev_delta, 4)
                    log(f"  PM 动量: {data['polymarket']['up_price_delta']:+.4f} (book同源: cur={_cur_book:.3f} prev={_prev_book:.3f}; delta_prev={data['polymarket'].get('up_price_delta_prev')})")
                elif isinstance(_prev_mark, (int, float)):
                    data["polymarket"]["up_price_delta"] = round(up_price - _prev_mark, 4)
                    data["polymarket"]["up_price_delta_source"] = "mark"
                    _prev_delta = _prev_pm.get("up_price_delta")
                    if isinstance(_prev_delta, (int, float)):
                        data["polymarket"]["up_price_delta_prev"] = round(_prev_delta, 4)
                    log(f"  PM 动量: {data['polymarket']['up_price_delta']:+.4f} (mark同源回退: 上小时 up={_prev_mark:.3f}; delta_prev={data['polymarket'].get('up_price_delta_prev')})")
        except Exception as _e:
            log(f"  PM 动量: 计算失败(不影响采集): {_e}", "W")
        log(f"  Polymarket: UP={up_price:.3f} DOWN={data['polymarket']['down_price']:.3f}")
    else:
        data["polymarket"] = None

    # ===== 8. 关键价位（5 档）=====
    k1h_for_levels = data.get("klines_1h") or []
    if k1h_for_levels:
        all_highs = [k["h"] for k in k1h_for_levels[-24:]]  # 24h
        all_lows = [k["l"] for k in k1h_for_levels[-24:]]

        strong_resist = max(all_highs)
        strong_support = min(all_lows)
        pivot = anchor_price

        # 弱支撑/弱压力：最近 6H 的低点/高点
        recent_highs = [k["h"] for k in k1h_for_levels[-6:]]
        recent_lows = [k["l"] for k in k1h_for_levels[-6:]]
        weak_support = min(recent_lows)
        weak_resistance = max(recent_highs)

        data["key_levels"] = {
            "strong_support": round(strong_support, 1),
            "weak_support": round(weak_support, 1),
            "pivot": round(pivot, 1),
            "weak_resistance": round(weak_resistance, 1),
            "strong_resistance": round(strong_resist, 1),
        }
        log(f"  关键价位: 强支撑={strong_support:.0f} 弱支撑={weak_support:.0f} 锚定={pivot:.0f} 弱压={weak_resistance:.0f} 强压={strong_resist:.0f}")

    # ===== 9. V 反检测（15m）=====
    if k15m_raw:
        k15m_parsed = parse_klines(k15m_raw)
        lows = [k["l"] for k in k15m_parsed[-8:]]
        closes_15m = [k["c"] for k in k15m_parsed[-8:]]
        if len(lows) >= 5:
            min_idx = lows.index(min(lows))
            if min_idx < len(lows) - 2:
                after = closes_15m[min_idx+1:]
                if len(after) >= 2 and all(after[i] >= after[i-1] for i in range(1, len(after))):
                    data["v_reversal_15m"] = {"detected": True, "low": round(min(lows), 1)}
                    log(f"  🔍 15m V反检测: 低点={min(lows):.0f}")
                else:
                    data["v_reversal_15m"] = {"detected": False}
            else:
                data["v_reversal_15m"] = {"detected": False}
        else:
            data["v_reversal_15m"] = {"detected": False}

    # ===== 10. 开盘微观数据（前 5 分钟，1H 预测信息密度最高）=====
    log("  分析开盘微观数据...")
    if k1m_raw and len(k1m_raw) >= 5:
        k1m_parsed = parse_klines(k1m_raw)
        # 按时间戳精确筛选当前小时前 5 分钟的蜡烛（避免 :05 执行时包含上一小时）
        current_et = now_et()
        target_hour_start = current_et.replace(minute=0, second=0, microsecond=0)
        target_ts_ms = int(target_hour_start.timestamp() * 1000)
        opening_candles_all = [c for c in k1m_parsed if c["t"] >= target_ts_ms]
        opening_candles = opening_candles_all[:5] if opening_candles_all else k1m_parsed[-5:]
        if len(opening_candles) < 3:
            opening_candles = k1m_parsed[-5:]  # 回退

        if not opening_candles:
            log("  ⚠️ opening_candles 为空,跳过开盘微观分析", "W")
            data["opening"] = {"dir": "UNKNOWN", "vol_ratio": 1.0, "has_fakeout": False, "has_v_reversal": False}
        else:
            candle_dirs = ["阳" if c["c"] >= c["o"] else "阴" for c in opening_candles]
            opening_dir = "UP" if candle_dirs.count("阳") >= 3 else "DOWN"
            opening_body_sizes = [abs(c["c"] - c["o"]) / max(c["o"], 0.01) for c in opening_candles]
            opening_volumes = [c["v"] for c in opening_candles]
            # 开盘量能 vs 前 5 根 1m 均量（开盘前 5 分钟）
            prev_5_vol = [c["v"] for c in k1m_parsed[-10:-5]] if len(k1m_parsed) >= 10 else opening_volumes[:5]
            avg_prev_vol = sum(prev_5_vol) / len(prev_5_vol) if prev_5_vol else opening_volumes[0]
            opening_vol_ratio = sum(opening_volumes[:2]) / (avg_prev_vol * 2) if avg_prev_vol > 0 else 1.0

            opening_high = max(c["h"] for c in opening_candles)
            opening_low = min(c["l"] for c in opening_candles)
            opening_range_pct = (opening_high - opening_low) / max(opening_candles[0]["o"], 0.01) * 100

            # 假突破检测：滑动窗口检测"两同向→反向"的任何模式
            has_fakeout = False
            if len(candle_dirs) >= 3:
                for i in range(len(candle_dirs) - 2):
                    a, b, c = candle_dirs[i], candle_dirs[i+1], candle_dirs[i+2]
                    if a == b and b != c:  # 任何"两同向→反向"
                        if a == "阳":  # 先涨后跌 = 诱多
                            rev = (opening_candles[i+2]["c"] - opening_candles[i+1]["h"]) / max(opening_candles[i+1]["h"], 0.01)
                        else:  # 先跌后涨 = 洗盘
                            rev = (opening_candles[i+2]["c"] - opening_candles[i+1]["l"]) / max(opening_candles[i+1]["l"], 0.01)
                        if abs(rev) > 0.0015:
                            has_fakeout = True
                            break

            # V反检测：1m 级别（开盘第 2-4 分钟出现方向反转）
            has_v_reversal_1m = False
            if len(candle_dirs) >= 4:
                for i in range(len(candle_dirs) - 3):
                    if candle_dirs[i] == candle_dirs[i+1] and all(d == ("阳" if candle_dirs[i] == "阴" else "阴") for d in candle_dirs[i+2:i+4]):
                        has_v_reversal_1m = True
                        break

            data["opening"] = {
                "dir": opening_dir,
                "candle_dirs": candle_dirs,
                "body_sizes": [round(s, 4) for s in opening_body_sizes],
                "high": round(opening_high, 1),
                "low": round(opening_low, 1),
                "range_pct": round(opening_range_pct, 3),
                "vol_ratio": round(opening_vol_ratio, 2),
                "has_fakeout": has_fakeout,
                "has_v_reversal": has_v_reversal_1m,
            }
            log(f"  开盘: {opening_dir} | 区间{opening_range_pct:.2f}% | 量比{opening_vol_ratio:.1f}x | 假突破={has_fakeout} | V反={has_v_reversal_1m}")

    # ===== 11. 前一根 1H K 线结果 =====
    if k1h_raw and len(k1h_raw) >= 2:
        k1h_parsed = parse_klines(k1h_raw)
        if len(k1h_parsed) < 2:
            log("  ⚠️ prev_hour: k1h_parsed 不足2根,跳过", "W")
        else:
            prev = k1h_parsed[-2]
            prev_side = "UP" if prev["c"] >= prev["o"] else "DOWN"
            prev_change_pct = (prev["c"] - prev["o"]) / max(prev["o"], 0.01) * 100

            # 连续涨跌计数（先判断再累加，避免 cons_up/cons_down 同时 >0）
            # 注意：从 len-2 起算，排除当前未收盘 1H K 线（与 direction_history 的 [:-1] 口径一致，
            # 遵守 prompt.md 步骤4.5 "禁止使用本小时结果" 原则）
            cons_up = 0; cons_down = 0
            for i in range(len(k1h_parsed) - 2, -1, -1):
                c = k1h_parsed[i]
                is_up = c["c"] >= c["o"]
                if is_up:
                    if cons_down > 0:
                        break
                    cons_up += 1
                else:
                    if cons_up > 0:
                        break
                    cons_down += 1

            # 近3根已完成1H方向序列（t-3..t-1 时间升序，排除当前未收盘K线）+ 变号次数
            # 供 prompt.md 步骤4.5 regime 识别使用（变号≥2 = 摆动市主指标）
            done_candles = k1h_parsed[:-1]  # 排除当前未收盘K线
            direction_history = [
                "UP" if c["c"] >= c["o"] else "DOWN" for c in done_candles[-3:]
            ]
            flips = sum(
                1 for a, b in zip(direction_history, direction_history[1:]) if a != b
            )
            # round41.2 审计修复（H13 引擎化）：最近 3 根已收 1H 涨跌幅（带符号 %）
            # H13 第三条件"涨跌幅均 <±0.7%"判据；与 direction_history 同源同口径
            last3_chg_pct = [
                round((c["c"] - c["o"]) / max(c["o"], 0.01) * 100, 3)
                for c in done_candles[-3:]
            ]

            data["prev_hour"] = {
                "side": prev_side,
                "open": round(prev["o"], 1),
                "close": round(prev["c"], 1),
                "high": round(prev["h"], 1),
                "low": round(prev["l"], 1),
                "change_pct": round(prev_change_pct, 2),
                "volume": round(prev["v"], 2),
                "consecutive_up": cons_up,
                "consecutive_down": cons_down,
                "direction_history": direction_history,
                "flips": flips,
                "last3_chg_pct": last3_chg_pct,
            }
            log(f"  上一小时: {prev_side} {prev_change_pct:+.2f}% | 连续涨{cons_up}/跌{cons_down} | 方向序列{direction_history} 变号{flips}")

            # ===== 11.1 宏观趋势族字段（round44 挪进采集主体，用户 8/26 指令）=====
            # 此前 macro_trend_1h_4h 只在 _apply_slim 里计算——而快照归档故意存全量（B2-fix），
            # 导致全量快照永不含该字段：validator 从快照读取时恒走 consecutive_up 降级口径
            # （6 根含进行中 K 线、判分语义不同），与 AI 层（slim 正确口径）两层 S0 不一致，
            # 且 factor_attribution 对该因子的归因统计口径失真（实测最近 12/12 快照缺失）。
            # 现改在采集主体统一计算（最近 4 根已收盘 1H），全量快照与 slim 输出同源同值。
            # _apply_slim 保留同逻辑仅作兜底（字段已在时不重算，见 _apply_slim 注释）。
            if len(done_candles) >= 4:
                _mt4 = done_candles[-4:]
                _mt_dirs = ["UP" if c["c"] >= c["o"] else "DOWN" for c in _mt4]
                data["macro_trend_1h_4h"] = {
                    "dirs": _mt_dirs,
                    "up_count": sum(1 for x in _mt_dirs if x == "UP"),
                    "down_count": sum(1 for x in _mt_dirs if x == "DOWN"),
                    "first_ts": _mt4[0]["t"],
                    "last_ts": _mt4[-1]["t"],
                }
                log(f"  宏观趋势族: {_mt_dirs} (up={data['macro_trend_1h_4h']['up_count']}/down={data['macro_trend_1h_4h']['down_count']}, 已收4根口径)")

    # ===== 12. OI-价格联动 =====
    if "open_interest" in capital_flow and oi_hist and len(oi_hist) >= 2:
        try:
            # OKX open-interest-volume 格式: [ts, oi, vol]
            oi_current = float(oi_hist[-1][1])
            oi_1h_ago = float(oi_hist[-2][1])
            oi_change_pct = (oi_current - oi_1h_ago) / max(oi_1h_ago, 0.01) * 100

            # 当前小时价格变化（从锚定价到当前价）
            price_change_pct = (current_price - anchor_price) / max(anchor_price, 0.01) * 100 if anchor_price else 0

            oi_up = oi_change_pct > 1.0
            price_up = price_change_pct > 0.1
            oi_down = oi_change_pct < -1.0
            price_down = price_change_pct < -0.1

            if oi_up and price_up:
                oi_signal = "bullish_add"       # 多头主动加仓 → 强看多
            elif oi_up and price_down:
                oi_signal = "bearish_add"       # 空头主动加仓 → 强看空
            elif oi_down and price_up:
                oi_signal = "bullish_cover"     # 空头被迫平仓 → 弱看多
            elif oi_down and price_down:
                oi_signal = "bearish_reduce"    # 多头主动减仓 → 弱看空
            else:
                oi_signal = "neutral"

            data["oi_price_relation"] = {
                "oi_1h_change_pct": round(oi_change_pct, 2),
                "price_1h_change_pct": round(price_change_pct, 2),
                "signal": oi_signal,
            }
            log(f"  OI联动: OI{oi_change_pct:+.1f}% Price{price_change_pct:+.1f}% → {oi_signal}")
        except Exception:
            pass

    # ===== 输出（原子写入：先临时文件再 rename，防止分析器读到半文件）=====
    # B6-fix 2026-08-23: 写前标注 schema_version + data_quality + price>0 sanity，
    #   使异常/缺字段不再"静默入库"，回测可按版本与质量追溯。
    _warn_issues: list[str] = []
    try:
        cp = float(data.get("current_price") or 0)
        if cp <= 0:
            _warn_issues.append("current_price<=0")
        if data.get("price_position_24h") is None:
            _warn_issues.append("price_position_24h 缺失")
        cf = data.get("capital_flow") or {}
        for _k in ("funding_rate", "taker_bs_ratio", "open_interest"):
            if _k not in cf:
                _warn_issues.append(f"capital_flow.{_k} 缺失")
    except (TypeError, ValueError):
        _warn_issues.append("数据 sanity 计算异常")
    data["schema_version"] = SCHEMA_VERSION
    data["data_quality"] = {
        "ok": not _warn_issues,
        "warnings": _warn_issues,
        "sanity": {"current_price": data.get("current_price"),
                   "price_pos_ok": data.get("price_position_24h") is not None},
    }
    if _warn_issues:
        log(f"  ⚠️ [data_quality] {len(_warn_issues)} 项异常: {'; '.join(_warn_issues)}")

    # B2-fix 2026-08-23: slim 只作用于给 AI 读的实时文件(OUTPUT)，snapshot 归档务必存全量，
    #   否则 49% 快照永久丢失原始 klines_1h / 盘口10档 / funding_history，回测资产不可逆损失。
    snapshot_data = data
    out_data = _apply_slim(data) if slim else data
    if slim:
        log(f"   [slim] 精简模式：{len(json.dumps(out_data, ensure_ascii=False))} 字符")
    tmp_out = OUTPUT.with_suffix(".tmp")
    _ = tmp_out.write_text(json.dumps(out_data, ensure_ascii=False, indent=2))
    os.replace(tmp_out, OUTPUT)

    # ===== 快照归档（8/11 新增）：每次采集存 data/snapshots/，供规则回测复盘 =====
    # 失败静默降级（warn 日志），不阻塞采集主流程
    try:
        snap_dir = DATA_DIR / "snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        snap_name = f"{slug}_{stamp}.json" if slug else f"market_{stamp}.json"
        _ = (snap_dir / snap_name).write_text(json.dumps(snapshot_data, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"   [snapshot] 已归档(全量) → data/snapshots/{snap_name}")
    except Exception as e:
        log(f"   [snapshot] ⚠️ 归档失败（不影响采集）: {e}")

    log(f"✅ 数据采集完成 → {OUTPUT}")
    log(f"   包含: {len(timeframes)} 周期 + 资金面 + 情绪 + 历史 + Polymarket + 开盘 + OI联动")
    return True

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Hermes BTC 1H 数据采集")
    parser.add_argument("--force", "-f", action="store_true", help="强制重拉,跳过去重(同小时也能重跑)")
    parser.add_argument("--slim", action="store_true", help="精简输出:砍掉冗余原始数据(42526→~5900字符),保留所有分析字段")
    args = parser.parse_args()
    _ = collect(force=args.force, slim=args.slim)
