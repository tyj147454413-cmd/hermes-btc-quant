#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round48 功能测试：PM 订单簿不平衡（聪明钱挂单失衡）pm_imbalance ×1 入评分表。

规则（用户 8/28 决策方案 A）：
  数据源 polymarket.book_imbalance（采集器 round41.8.11 起影子采集，本轮转正计分）；
  imb ≥ +0.3 → +1×1；imb ≤ -0.3 → -1×1；|imb|<0.3 → 0；缺失 → 0 + notes 标注（fail-open）。
  仅成型/博弈期计分（phase_scored 块内，同 pm_momentum 口径）；开盘/结算/贴线不计分。
  回测依据：data/hermes_factor_candidate_backtest_20260828_smart_money.md A1 档 81.2%（39/48）。

运行：python3 scripts/test_round48_pm_imbalance.py"""
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from signal_validator import (  # noqa: E402
    PM_IMBALANCE_THRESHOLD,
    W_PM_IMBALANCE,
    extract_factors,
)

PASS, FAIL = 0, 0


def check(name, got, want):
    global PASS, FAIL
    ok = got == want
    PASS += int(ok)
    FAIL += int(not ok)
    print(f"{'✅' if ok else '❌'} {name}: got={got!r} want={want!r}")


_SNAP = os.path.join(
    ROOT, "data", "snapshots",
    "bitcoin-up-or-down-august-27-2026-8pm-et_20260828_081745.json")  # 真实快照：imb=1.0，collect minute=17（博弈期）

_BASE = None


def _base():
    global _BASE
    if _BASE is None:
        _BASE = json.load(open(_SNAP))
    return copy.deepcopy(_BASE)


def run(md):
    fac, _inp, notes, mode = extract_factors(md)
    return fac, notes, mode


# ---- T1/T2 阈值触发（博弈期底座）----
md = _base()
md["polymarket"]["book_imbalance"] = 0.35
fac, notes, mode = run(md)
check("T1 成型/博弈期 imb=+0.35 → +1×1", fac.get("pm_imbalance"), 1.0 * W_PM_IMBALANCE)
check("T1 mode 为常规-博弈期", mode.startswith("常规-博弈"), True)

md = _base()
md["polymarket"]["book_imbalance"] = -0.35
fac, notes, mode = run(md)
check("T2 成型/博弈期 imb=-0.35 → -1×1", fac.get("pm_imbalance"), -1.0 * W_PM_IMBALANCE)
check("T2 note 标注触发", any("pm_imbalance" in n for n in notes), True)

# ---- T3/T4/T5 边界值（含等号）----
for name, imb, want in [
    ("T3 边界 imb=+0.29 → 0", 0.29, 0.0),
    ("T4 边界 imb=+0.30 → +1（含等号）", 0.30, 1.0 * W_PM_IMBALANCE),
    ("T5 边界 imb=-0.30 → -1（含等号）", -0.30, -1.0 * W_PM_IMBALANCE),
]:
    md = _base()
    md["polymarket"]["book_imbalance"] = imb
    fac, _n, _m = run(md)
    check(name, fac.get("pm_imbalance"), want)

check("阈值常量 PM_IMBALANCE_THRESHOLD=0.3", PM_IMBALANCE_THRESHOLD, 0.3)
check("权重常量 W_PM_IMBALANCE=1", W_PM_IMBALANCE, 1)

# ---- T6 缺失 fail-open（round41.8.11 前旧快照）----
md = _base()
md["polymarket"].pop("book_imbalance", None)
fac, notes, _m = run(md)
check("T6 book_imbalance 缺失 → 0", fac.get("pm_imbalance"), 0.0)
check("T6 缺失 note 标注", any("book_imbalance 缺失" in n for n in notes), True)

# ---- T7 贴线模式不计分 ----
md = _base()
md["polymarket"]["book_imbalance"] = 0.9  # 若误入 phase_scored 会 +1
md["current_price"] = md["anchor_price"]  # dev=0 → 贴线激活
md["volatility_5m"] = 0.1                 # ≤0.3 前置门通过
md["prev_hour"] = {}                      # 趋势市（非摆动市）
fac, _n, mode = run(md)
check("T7 贴线模式激活（mode=贴线激活）", mode, "贴线激活")
check("T7 贴线模式 pm_imbalance 不计分（键不写入）", "pm_imbalance" in fac, False)

# ---- T8 结算期/开盘期不计分 ----
md = _base()
md["polymarket"]["book_imbalance"] = 0.9
md["collect_time_utc"] = "2026-08-28T00:50:37.570331+00:00"  # minute=50 → 结算期
fac, _n, mode = run(md)
check("T8a 结算期（mode=常规-结算期）", mode, "常规-结算期")
check("T8a 结算期 pm_imbalance 不计分", "pm_imbalance" in fac, False)

md = _base()
md["polymarket"]["book_imbalance"] = 0.9
md["collect_time_utc"] = "2026-08-28T00:02:37.570331+00:00"  # minute=2 → 开盘期
fac, _n, mode = run(md)
check("T8b 开盘期（mode=常规-开盘期）", mode, "常规-开盘期")
check("T8b 开盘期 pm_imbalance 不计分", "pm_imbalance" in fac, False)

# ---- T9 与 pm_momentum 独立（各计各分互不覆盖）----
md = _base()
md["polymarket"]["book_imbalance"] = 0.4
md["polymarket"]["up_price_delta"] = 0.05
md["polymarket"]["up_price_delta_source"] = "book"
fac, _n, _m = run(md)
check("T9 pm_momentum 同场 +2（round49 回退 ×2）", fac.get("pm_momentum"), 2.0)
check("T9 pm_imbalance 独立 +1", fac.get("pm_imbalance"), 1.0 * W_PM_IMBALANCE)

# ---- T10 真实快照回归（8/27 8pm ET 快照 imb=1.0，博弈期）----
md = _base()
fac, _n, mode = run(md)
check("T10 真实快照 mode=常规-博弈期", mode, "常规-博弈期")
check("T10 真实快照 imb=+1.0 → +1", fac.get("pm_imbalance"), 1.0 * W_PM_IMBALANCE)

print(f"\n=== round48 pm_imbalance 测试：{PASS} 通过 / {FAIL} 失败 ===")
sys.exit(1 if FAIL else 0)
