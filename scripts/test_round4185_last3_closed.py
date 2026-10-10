#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
round41.8.5 行为级 mock 测试：tf_summary().last_3_candles 剔除「进行中」K 线
背景：8/24 8AM ET，进行中 1H 开盘16分钟暂时收阴被当「末阴」计 -1，
该小时最终收 +0.91% 大阳，误导净分-6→DOWN 成交亏损 -$10。
运行：python3 scripts/test_round4185_last3_closed.py
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)  # hermes_collect 依赖同目录 hermes_common
spec = importlib.util.spec_from_file_location(
    "hermes_collect", os.path.join(ROOT, "hermes_collect.py"))
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

PASS, FAIL = 0, 0


def check(name, got, want):
    global PASS, FAIL
    ok = got == want
    PASS += int(ok)
    FAIL += int(not ok)
    print(f"{'✅' if ok else '❌'} {name}: got={got!r} want={want!r}")


def kl(o, c):
    """构造最小 kline dict（tf_summary 只读 o/c/v）"""
    return {"o": float(o), "h": max(float(o), float(c)) + 1,
            "l": min(float(o), float(c)) - 1, "c": float(c), "v": 100.0, "tb": 50.0}


# ── 用例1：8/24 8AM 事故真实数据复现（快照最后6根，末根为进行中阴线）
snapshot_klines = [
    {"t": 1787554800000, "o": 77464.33, "h": 77790.19, "l": 77281.68, "c": 77307.15, "v": 681.33, "tb": 353.55},   # 阴 3-4AM
    {"t": 1787558400000, "o": 77307.14, "h": 77414.88, "l": 76838.00, "c": 77041.47, "v": 921.46, "tb": 414.98},   # 阴 4-5AM
    {"t": 1787562000000, "o": 77041.46, "h": 77602.80, "l": 77028.00, "c": 77554.08, "v": 523.01, "tb": 322.92},   # 阳 5-6AM
    {"t": 1787565600000, "o": 77554.08, "h": 78017.00, "l": 77534.09, "c": 77617.22, "v": 1113.08, "tb": 606.10},  # 阳 6-7AM
    {"t": 1787569200000, "o": 77617.23, "h": 78600.00, "l": 77450.00, "c": 78443.65, "v": 1324.46, "tb": 783.69},  # 阳 7-8AM
    {"t": 1787572800000, "o": 78443.65, "h": 78451.69, "l": 77992.66, "c": 77992.66, "v": 310.57, "tb": 168.84},   # 进行中(当时暂阴) 8-9AM
]
s1 = hc.tf_summary(snapshot_klines, "1H")
check("用例1a 8AM事故复现: 已收盘口径应为 阳阳阳", s1["last_3_candles"], "阳阳阳")

# 旧逻辑对照（证明旧代码确实输出 阳阳阴，即 bug 存在过）
old = "".join("阳" if k["c"] >= k["o"] else "阴" for k in snapshot_klines[-3:])
check("用例1b 旧口径对照(应输出含假末阴的 阳阳阴)", old, "阳阳阴")

# ── 用例2：进行中「绿」K线同样不得虚增序列（对称性）
s2_data = [kl(100, 99), kl(99, 98), kl(98, 101), kl(101, 105)]  # 阴阴阳 + 进行中大阳
check("用例2 末根进行中大阳不虚增→仍取已收盘 阴阴阳",
      hc.tf_summary(s2_data, "1H")["last_3_candles"], "阴阴阳")

# ── 用例3：恰好4根（剔除后剩3根，正常路径最小长度）
s3_data = [kl(10, 9), kl(9, 11), kl(11, 12), kl(12, 8)]  # 阴阳阳 + 进行中阴
check("用例3 边界恰好4根→阴阳阳", hc.tf_summary(s3_data, "1H")["last_3_candles"], "阴阳阳")

# ── 用例4：仅3根（<4 退回全列表，不抛错、行为与旧版一致）
s4_data = [kl(10, 9), kl(9, 8), kl(8, 11)]
check("用例4 边界仅3根退回全列表→阴阴阳", hc.tf_summary(s4_data, "1H")["last_3_candles"], "阴阴阳")

# ── 用例5：平盘 c==o 沿用 >= 计阳约定
s5_data = [kl(10, 11), kl(11, 12), kl(12, 12), kl(12, 10)]
check("用例5 平盘c==o计阳(末根已收盘平盘)→阳阳阳",
      hc.tf_summary(s5_data, "1H")["last_3_candles"], "阳阳阳")

# ── 用例6：15m 与 1H 走同一函数，口径自动统一
d6 = [kl(5, 6), kl(6, 7), kl(7, 6), kl(6, 5.5)]  # 阳阳阴+进行中阴
check("用例6a 15m 同口径", hc.tf_summary(d6, "15m")["last_3_candles"], "阳阳阴")
check("用例6b 1H 同口径", hc.tf_summary(d6, "1H")["last_3_candles"], "阳阳阴")

# ── 用例7：字段完整性回归——其余字段照常产出（下游 validator 兼容）
need = ["name", "rsi", "bb_pos", "macd_hist", "last_3_candles", "recent_dir",
        "last_close", "last_open", "consecutive_up", "consecutive_down",
        "taker_trend", "taker_pct_last"]
missing = [k for k in need if k not in s1]
check("用例7 返回字段完整无缺失", missing, [])

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
