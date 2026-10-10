#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round41.8.13 第二步测试：实时复核防线 _fetch_live_pm_book 同时拉标记价 + clob 订单簿。

验证：① 正常拉取解析出 up/down 标记价 + book_up_mid/book_imbalance；② 标记价与盘口脱节时
book 字段仍正确返回（供上层优先用 book_mid）；③ clob 缺失/异常时 fail-open 退回标记价；
④ gamma 缺失时返回空 dict。

运行：python3 scripts/test_round41813_live_book.py"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import signal_validator as v  # noqa: E402

PASS, FAIL = 0, 0
def check(name, got, want):
    global PASS, FAIL
    ok = got == want
    PASS += int(ok); FAIL += int(not ok)
    print(f"{'✅' if ok else '❌'} {name}: got={got!r} want={want!r}")


# ---- 伪造网络层：按 url 分发返回 ----
_CALLS = []
_FAKE_GAMMA = None
_FAKE_BOOK = None
_RAISE_BOOK = False

def _fake_http(url, timeout=10, proxy=None, **kw):
    _CALLS.append(url)
    if "gamma-api" in url:
        if _FAKE_GAMMA is None:
            return None
        return _FAKE_GAMMA
    if "clob.polymarket.com/book" in url:
        if _RAISE_BOOK:
            raise RuntimeError("clob down")
        return _FAKE_BOOK
    return None


v.http_get_json = _fake_http
v.GAMMA_API = "https://gamma-api.polymarket.com"
v.CLOB_API = "https://clob.polymarket.com"

_MK = {
    "outcomePrices": "[0.705, 0.295]",
    "clobTokenIds": '["tok_up", "tok_down"]',
    "enableOrderBook": True,
}
_BOOK = {
    "bids": [{"price": "0.43", "size": "10"}, {"price": "0.42", "size": "5"}],
    "asks": [{"price": "0.45", "size": "20"}, {"price": "0.46", "size": "8"}],
}


def reset():
    global _FAKE_GAMMA, _FAKE_BOOK, _RAISE_BOOK
    _CALLS.clear()
    _FAKE_GAMMA, _FAKE_BOOK, _RAISE_BOOK = None, None, False


# T1 正常：标记价 + 订单簿都拉到
reset(); _FAKE_GAMMA = [_MK]; _FAKE_BOOK = _BOOK
r = v._fetch_live_pm_book("test-slug")
check("T1 up 标记价", r.get("up"), 0.705)
check("T1 down 标记价", r.get("down"), 0.295)
check("T1 book_up_mid=(0.43+0.45)/2", r.get("book_up_mid"), 0.44)
# imbalance: 买盘中段 10+5? 0.3-0.7 区间：bids 0.43/0.42 都在区间=15, asks 0.45/0.46=28 → (15-28)/43
check("T1 book_imbalance=(15-28)/43", round(r.get("book_imbalance") or 0, 4), round((15 - 28) / 43, 4))

# T2 clob 缺失 → fail-open 退回标记价，book 字段 None
reset(); _FAKE_GAMMA = [dict(_MK, clobTokenIds="[]", enableOrderBook=False)]; _FAKE_BOOK = None
r = v._fetch_live_pm_book("test-slug")
check("T2 clob 缺失 up 仍在", r.get("up"), 0.705)
check("T2 clob 缺失 book_up_mid=None", r.get("book_up_mid"), None)

# T3 clob 拉取抛异常 → 退回标记价
reset(); _FAKE_GAMMA = [_MK]; _RAISE_BOOK = True
r = v._fetch_live_pm_book("test-slug")
check("T3 clob 异常 up 仍在", r.get("up"), 0.705)
check("T3 clob 异常 book_up_mid=None", r.get("book_up_mid"), None)

# T4 gamma 缺失 → 空 dict
reset(); _FAKE_GAMMA = None
r = v._fetch_live_pm_book("test-slug")
check("T4 gamma 缺失返回空 dict", r, {})

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
