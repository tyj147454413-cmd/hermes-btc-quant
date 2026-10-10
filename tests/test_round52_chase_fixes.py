# -*- coding: utf-8 -*-
"""9/2 追价链修复行为场景测试（BUG-1~6）。

覆盖:
1. BUG-1/BUG-4: PENDING 落盘含 token_id/chase_count/matched 字段; 恢复单还原档位与 token
2. BUG-2: 追价链部分成交继承——撤旧单部分成交并入链、新单金额扣减、链尾终态合并、
   CHASE_PARTIAL(剩余<$1)、TIMEOUT_PARTIAL(继承份额+本单零成交)、REPRICE_PARTIAL
3. BUG-3: 撤单竞态——整单成交(状态+FILLED 或 trades 证据)不重挂防双仓
4. BUG-5: 追价封顶绑定 probability_raw（praw=0.67→cap 0.65; praw=0.846→cap 0.80）
5. BUG-6: edge 守门——tier② 抬价时 mid<拟付价→撤单SKIP; tier①(ask≤0.65)豁免

全部隔离在临时目录，不触碰真实 data/*.jsonl；不发网络请求；time.sleep 打桩。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import hermes_1h_bot as hb  # noqa: E402


def _dummy_bot(client=None, live=True):
    b = hb.HermesBot.__new__(hb.HermesBot)
    b.live = live
    b.client = client
    b._pending_orders = {}
    return b


class FakeClient:
    def __init__(self):
        self.cancelled = []

    def cancel_order(self, order_id=None, **kw):
        self.cancelled.append(order_id)


class ChaseTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._bak = (hb.FILLS, hb.PENDING, hb.CHASE_ALERTS)
        hb.FILLS = self.tmp / "fills.jsonl"
        hb.PENDING = self.tmp / "pending.jsonl"
        hb.CHASE_ALERTS = self.tmp / "chase_alerts.jsonl"
        self._sleep = time.sleep
        time.sleep = lambda s: None  # 打桩: 竞态窗口不真等

    def tearDown(self):
        hb.FILLS, hb.PENDING, hb.CHASE_ALERTS = self._bak
        time.sleep = self._sleep

    def _fills(self):
        if not hb.FILLS.exists():
            return []
        return [json.loads(l) for l in hb.FILLS.read_text(encoding="utf-8").splitlines() if l.strip()]

    def _pending_lines(self):
        if not hb.PENDING.exists():
            return []
        return [json.loads(l) for l in hb.PENDING.read_text(encoding="utf-8").splitlines() if l.strip()]


class TestRecoverPending(ChaseTestBase):
    """BUG-1/BUG-4: 恢复单还原 token_id / chase_count / matched 份额"""

    def test_recover_keeps_token_and_chase_count(self):
        hb.PENDING.write_text(json.dumps({
            "order_id": "OID_R", "slug": "s1", "side": "DOWN",
            "amount": 4.0, "price": 0.65, "orig_price": 0.60,
            "price_cap": 0.65, "token_id": "TOK123",
            "probability_raw": 0.846, "chase_count": 2,
            "matched_size": 1.5, "matched_notional": 0.9,
            "ts": "2026-09-02T06:36:18+00:00",
        }) + "\n", encoding="utf-8")
        bot = _dummy_bot()
        hb.HermesBot._recover_pending_orders(bot)
        info = bot._pending_orders["OID_R"]
        self.assertEqual(info["token_id"], "TOK123")   # BUG-1: 追价必需
        self.assertEqual(info["chase_count"], 2)        # BUG-4: 档位不被重置
        self.assertEqual(info["matched_size"], 1.5)     # BUG-2: 部分成交继承
        self.assertEqual(info["matched_notional"], 0.9)
        self.assertEqual(info["price"], 0.65)

    def test_recover_legacy_line_without_new_fields(self):
        # 旧格式记录(无 token_id/chase_count)不崩, 默认值兜底
        hb.PENDING.write_text(json.dumps({
            "order_id": "OID_OLD", "slug": "s1", "side": "UP",
            "amount": 4.0, "price": 0.60, "ts": "2026-09-02T06:36:18+00:00",
        }) + "\n", encoding="utf-8")
        bot = _dummy_bot()
        hb.HermesBot._recover_pending_orders(bot)
        info = bot._pending_orders["OID_OLD"]
        self.assertEqual(info["token_id"], "")
        self.assertEqual(info["chase_count"], 0)
        self.assertEqual(info["matched_size"], 0.0)


class TestChaseScenarios(ChaseTestBase):
    """追价分支行为场景（mock 全外部依赖）"""

    def _make_bot(self, *, first_status="OPEN", post_status="CANCELLED",
                  fill_stats=None, ask=0.78, spread=0.12,
                  mid=0.865, place_result=None):
        """mock 真实时序: 轮询首次查状态(挂单活着=OPEN) → 撤单 → 复查(post_status)"""
        client = FakeClient()
        bot = _dummy_bot(client)
        placed = []
        calls = {"n": 0}

        def _status(oid, token_id=None):
            calls["n"] += 1
            return first_status if calls["n"] == 1 else post_status

        def _place(token_id, price, sz, side):
            placed.append((token_id, price, sz, side))
            if place_result is not None:
                return place_result
            return {"ok": True, "order_id": "NEW_OID", "status": "live"}

        bot._check_order_status = _status
        bot._fetch_fill_stats = lambda oid, token_id=None: dict(fill_stats or {})
        bot._read_matched = lambda oid: None
        bot._check_liquidity = lambda token_id, max_price: (True, ask, spread, "test")
        bot._gamma_price = lambda slug, side: mid
        bot.place_order = _place
        return bot, client, placed

    def _pending_info(self, **over):
        info = {"slug": "s1", "side": "DOWN", "ts": time.time() - 300,
                "amount": 7.0, "token_id": "TOK123", "retry": 0,
                "max_price": 0.60, "price": 0.65, "orig_price": 0.60,
                "price_cap": 0.65, "probability_raw": 0.846,
                "chase_count": 1, "last_chase_check": 0.0}
        info.update(over)
        return info

    def test_a_cancel_race_full_fill_no_replace(self):
        """场景A: 撤单瞬间整单成交(状态FILLED) → 记FILLED不重挂, 防双仓"""
        bot, client, placed = self._make_bot(first_status="OPEN", post_status="FILLED",
                                             fill_stats={"size_matched": 11.67, "avg_price": 0.60})
        bot._pending_orders["OID1"] = self._pending_info()
        bot._poll_pending_orders()
        self.assertEqual(placed, [])                    # 不重挂
        self.assertEqual(client.cancelled, ["OID1"])
        recs = {r["order_id"]: r for r in self._fills()}
        self.assertTrue(recs["OID1"]["filled"])
        self.assertEqual(recs["OID1"]["status"], "FILLED")
        self.assertNotIn("OID1", bot._pending_orders)

    def test_a2_cancel_race_full_fill_by_trades_evidence(self):
        """场景A2: 状态查不到但 trades 证据份额≈整单 → 视为整单成交, 不重挂"""
        bot, client, placed = self._make_bot(first_status="OPEN", post_status="UNKNOWN",
                                             fill_stats={"size_matched": 11.5, "avg_price": 0.60})
        # amount 7.0 / price 0.65 = 10.77sh 预期; 11.5 >= 98% → 整单
        bot._pending_orders["OID1"] = self._pending_info()
        bot._poll_pending_orders()
        self.assertEqual(placed, [])
        recs = {r["order_id"]: r for r in self._fills()}
        self.assertEqual(recs["OID1"]["status"], "FILLED")

    def test_b_partial_fill_carried_into_chain(self):
        """场景B: 撤单前部分成交 2sh@0.60 → 并入链, 新单金额扣减, pending 带继承字段"""
        bot, client, placed = self._make_bot(first_status="OPEN", post_status="CANCELLED",
                                             fill_stats={"size_matched": 2.0, "avg_price": 0.60})
        bot._pending_orders["OID1"] = self._pending_info()  # amount 7.0 price 0.65
        bot._poll_pending_orders()
        # 新单: 剩余预算 7.0-1.2=5.8 → 5.8/0.70=8.29sh @ 0.70
        self.assertEqual(len(placed), 1)
        self.assertAlmostEqual(placed[0][1], 0.70)
        self.assertAlmostEqual(placed[0][2], 5.8 / 0.70, places=2)
        new_info = bot._pending_orders["NEW_OID"]
        self.assertAlmostEqual(new_info["matched_size"], 2.0)
        self.assertAlmostEqual(new_info["matched_notional"], 1.2)
        self.assertAlmostEqual(new_info["amount"], 5.8)
        self.assertEqual(new_info["chase_count"], 2)
        # PENDING 落盘含 token_id/chase_count/matched（BUG-1/4/2）
        rows = {r["order_id"]: r for r in self._pending_lines()}
        self.assertEqual(rows["NEW_OID"]["token_id"], "TOK123")
        self.assertEqual(rows["NEW_OID"]["chase_count"], 2)
        self.assertAlmostEqual(rows["NEW_OID"]["matched_size"], 2.0)
        # 部分成交不单独落终态(防镜像撞车), fills 此时为空
        self.assertEqual(self._fills(), [])

    def test_c_partial_remainder_under_1_dollar(self):
        """场景C: 部分成交后剩余预算<$1 → CHASE_PARTIAL 终态, 不重挂"""
        bot, client, placed = self._make_bot(first_status="OPEN", post_status="CANCELLED",
                                             fill_stats={"size_matched": 5.5, "avg_price": 0.60})
        bot._pending_orders["OID1"] = self._pending_info(amount=4.0, price=0.60, chase_count=0)
        # matched notional 3.3 → remain 0.7 < 1
        bot._poll_pending_orders()
        self.assertEqual(placed, [])
        recs = {r["order_id"]: r for r in self._fills()}
        self.assertTrue(recs["OID1"]["filled"])
        self.assertEqual(recs["OID1"]["status"], "CHASE_PARTIAL")
        self.assertAlmostEqual(recs["OID1"]["size_matched"], 5.5)
        self.assertAlmostEqual(recs["OID1"]["avg_price"], 0.60)
        self.assertNotIn("OID1", bot._pending_orders)

    def test_d_dynamic_cap_strong_signal_chases(self):
        """场景D: praw=0.846 → 封顶0.80, ask0.78 档位0.70 → 正常重挂0.70"""
        bot, client, placed = self._make_bot(first_status="OPEN")
        bot._pending_orders["OID1"] = self._pending_info(probability_raw=0.846)
        bot._poll_pending_orders()
        self.assertEqual(len(placed), 1)
        self.assertAlmostEqual(placed[0][1], 0.70)

    def test_d2_unbounded_chase_weak_signal(self):
        """场景D2(9/2 round52.2 语义反转): 无封顶一直追——praw=0.67 弱信号、ask0.70
        (>旧封顶0.65)也重挂档位价 0.70(chase=1→第2档)，不再维持原单只记影子"""
        bot, client, placed = self._make_bot(first_status="OPEN", ask=0.70)
        bot._pending_orders["OID1"] = self._pending_info(probability_raw=0.67)
        bot._poll_pending_orders()
        self.assertEqual(len(placed), 1)
        self.assertAlmostEqual(placed[0][1], 0.70)       # 档位价 = CHASE_TARGETS[1]
        self.assertNotIn("OID1", bot._pending_orders)    # 旧单撤、新单入链

    def test_e_gate_blocks_only_on_consensus_flip(self):
        """场景E(9/2 收窄): 守门只剩共识反转线——mid=0.45<0.50(方向反了) → 撤单SKIP;
        原拟付价抬线(BUG-6)已随无封顶追价移除"""
        bot, client, placed = self._make_bot(first_status="OPEN", mid=0.45)
        bot._pending_orders["OID1"] = self._pending_info(probability_raw=0.846)
        bot._poll_pending_orders()
        self.assertEqual(placed, [])
        self.assertEqual(client.cancelled, ["OID1"])
        recs = {r["order_id"]: r for r in self._fills()}
        self.assertEqual(recs["OID1"]["status"], "RECHECK_CANCELLED")

    def test_f_tier1_cheap_eat_passes_gate(self):
        """场景F: tier① 便宜吃价通过守门——挂价0.60, ask0.63(>挂价且≤0.65), mid0.55>0.50
        → taker 吃价 0.63 放行（守门收窄后无需豁免概念，线恒为 0.50）"""
        bot, client, placed = self._make_bot(first_status="OPEN", ask=0.63, mid=0.55)
        bot._pending_orders["OID1"] = self._pending_info(
            price=0.60, max_price=0.60, chase_count=0, probability_raw=0.846)
        bot._poll_pending_orders()
        self.assertEqual(len(placed), 1)
        self.assertAlmostEqual(placed[0][1], 0.63)       # taker 吃价不被拦

    def test_g_reprlace_failure_with_inherited_partial(self):
        """场景G: 重挂失败且链上已有部分成交 → REPRICE_PARTIAL(filled=True)而非纯未成交"""
        bot, client, placed = self._make_bot(first_status="OPEN", post_status="CANCELLED",
                                             fill_stats={"size_matched": 2.0, "avg_price": 0.60},
                                             place_result={"ok": False, "error": "net down"})
        bot._pending_orders["OID1"] = self._pending_info()
        bot._poll_pending_orders()
        recs = {r["order_id"]: r for r in self._fills()}
        self.assertTrue(recs["OID1"]["filled"])
        self.assertEqual(recs["OID1"]["status"], "REPRICE_PARTIAL")
        self.assertAlmostEqual(recs["OID1"]["size_matched"], 2.0)

    def test_h_timeout_with_inherited_partial(self):
        """场景H: 超时取消, 本单零成交但链上继承2sh → TIMEOUT_PARTIAL而非TIMEOUT_CANCELLED"""
        bot, client, placed = self._make_bot(first_status="OPEN",
                                             fill_stats={})  # 本单无成交
        info = self._pending_info(matched_size=2.0, matched_notional=1.2,
                                  ts=time.time() - 26 * 60)  # 超过 ORDER_TTL_MIN=25
        bot._pending_orders["OID1"] = info
        bot._poll_pending_orders()
        self.assertEqual(placed, [])
        recs = {r["order_id"]: r for r in self._fills()}
        self.assertEqual(recs["OID1"]["status"], "TIMEOUT_PARTIAL")
        self.assertTrue(recs["OID1"]["filled"])
        self.assertAlmostEqual(recs["OID1"]["size_matched"], 2.0)
        self.assertAlmostEqual(recs["OID1"]["avg_price"], 0.60)

    def test_i_ask_above_old_cap_still_queues(self):
        """场景I(round52.2 核心修复): ask0.99 远超旧封顶0.80 → 不再死区维持，
        挂当前档位价排队等回落；本例 chase=0→_tier_count=1→档位0.65"""
        bot, client, placed = self._make_bot(first_status="OPEN", ask=0.99)
        bot._pending_orders["OID1"] = self._pending_info(
            chase_count=0, price=0.60, max_price=0.60, orig_price=0.60)
        bot._poll_pending_orders()
        self.assertEqual(len(placed), 1)
        self.assertAlmostEqual(placed[0][1], 0.65)       # min(ask0.99, 档位0.65)

    def test_j_tier_extends_beyond_five(self):
        """场景J(round52.2 无封顶延伸): chase=5 → 第6档=0.85+0.05=0.90, ask0.99 → 挂0.90;
        档位超过 CHASE_TARGETS 5 档后继续 +0.05 上移, 不因次数上限停摆"""
        bot, client, placed = self._make_bot(first_status="OPEN", ask=0.99)
        bot._pending_orders["OID1"] = self._pending_info(
            chase_count=5, price=0.85, max_price=0.85, orig_price=0.60)
        bot._poll_pending_orders()
        self.assertEqual(len(placed), 1)
        self.assertAlmostEqual(placed[0][1], 0.90)

    def test_k_ghost_order_requeues_after_unknown(self):
        """场景K(round52.3 幽灵单): 重启打断撤旧挂新 → CLOB查不到(UNKNOWN)+零成交,
        连续5次核实后按当前档位重挂接续追价链(无旧单可撤,跳过cancel)"""
        bot, client, placed = self._make_bot(first_status="UNKNOWN", post_status="UNKNOWN", ask=0.91)
        bot._pending_orders["OID1"] = self._pending_info(chase_count=2, price=0.70)
        for _ in range(4):
            bot._poll_pending_orders()
        self.assertEqual(placed, [])                     # 前4次 unknown_count<5 不动作
        bot._poll_pending_orders()                       # 第5次 → 触发幽灵重挂
        self.assertEqual(len(placed), 1)
        self.assertAlmostEqual(placed[0][1], 0.75)       # chase=2 → 第3档 0.75, min(ask0.91, 0.75)
        self.assertNotIn("OID1", bot._pending_orders)    # 幽灵记录移除

    def test_k2_ghost_gate_blocks_on_consensus_flip(self):
        """场景K2(round52.3): 幽灵单重挂前守门——mid<0.50 共识反转 → 不重挂"""
        bot, client, placed = self._make_bot(first_status="UNKNOWN", ask=0.91, mid=0.45)
        bot._pending_orders["OID1"] = self._pending_info(chase_count=2, price=0.70)
        for _ in range(5):
            bot._poll_pending_orders()
        self.assertEqual(placed, [])

    def test_l_first_order_taker_chase_baseline(self):
        """场景L(9/3 BUG-2 修复): round52.4 首挂吃价单(0.93)首次追价——档位以现挂价为
        基线(+0.05)，不再按旧档位 0.65 空转到档位爬上来；ask0.97 → 挂 min(0.97, 0.98)=0.97"""
        bot, client, placed = self._make_bot(first_status="OPEN", ask=0.97)
        bot._pending_orders["OID1"] = self._pending_info(
            chase_count=0, price=0.93, max_price=0.93, orig_price=0.93)
        bot._poll_pending_orders()
        self.assertEqual(len(placed), 1)
        self.assertAlmostEqual(placed[0][1], 0.97)       # 档位=max(0.65, 0.93+0.05)=0.98, min(ask,档位)

    def test_m_ghost_no_downgrade_from_taker_price(self):
        """场景M(9/3 BUG-2 修复): 首挂吃价单(0.93)幽灵重挂不降档——旧口径会挂 0.65
        (降 0.28, 9/3 7am-et 实证整小时踏空)；现取 max(现挂价, 档位)=0.93, 再被 ask 封顶"""
        bot, client, placed = self._make_bot(first_status="UNKNOWN", post_status="UNKNOWN", ask=0.95)
        bot._pending_orders["OID1"] = self._pending_info(
            chase_count=0, price=0.93, max_price=0.93, orig_price=0.93)
        for _ in range(5):
            bot._poll_pending_orders()
        self.assertEqual(len(placed), 1)
        self.assertAlmostEqual(placed[0][1], 0.93)       # max(0.93, 档位0.65) 不降档

    def test_n_timeout_client_unavailable_keeps_tracking(self):
        """场景N(9/3 BUG-3 修复): 超时到达但 client 不可用(None)——原实现静默删 PENDING
        追踪(挂单留簿到结算/fills无终态/风控漏计敞口)；现保留追踪待 client 恢复收口"""
        bot, client, placed = self._make_bot(first_status="OPEN", post_status="UNKNOWN",
                                             fill_stats={})
        bot.client = None                                # 网络故障窗口重建失败
        bot._pending_orders["OID1"] = self._pending_info(
            ts=time.time() - 26 * 60)                    # 超过 ORDER_TTL_MIN=25
        bot._poll_pending_orders()
        self.assertIn("OID1", bot._pending_orders)       # 追踪保留，不丢
        self.assertEqual(self._fills(), [])              # 无终态落档
        self.assertEqual(placed, [])                      # 无新下单


class TestShutdownPreserveKeys(ChaseTestBase):
    """P1-4(9/3): SIGTERM 紧急关闭保留重写必须携带恢复挂单全套键。

    硬约束: 恢复挂单必须含 price/token_id/chase_count——原实现仅写 9 键，
    缺 chase_count/max_price/probability_raw/matched_size/matched_notional，
    重启恢复后档位从头爬、部分成交份额蒸发。
    """

    def test_preserve_keeps_chase_and_matched_fields(self):
        hb.PENDING.write_text(json.dumps({
            "order_id": "OID_S", "slug": "s1", "side": "DOWN",
            "amount": 5.8, "price": 0.70, "orig_price": 0.60,
            "price_cap": 0.80, "max_price": 0.93, "token_id": "TOK123",
            "orig_order_id": "HEAD_OID", "probability_raw": 0.846,
            "chase_count": 3, "matched_size": 2.0, "matched_notional": 1.2,
            "ts": time.time() - 60,   # 窗口内 → preserved
        }) + "\n", encoding="utf-8")
        bot = _dummy_bot(client=FakeClient())
        bot._pending_orders = {}    # SIGTERM 路径直接扫 PENDING 文件前的内存态
        # 构造: _pending_orders 空但 PENDING 文件有记录——走文件路径需先 recover，
        # 这里直接调用 _cancel_all_pending_orders（其内存态从 _pending_orders 取）
        bot._pending_orders["OID_S"] = {
            "slug": "s1", "side": "DOWN", "ts": time.time() - 60,
            "amount": 5.8, "price": 0.70, "orig_price": 0.60,
            "price_cap": 0.80, "max_price": 0.93, "token_id": "TOK123",
            "orig_order_id": "HEAD_OID", "probability_raw": 0.846,
            "chase_count": 3, "matched_size": 2.0, "matched_notional": 1.2,
            "retry": 0,
        }
        bot._fetch_fill_stats = lambda oid, token_id=None: {}
        bot._read_matched = lambda oid: None
        bot._check_order_status = lambda oid, token_id=None: "OPEN"
        bot.client.list_open_orders = lambda: []
        hb.HermesBot._cancel_all_pending_orders(bot)
        rows = {r["order_id"]: r for r in self._pending_lines()}
        self.assertIn("OID_S", rows)                       # 保留不撤
        for k, v in (("chase_count", 3), ("max_price", 0.93),
                      ("probability_raw", 0.846), ("matched_size", 2.0),
                      ("matched_notional", 1.2)):
            self.assertAlmostEqual(rows["OID_S"][k], v, msg=f"保留重写丢键: {k}")


class TestRecordFillInheritance(ChaseTestBase):
    """BUG-2: _record_fill 部分成交继承合并"""

    def test_merge_inherited_partial_into_terminal(self):
        bot = _dummy_bot()
        info = {"orig_order_id": "HEAD", "matched_size": 2.0, "matched_notional": 1.2}
        hb.HermesBot._record_fill(bot, "NEW1", "s1", "DOWN", True, "FILLED",
                                  size_matched=8.0, avg_price=0.70, info=info)
        recs = {r["order_id"]: r for r in self._fills()}
        # 总份额 2+8=10, 加权均价 (1.2 + 8*0.7)/10 = 0.68
        self.assertAlmostEqual(recs["NEW1"]["size_matched"], 10.0)
        self.assertAlmostEqual(recs["NEW1"]["avg_price"], 0.68)
        # 链头镜像同样携带合并后的总份额
        self.assertAlmostEqual(recs["HEAD"]["size_matched"], 10.0)

    def test_unfilled_flips_to_filled_when_inherited(self):
        bot = _dummy_bot()
        info = {"matched_size": 2.0, "matched_notional": 1.2}
        hb.HermesBot._record_fill(bot, "NEW1", "s1", "DOWN", False, "SOME_FAIL", info=info)
        rec = self._fills()[0]
        self.assertTrue(rec["filled"])                   # 有真实持仓不能再记未成交
        self.assertAlmostEqual(rec["size_matched"], 2.0)

    def test_no_inheritance_no_change(self):
        bot = _dummy_bot()
        hb.HermesBot._record_fill(bot, "OID1", "s1", "UP", False, "TIMEOUT_CANCELLED", info={})
        rec = self._fills()[0]
        self.assertFalse(rec["filled"])
        self.assertNotIn("size_matched", rec)


class TestRound524FirstOrderTaker(unittest.TestCase):
    """round52.4 首挂吃价契约测试：两段式废除，首挂一律按实时 best_ask 跨价吃。

    process_signal 依赖钱包/gamma/流动性等重资源，无法纯 mock 单测；此处沿用
    round46 源码契约模式，锁定关键定价分支，防止后续改动悄悄复活低价静置挂单。
    """

    @classmethod
    def setUpClass(cls):
        import inspect
        cls._src = inspect.getsource(hb.HermesBot.process_signal)

    def test_no_maker_static_branch(self):
        """两段式已废除: maker静置 模式串不得再出现在首挂定价"""
        self.assertNotIn("maker静置", self._src)

    def test_taker_branch_eats_ask(self):
        """ask≤0.99 一律 taker 吃实时 ask（跨价单立即成交）"""
        self.assertIn("elif best_ask <= 0.99:", self._src)
        self.assertIn("order_price = round(best_ask, 2)", self._src)

    def test_deprecated_constants_not_used(self):
        """FAST_TAKE_MAX/MAKER_CAP/MAKER_DISCOUNT 不得回到 execute_order 主流程"""
        for c in ("FAST_TAKE_MAX", "MAKER_CAP", "MAKER_DISCOUNT"):
            self.assertNotIn(c, self._src)

    def test_hard_floor_still_guards(self):
        """0.25 硬底线仍在公共必经点（吃价路径同样受拦）"""
        self.assertIn("order_price < 0.25", self._src)

    def test_book_missing_fallback_kept(self):
        """book 缺失兜底口径保留（信号价封顶）"""
        self.assertIn("book缺失兜底", self._src)


if __name__ == "__main__":
    unittest.main()
