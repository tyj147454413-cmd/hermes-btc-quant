#!/usr/bin/env python3
"""round44 评分层 mock 测试：K线序列拆分双行 ×1 + 贴线墙背离 ×1 + macro_trend 入采集主体。

验证项：
- T1: K线拆分——15m/1H 各自独立计票（kline_15m/kline_1h 两键），旧键 "kline" 不再产出
- T2: 单窗口平票（2阳2阴）→ 该窗计 0
- T3: 旧快照缺 last_4_candles → 退 last_3_candles 兼容口径仍按窗独立
- T4: 双窗字段全缺 → 两键均 0 且不崩
- T5: 贴线墙背离 买墙>3+价跌 → +1（旧口径 ±4）；卖墙侧对称 -1；墙价同向 → 0
- T6: macro_trend 主口径——快照含 macro_trend_1h_4h(up_count=3) → factors["macro_trend"]=+1；3跌→-1；2涨2跌→0
- T7: macro_trend 字段缺失 → 降级 consecutive_up 口径 + notes 标注
- T8: hermes_collect 采集主体含步骤11.1（data["macro_trend_1h_4h"] 在归档前计算），_apply_slim 仅兜底
- T9: SCHEMA_VERSION == 6
- T10: 权重常量——W_KLINE 移除、W_KLINE_15M=W_KLINE_1H=1、W_WALL_DIV=1、墙背离表达式 ±1.0×W
- T11: judge 对新因子键透明求和（引擎与键名解耦）
- T12: 端到端——两窗矛盾场（15m阳多/1H阴多）S₀ 净影响 0 且分歧留痕（两键可查）
"""
import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path("/Users/tangyijun/Desktop/hermes btc")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from signal_validator import extract_factors, W_KLINE_15M, W_KLINE_1H, W_WALL_DIV  # noqa: E402
from rule_engine import judge  # noqa: E402

sv_src = (ROOT / "scripts" / "signal_validator.py").read_text(encoding="utf-8")
hc_src = (ROOT / "hermes_collect.py").read_text(encoding="utf-8")

passed = failed = 0


def ok(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"✅ {label}" + (f" | {detail}" if detail else ""))
    else:
        failed += 1
        print(f"❌ {label}" + (f" | {detail}" if detail else ""))


BASE_TF = {
    "1m": {}, "5m": {},
    "15m": {"macd_hist": 1.0},
    "1H": {"macd_hist": 40.0},
    "4H": {"macd_hist": 35.0},
}


def make_md(*, vol=0.50, dev_pct=0.30, tf15=None, tf1h=None, extra_tf=None, pm_up=None):
    """构造最小可跑 extract_factors 的快照。默认常规模式（vol>0.3 禁贴线、dev≥0.15 非灰区）。"""
    anchor = 50000.0
    current = anchor * (1 + dev_pct / 100.0)
    now = datetime(2026, 8, 26, 14, 20, tzinfo=timezone.utc)
    md = {
        "slug": "test-round44",
        "anchor_price": anchor,
        "current_price": round(current, 2),
        "collect_time_utc": now.isoformat(),
        "timestamp": (now - timedelta(minutes=20)).isoformat(),
        "volatility_5m": vol,
        "timeframes": copy.deepcopy(BASE_TF),  # 深拷贝防跨用例污染（内层 dict 共享会导致前例字段泄漏）
        "capital_flow": {},
        "order_flow": {},
        "polymarket": {},
        "prev_hour": {"direction_history": ["UP", "UP", "UP"], "flips": 0,
                      "consecutive_up": 3, "last3_chg_pct": [0.5, 0.4, 0.3]},
        "historical": {},
        "price_position_24h": {},
    }
    if tf15 is not None:
        md["timeframes"]["15m"].update(tf15)
    if tf1h is not None:
        md["timeframes"]["1H"].update(tf1h)
    if extra_tf:
        for k, v in extra_tf.items():
            md["timeframes"][k].update(v)
    if pm_up is not None:
        md["polymarket"]["up_price"] = pm_up
        md["polymarket"]["book_up_mid"] = pm_up
    return md


# ---- T1: K线拆分双行 ----
md = make_md(tf15={"last_4_candles": "阳阳阳阴"}, tf1h={"last_4_candles": "阴阴阳阴"})
factors, inp, notes, mode = extract_factors(md)
ok("T1a 15m窗 阳多→+1", factors.get("kline_15m") == 1.0 * W_KLINE_15M, f"kline_15m={factors.get('kline_15m')}")
ok("T1b 1H窗 阴多→-1", factors.get("kline_1h") == -1.0 * W_KLINE_1H, f"kline_1h={factors.get('kline_1h')}")
ok("T1c 旧键 kline 不再产出", "kline" not in factors)

# ---- T2: 平票窗口计 0 ----
md = make_md(tf15={"last_4_candles": "阳阳阴阴"}, tf1h={"last_4_candles": "阳阴阴阴"})
factors, _, _, _ = extract_factors(md)
ok("T2a 15m 2阳2阴→0", factors.get("kline_15m") == 0.0)
ok("T2b 1H 1阳3阴→-1", factors.get("kline_1h") == -1.0)

# ---- T3: 旧快照退 last_3 兼容 ----
md = make_md(tf15={"last_3_candles": "阳阴阳"}, tf1h={"last_3_candles": "阳阴阴"})
factors, _, _, _ = extract_factors(md)
ok("T3 last_3 兼容按窗独立(+1/-1)", factors.get("kline_15m") == 1.0 and factors.get("kline_1h") == -1.0)

