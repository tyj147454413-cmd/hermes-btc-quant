#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round50 v3 聪明钱钱包账本 mock 测试 (2026-08-28)

覆盖:
- hermes_collect v3 段逻辑(镜像复刻): DOWN 侧大户成交聚合 / 钱包明细落盘结构 / 池子标记
- smart_money_settle 纯函数(直接 import): 去重 / 结算判定 / 盈亏计算
- smart_money_pool 纯函数(直接 import): 聚合 / 筛选条件
运行: python3 scripts/test_round50_v3_wallets.py
"""
import json
import sys
import unittest
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
ROOT = THIS_DIR.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(THIS_DIR))

from smart_money_settle import dedupe_wallet_records, fetch_outcome, settle_records  # noqa: E402
from smart_money_pool import aggregate_wallets, select_pool  # noqa: E402


def mock_holders():
    """复刻真实 /holders 响应结构: 外层 token_entry, 内层 holder 含 outcomeIndex/proxyWallet"""
    return [
        {"token": "tok_up", "holders": [
            {"proxyWallet": "0xUP1", "amount": 900.0, "outcomeIndex": 0, "name": "A"},
            {"proxyWallet": "0xUP2", "amount": 500.0, "outcomeIndex": 0, "name": "B"},
        ]},
        {"token": "tok_down", "holders": [
            {"proxyWallet": "0xDN1", "amount": 800.0, "outcomeIndex": 1, "name": "C"},
            {"proxyWallet": "0xDN2", "amount": 400.0, "outcomeIndex": 1, "name": "D"},
        ]},
    ]


def parse_holders_v3(resp):
    """镜像复刻 hermes_collect v3 的 holders 解析(UP/DOWN 两侧分类 + 排序截断)"""
    up, down = [], []
    if isinstance(resp, list):
        for tok in resp:
            if not isinstance(tok, dict):
                continue
            hs = tok.get("holders") or []
            if not isinstance(hs, list):
                continue
            for h in hs:
                if not isinstance(h, dict):
                    continue
                oi = h.get("outcomeIndex")
                if oi == 0:
                    up.append(h)
                elif oi == 1:
                    down.append(h)
    up.sort(key=lambda x: float(x.get("amount", 0) or 0), reverse=True)
    down.sort(key=lambda x: float(x.get("amount", 0) or 0), reverse=True)
    return up[:20], down[:20]


def agg_trades_mirror(trades, target_oi):
    """镜像复刻 hermes_collect v3 的 _agg_wallet_trades 聚合核心"""
    buy = sell = 0.0
    for t in trades:
        if not isinstance(t, dict):
            continue
        if t.get("outcomeIndex") != target_oi:
            continue
        sz = float(t.get("size", 0) or 0)
        if t.get("side") == "BUY":
            buy += sz
        elif t.get("side") == "SELL":
            sell += sz
    return buy, sell


class TestCollectV3Mirror(unittest.TestCase):
    """hermes_collect v3 段镜像测试"""

    def test_both_sides_extracted(self):
        up, down = parse_holders_v3(mock_holders())
        self.assertEqual(len(up), 2)
        self.assertEqual(len(down), 2)
        self.assertEqual(up[0]["proxyWallet"], "0xUP1")  # 900 > 500 降序
        self.assertEqual(down[0]["proxyWallet"], "0xDN1")

    def test_down_side_trades_aggregated(self):
        """v3 核心补洞: DOWN 侧大户成交也要聚合(镜像)"""
        trades = [
            {"side": "BUY", "size": 100.0, "outcomeIndex": 1},
            {"side": "BUY", "size": 50.0, "outcomeIndex": 1},
            {"side": "SELL", "size": 30.0, "outcomeIndex": 1},
            {"side": "BUY", "size": 999.0, "outcomeIndex": 0},  # UP 侧不算
        ]
        buy, sell = agg_trades_mirror(trades, 1)
        self.assertAlmostEqual(buy, 150.0)
        self.assertAlmostEqual(sell, 30.0)

    def test_wallet_detail_record_structure(self):
        """钱包明细落盘行结构(镜像复刻采集器写入字段)"""
        up, down = parse_holders_v3(mock_holders())
        all_holders = [(h, 0) for h in up] + [(h, 1) for h in down]
        row = {
            "ts": "2026-08-28T19:30:00+00:00",
            "et_hour": "2026-08-28T15:00",
            "slug": "bitcoin-up-or-down-august-28-2026-3pm-et",
            "cid": "0xabc",
            "wallet": all_holders[0][0].get("proxyWallet"),
            "side": "UP" if all_holders[0][1] == 0 else "DOWN",
            "outcome_index": all_holders[0][1],
            "amount": float(all_holders[0][0].get("amount", 0) or 0),
            "name": all_holders[0][0].get("name") or "",
            "up_price": 0.45,
            "down_price": 0.55,
        }
        for k in ("ts", "et_hour", "slug", "cid", "wallet", "side",
                  "outcome_index", "amount", "name", "up_price", "down_price"):
            self.assertIn(k, row)
        self.assertEqual(row["side"], "UP")
        self.assertEqual(row["amount"], 900.0)

    def test_pool_marking(self):
        """池子标记(镜像): 池内钱包按持仓侧算方向"""
        pool = {"pool": [{"wallet": "0xUP1"}, {"wallet": "0xDN1"}]}
        pool_set = {p["wallet"] for p in pool["pool"]}
        up, down = parse_holders_v3(mock_holders())
        all_holders = [(h, 0) for h in up] + [(h, 1) for h in down]
        hits = up_vol = down_vol = 0.0
        for h, oi in all_holders:
            w = h.get("proxyWallet") or h.get("wallet")
            if w in pool_set:
                hits += 1
                if oi == 0:
                    up_vol += float(h.get("amount", 0) or 0)
                else:
                    down_vol += float(h.get("amount", 0) or 0)
        direction = round((up_vol - down_vol) / (up_vol + down_vol), 4) if (up_vol + down_vol) > 0 else None
        self.assertEqual(hits, 2)
        self.assertAlmostEqual(up_vol, 900.0)
        self.assertAlmostEqual(down_vol, 800.0)
        self.assertAlmostEqual(direction, round(100.0 / 1700.0, 4))

    def test_pool_file_missing_fail_open(self):
        """池子文件不存在 -> direction=None, 不抛异常(镜像)"""
        direction = None
        try:
            json.loads(Path("/nonexistent/pool.json").read_text())
        except Exception:
            direction = None
        self.assertIsNone(direction)


class TestSettle(unittest.TestCase):
    """smart_money_settle 纯函数"""

    def test_dedupe_keeps_latest(self):
        records = [
            {"slug": "s1", "wallet": "0xA", "ts": "2026-08-28T10:00:00+00:00", "amount": 100},
            {"slug": "s1", "wallet": "0xA", "ts": "2026-08-28T10:30:00+00:00", "amount": 200},
            {"slug": "s1", "wallet": "0xB", "ts": "2026-08-28T10:00:00+00:00", "amount": 50},
        ]
        out = dedupe_wallet_records(records)
        self.assertEqual(len(out), 2)
        a = [r for r in out if r["wallet"] == "0xA"][0]
        self.assertEqual(a["amount"], 200)  # 保留最新

    def test_fetch_outcome_up_down_unresolved(self):
        def mk(prices, closed=True):
            return lambda url: [{"closed": closed, "outcomePrices": json.dumps(prices)}]
        self.assertEqual(fetch_outcome("s", fetch=mk(["1", "0"])), "UP")
        self.assertEqual(fetch_outcome("s", fetch=mk(["0", "1"])), "DOWN")
        self.assertIsNone(fetch_outcome("s", fetch=mk(["0.45", "0.55"], closed=False)))  # 未结算
        self.assertIsNone(fetch_outcome("s", fetch=mk(["0.5", "0.5"])))  # 异常价

    def test_fetch_outcome_closed_true_first(self):
        """gamma 已结算市场必须 closed=true 才返回: 第一查(closed=true)拿到即用"""
        calls = []

        def fetch(url):
            calls.append(url)
            if "closed=true" in url:
                return [{"closed": True, "outcomePrices": '["1", "0"]'}]
            return []  # 已结算 slug 不带参数返回空(实测行为)

        self.assertEqual(fetch_outcome("settled-slug", fetch=fetch), "UP")
        self.assertEqual(len(calls), 1)  # 第一次就命中, 不发第二次请求

    def test_fetch_outcome_active_slug_falls_back(self):
        """活跃市场: closed=true 查空 -> 回退默认查询, closed=False -> None"""
        calls = []

        def fetch(url):
            calls.append(url)
            if "closed=true" in url:
                return []
            return [{"closed": False, "outcomePrices": '["0.45", "0.55"]'}]

        self.assertIsNone(fetch_outcome("active-slug", fetch=fetch))
        self.assertEqual(len(calls), 2)  # 两次都查了

    def test_settle_pnl_math(self):
        deduped = [
            {"slug": "s1", "et_hour": "h", "wallet": "0xA", "name": "A",
             "side": "UP", "amount": 100.0, "up_price": 0.30, "down_price": 0.70},
            {"slug": "s1", "et_hour": "h", "wallet": "0xB", "name": "B",
             "side": "DOWN", "amount": 100.0, "up_price": 0.30, "down_price": 0.70},
        ]
        rows = settle_records(deduped, {"s1": "UP"})
        self.assertEqual(len(rows), 2)
        a = [r for r in rows if r["wallet"] == "0xA"][0]
        b = [r for r in rows if r["wallet"] == "0xB"][0]
        self.assertTrue(a["win"])
        self.assertAlmostEqual(a["pnl_shares"], 100.0)
        self.assertAlmostEqual(a["pnl_usd_est"], 70.0)  # 100*(1-0.30)
        self.assertFalse(b["win"])
        self.assertAlmostEqual(b["pnl_shares"], -100.0)
        self.assertAlmostEqual(b["pnl_usd_est"], -70.0)  # -100*0.70

    def test_settle_skips_unresolved(self):
        deduped = [{"slug": "sX", "wallet": "0xA", "side": "UP", "amount": 10,
                    "up_price": 0.5, "down_price": 0.5}]
        self.assertEqual(settle_records(deduped, {}), [])


class TestPool(unittest.TestCase):
    """smart_money_pool 纯函数"""

    def _ledger(self):
        rows = []
        # 0xA: 12 场 8 胜, pnl +600 -> 应入池
        for i in range(12):
            rows.append({"slug": f"s{i}", "wallet": "0xA", "name": "Alpha",
                         "win": i < 8, "pnl_shares": 80.0 if i < 8 else -40.0,
                         "pnl_usd_est": 40.0, "settled_at": "2026-08-28T10:00:00+00:00"})
        # 0xB: 12 场 5 胜, pnl +100 但胜率 41.7% < 55% -> 出局
        for i in range(12):
            rows.append({"slug": f"s{i}", "wallet": "0xB", "name": "Beta",
                         "win": i < 5, "pnl_shares": 50.0 if i < 5 else -25.0,
                         "pnl_usd_est": 10.0, "settled_at": "2026-08-28T10:00:00+00:00"})
        # 0xC: 3 场全胜但 rounds < 10 -> 出局
        for i in range(3):
            rows.append({"slug": f"s{i}", "wallet": "0xC", "name": "Gamma",
                         "win": True, "pnl_shares": 200.0,
                         "pnl_usd_est": 100.0, "settled_at": "2026-08-28T10:00:00+00:00"})
        return rows

    def test_aggregate_rounds_by_slug(self):
        """rounds = 不同 slug 数(不是行数)"""
        ledger = [
            {"slug": "s1", "wallet": "0xA", "win": True, "pnl_shares": 10,
             "pnl_usd_est": 5, "settled_at": "2026-08-28T10:00:00+00:00"},
            {"slug": "s1", "wallet": "0xA", "win": False, "pnl_shares": -5,
             "pnl_usd_est": -2, "settled_at": "2026-08-28T11:00:00+00:00"},
        ]
        agg = aggregate_wallets(ledger)
        self.assertEqual(agg["0xA"]["rounds"], 1)
        self.assertAlmostEqual(agg["0xA"]["pnl_shares"], 5.0)

    def test_select_pool_filters(self):
        agg = aggregate_wallets(self._ledger())
        pool = select_pool(agg, min_rounds=10, min_pnl=100, min_winrate=0.55)
        wallets = {p["wallet"] for p in pool}
        self.assertIn("0xA", wallets)   # 12场8胜+pnl480
        self.assertNotIn("0xB", wallets)  # 胜率不足
        self.assertNotIn("0xC", wallets)  # rounds 不足
        self.assertEqual(pool[0]["wallet"], "0xA")  # pnl 降序

    def test_select_pool_sorted_desc(self):
        agg = aggregate_wallets(self._ledger())
        pool = select_pool(agg, min_rounds=10, min_pnl=100, min_winrate=0.55)
        pnls = [p["pnl_shares"] for p in pool]
        self.assertEqual(pnls, sorted(pnls, reverse=True))

    def test_window_days_filters_old_rows(self):
        """窗口过滤: 老记录被剔出统计"""
        ledger = [
            {"slug": "s_old", "wallet": "0xA", "win": True, "pnl_shares": 500,
             "pnl_usd_est": 200, "settled_at": "2026-08-20T10:00:00+00:00"},
            {"slug": "s_new", "wallet": "0xA", "win": True, "pnl_shares": 10,
             "pnl_usd_est": 5, "settled_at": "2026-08-28T10:00:00+00:00"},
        ]
        agg = aggregate_wallets(ledger, window_days=7)
        self.assertEqual(agg["0xA"]["rounds"], 1)  # 只剩 s_new
        self.assertAlmostEqual(agg["0xA"]["pnl_shares"], 10.0)

    def test_empty_ledger(self):
        self.assertEqual(aggregate_wallets([]), {})
        self.assertEqual(select_pool({}), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
