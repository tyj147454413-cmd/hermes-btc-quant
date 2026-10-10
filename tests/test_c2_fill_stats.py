"""C2 最小回归测试（2026-08-23）。

覆盖:
1. _fetch_fill_stats — trades API 真实成交加权均价（taker/maker 两路、无匹配、异常回退）
2. _record_fill avg_price 落档 + 追价回写 CHASED_ 镜像（幂等、已有终态不覆盖）

全部隔离在临时目录，不触碰真实 data/hermes_fills.jsonl；不发起网络请求。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import hermes_1h_bot as hb  # noqa: E402


class FakePage:
    def __init__(self, items, has_more=False):
        self.items = items
        self.has_more = has_more


class FakePaginator:
    def __init__(self, pages):
        self._pages = pages

    def __iter__(self):
        return iter(self._pages)


class FakeClient:
    def __init__(self, trades=None, raise_exc=False):
        self._trades = trades or []
        self._raise = raise_exc
        self.calls = []

    def list_account_trades(self, token_id=None, **kw):
        self.calls.append(token_id)
        if self._raise:
            raise RuntimeError("api down")
        return FakePaginator([FakePage(self._trades)])


def _taker_trade(price, size, oid="OID1"):
    return NS(taker_order_id=oid, maker_orders=(), price=price, size=size)


def _maker_trade(maker_oid, price, matched, taker_oid="someone-else"):
    mo = NS(order_id=maker_oid, price=price, matched_amount=matched)
    return NS(taker_order_id=taker_oid, maker_orders=(mo,), price=price,
              size=matched)


def _dummy_bot(client, live=True):
    # 跳过 __init__(避免钱包/网络初始化)，仅注入方法所需属性
    b = hb.HermesBot.__new__(hb.HermesBot)
    b.live = live
    b.client = client
    return b


class TestFetchFillStats(unittest.TestCase):
    def test_taker_weighted_avg(self):
        trades = [_taker_trade(0.52, 10), _taker_trade(0.56, 30)]
        bot = _dummy_bot(FakeClient(trades))
        stats = hb.HermesBot._fetch_fill_stats(bot, "OID1", "tok")
        self.assertAlmostEqual(stats["size_matched"], 40.0)
        # 加权均价 (0.52*10 + 0.56*30)/40 = 0.55
        self.assertAlmostEqual(stats["avg_price"], 0.55, places=6)

    def test_maker_fills_matched_by_order_id(self):
        trades = [_maker_trade("OID1", 0.61, 20), _taker_trade(0.50, 5, oid="OTHER")]
        bot = _dummy_bot(FakeClient(trades))
        stats = hb.HermesBot._fetch_fill_stats(bot, "OID1", "tok")
        self.assertAlmostEqual(stats["size_matched"], 20.0)
        self.assertAlmostEqual(stats["avg_price"], 0.61, places=6)

    def test_no_match_returns_empty(self):
        bot = _dummy_bot(FakeClient([_taker_trade(0.5, 10, oid="X")]))
        self.assertEqual(hb.HermesBot._fetch_fill_stats(bot, "OID1", "tok"), {})

    def test_api_exception_returns_empty(self):
        bot = _dummy_bot(FakeClient(raise_exc=True))
        self.assertEqual(hb.HermesBot._fetch_fill_stats(bot, "OID1", "tok"), {})

    def test_paper_or_not_live_skipped(self):
        self.assertEqual(
            hb.HermesBot._fetch_fill_stats(_dummy_bot(None), "PAPER_123", None), {})
        self.assertEqual(
            hb.HermesBot._fetch_fill_stats(_dummy_bot(FakeClient(), live=False), "OID", None), {})

    def test_token_filter_forwarded(self):
        client = FakeClient([_taker_trade(0.5, 10)])
        bot = _dummy_bot(client)
        hb.HermesBot._fetch_fill_stats(bot, "OID1", "TOK")
        self.assertEqual(client.calls, ["TOK"])


class TestRecordFillAndChaseWriteback(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._fills_backup = hb.FILLS
        hb.FILLS = self.tmp / "hermes_fills.jsonl"

    def tearDown(self):
        hb.FILLS = self._fills_backup

    def _lines(self):
        if not hb.FILLS.exists():
            return []
        return [json.loads(l) for l in hb.FILLS.read_text().splitlines() if l.strip()]

    def test_avg_price_recorded(self):
        bot = _dummy_bot(None)
        hb.HermesBot._record_fill(bot, "OID1", "s1", "UP", True, "FILLED",
                                  size_matched=12.3456, avg_price=0.545)
        rec = self._lines()[0]
        self.assertEqual(rec["avg_price"], 0.545)  # round(...,4) → 0.545
        self.assertEqual(rec["size_matched"], 12.3456)

    def test_zero_avg_price_not_written(self):
        bot = _dummy_bot(None)
        hb.HermesBot._record_fill(bot, "OID1", "s1", "UP", True, "FILLED", avg_price=0.0)
        self.assertNotIn("avg_price", self._lines()[0])

    def test_chase_writeback_mirror(self):
        bot = _dummy_bot(None)
        info = {"orig_order_id": "ORIG"}
        hb.HermesBot._record_fill(bot, "NEW1", "s1", "UP", True, "FILLED",
                                  avg_price=0.58, info=info)
        lines = {r["order_id"]: r for r in self._lines()}
        self.assertIn("NEW1", lines)
        mirror = lines.get("ORIG")
        self.assertIsNotNone(mirror)
        self.assertEqual(mirror["status"], "CHASED_FILLED")
        self.assertEqual(mirror["repriced_to"], "NEW1")
        self.assertTrue(mirror["filled"])
        self.assertEqual(mirror["avg_price"], 0.58)

    def test_chase_writeback_unfilled_mirror(self):
        bot = _dummy_bot(None)
        info = {"orig_order_id": "ORIG"}
        hb.HermesBot._record_fill(bot, "NEW1", "s1", "DOWN", False,
                                  "TIMEOUT_CANCELLED", info=info)
        lines = {r["order_id"]: r for r in self._lines()}
        self.assertFalse(lines["ORIG"]["filled"])
        self.assertEqual(lines["ORIG"]["status"], "CHASED_TIMEOUT_CANCELLED")

    def test_multi_chase_collapses_to_head(self):
        """二段追价: B 的链头是 A → C 的终态镜像直接落到 A，B 不产生记录"""
        bot = _dummy_bot(None)
        info_b = {"orig_order_id": "A"}
        # 模拟追价时新单继承链头
        head = info_b.get("orig_order_id") or "B"
        info_c = {"orig_order_id": head}
        hb.HermesBot._record_fill(bot, "C", "s1", "UP", True, "FILLED",
                                  avg_price=0.59, info=info_c)
        lines = {r["order_id"]: r for r in self._lines()}
        # 新单 C 自身终态 + 镜像落到链头 A；中间单 B 不产生任何记录
        self.assertEqual(set(lines.keys()), {"C", "A"})
        self.assertEqual(lines["A"]["status"], "CHASED_FILLED")

    def test_idempotent_no_duplicate(self):
        bot = _dummy_bot(None)
        info = {"orig_order_id": "ORIG"}
        hb.HermesBot._record_fill(bot, "NEW1", "s1", "UP", True, "FILLED",
                                  avg_price=0.58, info=info)
        hb.HermesBot._record_fill(bot, "NEW1", "s1", "UP", True, "FILLED",
                                  avg_price=0.58, info=info)
        self.assertEqual(len(self._lines()), 2)  # NEW1 + ORIG 各一条

    def test_existing_orig_terminal_not_overwritten(self):
        bot = _dummy_bot(None)
        hb.HermesBot._record_fill(bot, "ORIG", "s1", "UP", False, "REPRICE_FAILED")
        hb.HermesBot._record_fill(bot, "NEW1", "s1", "UP", True, "FILLED",
                                  avg_price=0.58, info={"orig_order_id": "ORIG"})
        lines = {r["order_id"]: r for r in self._lines()}
        # 首挂已有终态(REPRICE_FAILED) → 不被镜像覆盖
        self.assertEqual(lines["ORIG"]["status"], "REPRICE_FAILED")


if __name__ == "__main__":
    unittest.main()