# ---- T4: 全缺不崩 ----
md = make_md()
factors, _, _, _ = extract_factors(md)
ok("T4 双窗全缺→均0", factors.get("kline_15m") == 0.0 and factors.get("kline_1h") == 0.0)

# ---- T5: 贴线墙背离 ±1 ----
md = make_md(vol=0.20, dev_pct=-0.02,
             extra_tf={"1m": {"recent_dir": "DOWN", "taker_pct_last": 0.5}},
             pm_up=0.45)
md["capital_flow"] = {"order_book": {"bid_volume_top10": 400.0, "ask_volume_top10": 100.0}}  # buy_wall=4>3
factors, inp, notes, mode = extract_factors(md)
ok(f"T5a 模式=贴线(mode={mode})", "贴线" in mode)
ok("T5b 买墙背离→+1(round44)", factors.get("wall_divergence") == 1.0 * W_WALL_DIV,
   f"wall_divergence={factors.get('wall_divergence')}")
md["capital_flow"] = {"order_book": {"bid_volume_top10": 100.0, "ask_volume_top10": 400.0}}
md["timeframes"]["1m"]["recent_dir"] = "UP"
factors, _, _, _ = extract_factors(md)
ok("T5c 卖墙背离→-1", factors.get("wall_divergence") == -1.0 * W_WALL_DIV)
md["capital_flow"] = {"order_book": {"bid_volume_top10": 400.0, "ask_volume_top10": 100.0}}
factors, _, _, _ = extract_factors(md)  # 价跌+买墙已设，recent_dir=UP → 同向不计
ok("T5d 墙价同向→0", factors.get("wall_divergence") == 0.0)

# ---- T6: macro_trend 主口径 ----
for upc, dnc, expect in ((3, 1, 1.0), (1, 3, -1.0), (2, 2, 0.0)):
    md = make_md()
    md["macro_trend_1h_4h"] = {"dirs": ["UP"] * upc + ["DOWN"] * dnc, "up_count": upc, "down_count": dnc}
    factors, _, notes_, _ = extract_factors(md)
    ok(f"T6 macro({upc}涨{dnc}跌)→{expect:+.0f}", factors.get("macro_trend") == expect,
       f"got={factors.get('macro_trend')}")

# ---- T7: 字段缺失走降级口径 + 标注 ----
md = make_md(tf1h={"consecutive_up": 5})
factors, _, notes_, _ = extract_factors(md)
ok("T7a 缺字段降级 consecutive_up≥4→+1", factors.get("macro_trend") == 1.0)
ok("T7b notes 含降级标注", any("降级 consecutive_up" in n for n in notes_))

# ---- T8: 采集主体步骤11.1 + slim 兜底 ----
ok("T8a collect 主体计算 macro_trend_1h_4h", 'data["macro_trend_1h_4h"]' in hc_src and "步骤11.1" in hc_src)
ok("T8b _apply_slim 改兜底守卫", '"macro_trend_1h_4h" not in slim' in hc_src)
# 归档在写盘前使用全量 data（snapshot_data = data），确认顺序注释仍在
ok("T8c 快照归档存全量语义未动", "snapshot_data = data" in hc_src)

# ---- T9: SCHEMA_VERSION ----
m = [ln for ln in hc_src.splitlines() if ln.startswith("SCHEMA_VERSION")]
ok("T9 SCHEMA_VERSION>=6（round44.1 起 7）", m and m[0].split("=")[1].strip() in ("6", "7"), str(m))

# ---- T10: 权重常量 ----
ok("T10a W_KLINE 旧常量移除", "W_KLINE = " not in sv_src)
ok("T10b W_KLINE_15M=W_KLINE_1H=1", W_KLINE_15M == 1 and W_KLINE_1H == 1)
ok("T10c W_WALL_DIV=1", W_WALL_DIV == 1)
ok("T10d 墙背离表达式 ±1.0×W", "wall_div = 1.0 * W_WALL_DIV" in sv_src and "wall_div = -1.0 * W_WALL_DIV" in sv_src
   and "2.0 * W_WALL_DIV" not in sv_src)

# ---- T11: judge 对新键透明求和 ----
d = judge({"factors": {"kline_15m": 1.0, "kline_1h": -1.0}, "pm_up": 55.0, "deviation_pct": 0.02,
           "tie_line_active": False, "macd_1h_hist": 5.0, "macd_4h_hist": 5.0, "regime": "趋势市"})
ok("T11a judge 求和新键 S0=0", abs(d.s0) < 1e-9, f"s0={d.s0}")
d = judge({"factors": {"kline_15m": 1.0}, "pm_up": 55.0, "deviation_pct": 0.02,
           "tie_line_active": False, "macd_1h_hist": 5.0, "macd_4h_hist": 5.0, "regime": "趋势市"})
ok("T11b 单窗 +1 进 S0", abs(d.s0 - 1.0) < 1e-9, f"s0={d.s0}")

# ---- T12: 端到端两窗分歧留痕 ----
md = make_md(tf15={"last_4_candles": "阳阳阳阴"}, tf1h={"last_4_candles": "阴阴阴阳"})
factors, _, _, _ = extract_factors(md)
s0_kline_part = factors.get("kline_15m", 0) + factors.get("kline_1h", 0)
ok("T12 两窗矛盾净0但分歧留痕", s0_kline_part == 0 and factors.get("kline_15m") == 1.0 and factors.get("kline_1h") == -1.0,
   f"kline_15m={factors.get('kline_15m')} kline_1h={factors.get('kline_1h')}")

print(f"\nround44 mock：{passed}/{passed + failed} 通过")
sys.exit(1 if failed else 0)
