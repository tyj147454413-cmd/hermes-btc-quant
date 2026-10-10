#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round41.8.16 功能测试：pm_momentum delta 同源优先真实订单簿（标记价失真污染修复）。

事故场景（2026-08-24 3pm->4pm ET 实证）：
  3pm 快照 up_price(mark)=0.705（假强共识，真实盘口 book_up_mid=0.44）；
  4pm 快照 mark=0.44 / book=0.415。
  旧口径 mark-mark delta = 0.44-0.705 = -0.265（假暴跌，触发 pm_momentum -1）；
  真实口径 book-book delta = 0.415-0.44 = -0.025。
  且旧掉头检测用 mark 绝对价：mark 0.705 看多 vs delta 向下 = 假掉头。

修复后（采集器）：book-book 同源优先，任一侧缺 book 整体退 mark-mark，绝不混合；
up_price_delta_source(book|mark) 供消费端同源与归因。
修复后（消费端）：掉头检测绝对价跟随 delta 源（book 源优先 book_up_mid）。

运行：python3 scripts/test_round41816_pmmomentum_book.py"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from signal_validator import extract_factors  # noqa: E402

PASS, FAIL = 0, 0
def check(name, got, want):
    global PASS, FAIL
    ok = got == want
    PASS += int(ok); FAIL += int(not ok)
    print(f"{'✅' if ok else '❌'} {name}: got={got!r} want={want!r}")


_BASE = None
def _base():
    """4pm 真实快照做底座（字段齐全），测试只覆盖 polymarket 段。"""
    global _BASE
    if _BASE is None:
        _BASE = json.load(open(os.path.join(
            ROOT, "data", "snapshots",
            "bitcoin-up-or-down-august-24-2026-4pm-et_20260825_041445.json")))
    import copy
    return copy.deepcopy(_BASE)


def mk(delta, source=None, book_mid=None, mark_up=None):
    """在真实快照底座上覆盖 polymarket 动量相关字段。"""
    md = _base()
    pm = md.setdefault("polymarket", {})
    pm["up_price_delta"] = delta
    pm.pop("up_price_delta_source", None)
    if source is not None:
        pm["up_price_delta_source"] = source
    if book_mid is not None:
        pm["book_up_mid"] = book_mid
    if mark_up is not None:
        pm["up_price"] = mark_up
    return md


def pmm(md):
    factors, notes, _ = None, None, None
    factors, inp, notes, mode = extract_factors(md)
    return factors.get("pm_momentum"), notes


# ---- 消费端：掉头检测同源 ----

# T1 4pm 场真实形态：delta=-0.025 源=book，book=0.415 看空、mark=0.44 看空（同向无掉头）
v, notes = pmm(mk(-0.025, "book", 0.415, 0.44))
check("T1 book 源：|Δ|≥0.02 计 -2（round44.1 权重×2）", v, -2.0)
check("T1 book 源：绝对价用 book 同向 -> 无假掉头", any("掉头" in n for n in notes), False)
check("T1 note 标注源=book", any("源=book" in n for n in notes), True)

# T2 假掉头拦截（3pm 形态）：delta=-0.03 源=book，book=0.44 看空、mark=0.705 看多
#    旧逻辑绝对价用 mark 0.705 -> 看多 vs 动量向下 = 假掉头；新逻辑 book 0.44 看空同向 -> 无掉头
v, notes = pmm(mk(-0.03, "book", 0.44, 0.705))
check("T2 book 源：mark 失真不再产假掉头", any("掉头" in n for n in notes), False)

# T3 mark 源真掉头仍正常触发：delta=-0.03 源=mark，mark=0.62 看多（book 缺失）
v, notes = pmm(mk(-0.03, "mark", None, 0.62))
check("T3 mark 源：真掉头（看多+动量向下）仍触发", any("PM 动量掉头" in n for n in notes), True)
check("T3 note 标注源=mark", any("源=mark" in n for n in notes), True)

# T4 旧快照（41.8.15 前无 source 字段）-> 按 mark 源处理：mark=0.62 看多 + delta=-0.03 -> 掉头
v, notes = pmm(mk(-0.03, None, 0.44, 0.62))
check("T4 无 source 字段：按 mark 源，mark 看多+动量向下 -> 掉头", any("PM 动量掉头" in n for n in notes), True)

# T5 |Δ|<0.02 阈值 -> 因子 0
v, notes = pmm(mk(-0.015, "book", 0.415, 0.44))
check("T5 |Δ|<0.02 不计分", v, 0.0)

# T6 delta 缺失 -> 因子 0 不报错
v, notes = pmm(mk(None, None, None, 0.5))
check("T6 delta 缺失计 0", v, 0.0)

# ---- 消费端：4pm 真实快照回归（旧快照无 source，行为与实盘信号一致）----
snap4 = os.path.join(ROOT, "data", "snapshots",
                     "bitcoin-up-or-down-august-24-2026-4pm-et_20260825_041445.json")
if os.path.exists(snap4):
    md4 = json.load(open(snap4))
    v, notes = pmm(md4)
    check("T7 4pm 真实快照回归：pm_momentum=-2（round44.1 权重×2，方向语义与实盘一致）", v, -2.0)
    check("T7 4pm 真实快照回归：无掉头（mark 0.44 看空同向）",
          any("PM 动量掉头" in n for n in notes), False)
else:
    print("⚠️ T7 跳过：4pm 快照不存在")

# ---- 采集器：同源 delta 计算逻辑重放（3pm->4pm 快照对）----
snap3 = os.path.join(ROOT, "data", "snapshots",
                     "bitcoin-up-or-down-august-24-2026-3pm-et_20260825_031837.json")
if os.path.exists(snap4) and os.path.exists(snap3):
    pm4 = (json.load(open(snap4)).get("polymarket") or {})
    pm3 = (json.load(open(snap3)).get("polymarket") or {})
    cur_book, cur_mark = pm4.get("book_up_mid"), pm4.get("up_price")
    prev_book, prev_mark = pm3.get("book_up_mid"), pm3.get("up_price")
    book_ok = (isinstance(cur_book, (int, float)) and 0.0 < cur_book < 1.0
               and isinstance(prev_book, (int, float)) and 0.0 < prev_book < 1.0)
    if book_ok:
        delta = round(cur_book - prev_book, 4)
        src = "book"
    else:
        delta = round(cur_mark - prev_mark, 4)
        src = "mark"
    check("T8 采集器重放 3pm->4pm：源=book", src, "book")
    check("T8 采集器重放 3pm->4pm：delta=-0.025（非 mark 假暴跌 -0.265）", delta, -0.025)
else:
    print("⚠️ T8 跳过：3pm/4pm 快照不齐")

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
