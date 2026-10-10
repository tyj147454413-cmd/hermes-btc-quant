#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round41.8.10 功能测试：pm_momentum（PM 动量因子）+ 掉头检测。
运行：python3 scripts/test_round41810_pm_momentum.py"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

PASS, FAIL = 0, 0
def check(name, got, want):
    global PASS, FAIL
    ok = got == want
    PASS += int(ok); FAIL += int(not ok)
    print(f"{'✅' if ok else '❌'} {name}: got={got!r} want={want!r}")

from signal_validator import extract_factors

REAL = os.path.join(ROOT, "data", "snapshots",
                    "bitcoin-up-or-down-august-24-2026-9am-et_20260824_211602.json")

def mk(delta=None, up_price=None):
    d = json.load(open(REAL))
    d.setdefault("polymarket", {})
    if delta is not None:
        d["polymarket"]["up_price_delta"] = delta
    if up_price is not None:
        d["polymarket"]["up_price"] = up_price
    return d

# T1 动量向上 ≥阈值 → +2（round44.1 权重 ×1→×2）
f, _, notes, _ = extract_factors(mk(delta=0.05))
check("T1 动量向上0.05 → +2", f.get("pm_momentum"), 2.0)

# T2 动量向下 ≥阈值 → -2
f, _, notes, _ = extract_factors(mk(delta=-0.05))
check("T2 动量向下-0.05 → -2", f.get("pm_momentum"), -2.0)

# T3 动量微小(<0.02) → 0
f, _, notes, _ = extract_factors(mk(delta=0.01))
check("T3 动量微小0.01 → 0", f.get("pm_momentum"), 0.0)

# T4 缺失 → 0(不抛错)
f, _, notes, _ = extract_factors(mk(delta=None))
check("T4 缺失 → 0", f.get("pm_momentum"), 0.0)

# T5 掉头：绝对价看多 + 动量向下 → 掉头 note
f, _, notes, _ = extract_factors(mk(delta=-0.05, up_price=0.62))
check("T5 掉头(看多+动量向下)note", any("PM 动量掉头" in n for n in notes), True)

# T6 同向：绝对价看多 + 动量向上 → 无掉头 note
f, _, notes, _ = extract_factors(mk(delta=0.05, up_price=0.62))
check("T6 同向(看多+动量向上)无掉头note", any("PM 动量掉头" in n for n in notes), False)

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
