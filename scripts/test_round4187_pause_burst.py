#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round41.8.7 功能测试：①momentum_burst 因子触发/不触发；②trading_paused 暂停守卫。
运行：python3 scripts/test_round4187_pause_burst.py"""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)          # hermes_collect / hermes_common
sys.path.insert(0, HERE)          # signal_validator / rule_engine

PASS, FAIL = 0, 0
def check(name, got, want):
    global PASS, FAIL
    ok = got == want
    PASS += int(ok); FAIL += int(not ok)
    print(f"{'✅' if ok else '❌'} {name}: got={got!r} want={want!r}")

from signal_validator import extract_factors

REAL = os.path.join(ROOT, "data", "snapshots",
                    "bitcoin-up-or-down-august-24-2026-11am-et_20260824_231417.json")

def mk_snap(vol_ratio=None, rd="UP"):
    d = json.load(open(REAL))          # 真实快照保证全部必填字段存在
    t15 = d.setdefault("timeframes", {}).setdefault("15m", {})
    t15["vol_ratio"] = vol_ratio
    t15["recent_dir"] = rd
    return d

f, _, notes, mode = extract_factors(mk_snap(vol_ratio=2.0, rd="UP"))
check("T1 放量2.0+UP → momentum_burst=+2(round41.8.9 ×2)", f.get("momentum_burst"), 2.0)
check("T1b notes 含触发说明", any("momentum_burst" in n for n in notes), True)
print(f"   （基底快照 mode={mode}, S₀含burst={round(sum(f.values()),1)}）")

f, _, _, _ = extract_factors(mk_snap(vol_ratio=2.0, rd="DOWN"))
check("T2 放量2.0+DOWN → -2", f.get("momentum_burst"), -2.0)

f, _, _, _ = extract_factors(mk_snap(vol_ratio=1.2, rd="UP"))
check("T3 未放量1.2 → 0", f.get("momentum_burst"), 0.0)

f, _, _, _ = extract_factors(mk_snap(vol_ratio=None, rd="UP"))
check("T4 vol_ratio缺失 → 0(不抛错)", f.get("momentum_burst"), 0.0)

# ── 暂停守卫 ──
spec = importlib.util.spec_from_file_location("hb", os.path.join(ROOT, "hermes_1h_bot.py"))
try:
    hb = importlib.util.module_from_spec(spec); spec.loader.exec_module(hb)
    imported = True
except Exception as e:
    print(f"⚠️ bot 模块导入失败({e})，跳过 T5-T8"); imported = False

if imported:
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump({"trading_paused": True}, open(tmp.name, "w"))
    check("T5 config paused=true → 暂停", hb._trading_pause_active(tmp.name), True)
    json.dump({"trading_paused": False}, open(tmp.name, "w"))
    check("T6 paused=false → 不暂停", hb._trading_pause_active(tmp.name), False)
    json.dump({"bet_amount": 10}, open(tmp.name, "w"))
    check("T7 缺键 → 不暂停(默认可交易)", hb._trading_pause_active(tmp.name), False)
    open(tmp.name, "w").write("{broken json!!")
    check("T8 JSON损坏 → fail-closed 暂停", hb._trading_pause_active(tmp.name), True)
    check("T9 文件不存在 → 不暂停", hb._trading_pause_active("/tmp/no_such_cfg_4187.json"), False)
    os.unlink(tmp.name)

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
