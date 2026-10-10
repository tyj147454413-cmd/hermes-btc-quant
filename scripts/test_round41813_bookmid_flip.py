#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round41.8.13 功能测试：翻向共识价改用真实订单簿 book_up_mid（3pm 场标记价失真事故修复）。

回归场景（2026-08-24 3pm ET 实证）：
  up_price(mark)=0.705 但 book_up_mid=0.44 / 实时 best_ask=0.28 → 标记价失真假强共识，
  引擎被 round41.6 翻向骗着买涨，真实市场其实偏空。修复后翻向共识价优先 book_up_mid。

运行：python3 scripts/test_round41813_bookmid_flip.py"""
import os
import sys
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from signal_validator import _consensus_flip  # noqa: E402

PASS, FAIL = 0, 0
def check(name, got, want):
    global PASS, FAIL
    ok = got == want
    PASS += int(ok); FAIL += int(not ok)
    print(f"{'✅' if ok else '❌'} {name}: got={got!r} want={want!r}")


def run(mark_up=None, book_mid=None, book_imb=None, model_side="DOWN"):
    """构造最小 out/inp/check，跑一次 _consensus_flip，返回 (out, check, notes)。"""
    out = {"signal": model_side, "side": model_side, "action": "ORDER",
           "confidence": "中", "probability": 0.55, "max_price": 0.60, "ev": -5.0}
    inp = {}
    if mark_up is not None:
        inp["pm_up"] = mark_up
    if book_mid is not None:
        inp["book_up_mid"] = book_mid
    if book_imb is not None:
        inp["book_imbalance"] = book_imb
    check = {}
    notes = []
    d = SimpleNamespace(position_scale=0.5)
    _consensus_flip(out, inp, d, check, notes.append)
    return out, check, notes


# T1 3pm 事故场景：mark=70.5 假强共识 UP，book_mid=44 真实偏空 → 不应翻向，保持 DOWN
out, ck, notes = run(mark_up=70.5, book_mid=44.0, book_imb=-0.2697, model_side="DOWN")
check("T1 标记价失真(70.5 vs 44)不翻向,方向保持", out["side"], "DOWN")
check("T1 无 consensus_flip 标记", "consensus_flip" in ck, False)
check("T1 记入标记价失真预警 note", any("标记价失真" in n for n in notes), True)

# T2 book_mid 真强共识 UP(72) 且模型逆 DOWN → round42 G3.5 已禁用，**保留 DOWN**
# 历史行为（round41.6/41.8.13）：翻向 UP
# 新行为（round42 用户决策）：保留原方向，填 consensus_flip_disabled 插桩
out, ck, notes = run(mark_up=50.0, book_mid=72.0, model_side="DOWN")
check("T2 book_mid=72 强共识 round42 保留 DOWN(G3.5禁用)", out["side"], "DOWN")
check("T2 填 consensus_flip_disabled 源=book_mid",
      "(book_mid)" in ck.get("consensus_flip_disabled", ""), True)
check("T2 不填原 consensus_flip 字段(G3.5 已禁用)",
      "consensus_flip" not in ck, True)

# T3 book_mid 缺失 → 退回标记价（向后兼容旧快照）；round42 G3.5 禁用保留 DOWN
out, ck, notes = run(mark_up=70.5, book_mid=None, model_side="DOWN")
check("T3 无 book_mid 退回标记价 round42 保留 DOWN", out["side"], "DOWN")
check("T3 填 consensus_flip_disabled 源=mark",
      "(mark)" in ck.get("consensus_flip_disabled", ""), True)

# T4 book_mid 弱共识(56) 且模型逆 → 不翻向（60 边界不触发）
out, ck, notes = run(mark_up=70.5, book_mid=56.0, model_side="DOWN")
check("T4 book_mid=56 弱共识不翻向", out["side"], "DOWN")

# T5 模型顺 book 共识 → 不翻向（方向已一致）
out, ck, notes = run(mark_up=50.0, book_mid=72.0, model_side="UP")
check("T5 模型顺共识不翻向", out["side"], "UP")

# T6 两者均缺失 → 直接返回不报错
out, ck, notes = run(mark_up=None, book_mid=None, model_side="DOWN")
check("T6 无任何共识价直接返回", out["side"], "DOWN")

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
