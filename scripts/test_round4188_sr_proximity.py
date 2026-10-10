#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round41.8.9 测试：sr_proximity 不记分（仅参考，notes 产出读数、不写 factors 不影响 S0）。
运行：python3 scripts/test_round4188_sr_proximity.py"""
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

def mk_snap(weak_resistance=None, weak_support=None, current_price=None, percentile=None):
    d = json.load(open(REAL))
    d.setdefault("key_levels", {})
    d["key_levels"]["weak_resistance"] = weak_resistance
    d["key_levels"]["weak_support"] = weak_support
    if current_price is not None:
        d["current_price"] = current_price
    if percentile is not None:
        d.setdefault("price_position_24h", {})["current_percentile"] = percentile
    return d

# T1-T4：不记分（不在 factors）+ 参考 note 产出
# T1 逼近阻力 → 不在 factors，note 含"参考,不计分"
f, _, notes, _ = extract_factors(mk_snap(weak_resistance=80000.0, weak_support=76000.0, current_price=79840.0))
check("T1 逼近阻力 → sr_proximity 不在 factors(不记分)", "sr_proximity" in f, False)
check("T1b note 含参考说明", any("sr_proximity(参考,不计分)" in n for n in notes), True)

# T2 逼近支撑 → 不记分 + note
f, _, notes, _ = extract_factors(mk_snap(weak_resistance=80000.0, weak_support=76000.0, current_price=76152.0))
check("T2 逼近支撑 → 不在 factors", "sr_proximity" in f, False)
check("T2b note 含参考说明", any("sr_proximity(参考,不计分)" in n for n in notes), True)

# T3 中间区域 → 不记分 + 无 note
f, _, notes, _ = extract_factors(mk_snap(weak_resistance=80000.0, weak_support=76000.0, current_price=78000.0))
check("T3 中间区域 → 不在 factors", "sr_proximity" in f, False)
check("T3b 无参考 note", any("sr_proximity" in n for n in notes), False)

# T4 S0 不受 sr_proximity 影响：同快照 S0 与去掉 sr_proximity 前一致（本质=不写 factors）
f_near, _, _, _ = extract_factors(mk_snap(weak_resistance=80000.0, weak_support=76000.0, current_price=79840.0))
f_mid, _, _, _ = extract_factors(mk_snap(weak_resistance=80000.0, weak_support=76000.0, current_price=78000.0))
# 两者 S0 差异仅来自 key_levels 距离之外的其他因子（sr_proximity 不参与，故两快照 current_price 不同会导致 price_position/dev 不同，
# 此处仅验证 sr_proximity 键确实不存在于 factors，避免计分）
check("T4 逼近阻力快照无 sr_proximity 计分键", "sr_proximity" not in f_near, True)

# T5-T6：key_levels 缺失降级 percentile 参考 note
d = mk_snap(weak_resistance=None, weak_support=None, percentile=94.0)
f, _, notes, _ = extract_factors(d)
check("T5 key_levels缺失 percentile≥90 → 不在 factors", "sr_proximity" in f, False)
check("T5b note 含极端高位参考", any("sr_proximity(参考,不计分)" in n for n in notes), True)

f, _, notes, _ = extract_factors(mk_snap(weak_resistance=None, weak_support=None, percentile=5.0))
check("T6 key_levels缺失 percentile≤10 → 不在 factors", "sr_proximity" in f, False)
check("T6b note 含极端低位参考", any("sr_proximity(参考,不计分)" in n for n in notes), True)

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
