#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round41.8.20 测试：价格位置因子锚定口径统一（用户指令「都改成锚定价格：24h 区间分位不使用」）。
核心断言：取值=deviation_pct 锚定口径；24h 分位彻底退出；PM 弱共识计 0（book 优先）。
运行：python3 scripts/test_round41820_pricepos_anchor.py"""
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

SNAP2AM = os.path.join(ROOT, "data", "snapshots",
                       "bitcoin-up-or-down-august-25-2026-2am-et_20260825_142705.json")
SNAP3AM = os.path.join(ROOT, "data", "snapshots",
                       "bitcoin-up-or-down-august-25-2026-3am-et_20260825_152657.json")

def load(path):
    return json.load(open(path))

def set_dev(d, pct):
    d["current_price"] = d["anchor_price"] * (1 + pct / 100.0)

# ===== 一、真实快照回归（事故场） =====
# T1 8/25 2AM（G4 触发场）：dev=+0.1604%>=0.15%，book 69% 非弱共识 -> +1
#    （旧 AI 层按 percentile 90.7% 极端位记 0，与机算 +1 两层不一致；新口径统一 +1）
d = load(SNAP2AM)
f, _, notes, _ = extract_factors(d)
check("T1 2AM 事故场 |dev|=0.16%>=0.15% -> +1", f.get("price_position"), 1.0)
check("T1b 无极端位 note（分位退出本因子）", any("极端位置" in n for n in notes), False)

# T2 8/25 3AM：dev=+0.0744%<0.15% 未选边 -> 0（旧机算口径 sign(dev)=+1）
d = load(SNAP3AM)
f, _, _, _ = extract_factors(d)
check("T2 3AM |dev|=0.074%<0.15% -> 0", f.get("price_position"), 0.0)

# ===== 二、锚定侧方向与边界 =====
d = load(SNAP2AM); set_dev(d, -0.20)
f, _, _, _ = extract_factors(d)
check("T3 dev=-0.20% -> -1（锚定下方）", f.get("price_position"), -1.0)

d = load(SNAP2AM); set_dev(d, 0.30)
f, _, _, _ = extract_factors(d)
check("T4 dev=+0.30% -> +1（锚定上方）", f.get("price_position"), 1.0)

d = load(SNAP2AM); set_dev(d, 0.0999)
f, _, _, _ = extract_factors(d)
check("T5a 边界 dev=0.0999%<0.10% -> 0", f.get("price_position"), 0.0)

d = load(SNAP2AM); set_dev(d, 0.1001)
f, _, _, _ = extract_factors(d)
check("T5b 边界 dev=0.1001%>=0.10% -> +1", f.get("price_position"), 1.0)

d = load(SNAP2AM); d["current_price"] = d["anchor_price"]
f, _, _, _ = extract_factors(d)
check("T6 dev=0（贴锚定）-> 0", f.get("price_position"), 0.0)

# ===== 三、24h 分位彻底退出（核心回归） =====
# T7 同一 dev=+0.16%，percentile=3/50/97 -> 全部 +1，分位不影响取值不发 note
for pct in (3.0, 50.0, 97.0):
    d = load(SNAP2AM)
    d.setdefault("price_position_24h", {})["current_percentile"] = pct
    f, _, notes, _ = extract_factors(d)
    check(f"T7 percentile={pct} 不影响取值 -> +1", f.get("price_position"), 1.0)
check("T7b 不产 price_position 极端位 note", any("price_position 极端位置" in n for n in notes), False)

# T8 极端高分位 + dev<0.15% -> 0（旧 AI「恐高」语义会记 -1，事故=8/25 1am）
d = load(SNAP3AM)
d.setdefault("price_position_24h", {})["current_percentile"] = 97.0
f, _, _, _ = extract_factors(d)
check("T8 极端高分位+dev<0.15% -> 0（禁恐高计分）", f.get("price_position"), 0.0)

# ===== 四、PM 弱共识计 0（round41.8.9 沿用，共识源 book 优先） =====
# T9 book 弱共识（0.45）但标记价强（0.70）-> 0（book 优先，防标记价误判）
d = load(SNAP2AM)
d["polymarket"]["book_up_mid"] = 0.45
d["polymarket"]["up_price"] = 0.70
d["polymarket"]["down_price"] = 0.30
f, _, notes, _ = extract_factors(d)
check("T9 book 弱共识 0.45 优先于标记价 0.70 -> 0", f.get("price_position"), 0.0)
check("T9b PM 弱共识 note（计 0 仅参考）", any("PM 弱共识" in n and "计 0" in n for n in notes), True)

# T10 book 缺失 + 标记价弱（0.55/0.45）-> 0（标记价回退判弱）
d = load(SNAP2AM)
d["polymarket"]["book_up_mid"] = None
d["polymarket"]["up_price"] = 0.55
d["polymarket"]["down_price"] = 0.45
f, _, notes, _ = extract_factors(d)
check("T10 book 缺失标记价弱 0.55/0.45 -> 0", f.get("price_position"), 0.0)
check("T10b PM 弱共识 note", any("PM 弱共识" in n for n in notes), True)

# T11 book 缺失 + 标记价强（0.75/0.25）-> +1（标记价回退判非弱）
d = load(SNAP2AM)
d["polymarket"]["book_up_mid"] = None
d["polymarket"]["up_price"] = 0.75
d["polymarket"]["down_price"] = 0.25
f, _, _, _ = extract_factors(d)
check("T11 book 缺失标记价强 0.75/0.25 -> +1", f.get("price_position"), 1.0)

# T12 2AM 原始快照：标记价 0.58/0.42（按标记价会误判弱共识）但 book 0.69 -> +1
d = load(SNAP2AM)
f, _, notes, _ = extract_factors(d)
check("T12 标记价 0.58<0.60 但 book 0.69 非弱 -> +1", f.get("price_position"), 1.0)
check("T12b 无 PM 弱共识 note", any("PM 弱共识" in n for n in notes), False)

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
