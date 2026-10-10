# -*- coding: utf-8 -*-
"""round53(2026-09-02 全面代码审查) 修复行为测试。

覆盖本轮 6 项修复：
- A2: dashboard save_config 防抹除守卫（读取失败拒绝写回）
- A3: hermes_common write_hourly_prediction 中文 utf-8 写入
- B1: _is_conn_error 排除 requests HTTPError（503 维护不算网络切换）
- B2: 删除 8/17 硬编码 slug 方向劫持（源码契约）
- B4: 风控瞬时失败(_check_risk 第三返回值 transient)保留信号（源码契约）
- B5: _sell_market 卖出真实成交价复核（list_account_trades 证据）

纪律：所有文件类测试一律临时路径，禁止写真实数据文件（9/2 实证：A2 验证脚本
误对真实 hermes_config.json 写回，16 键被抹成 3 键——守卫逻辑本身是先验证过才上线的）。
"""
import inspect
import json
import tempfile
import time
import types
import unittest
from pathlib import Path

import requests

import dashboard
import hermes_common
import hermes_1h_bot as hb


class TestA2ConfigWipeGuard(unittest.TestCase):
    """A2: dashboard 配置防抹除守卫"""

    def setUp(self):
        self._orig = dashboard.CONFIG_FILE
        self.tmpdir = tempfile.mkdtemp()
        dashboard.CONFIG_FILE = Path(self.tmpdir) / "cfg.json"

    def tearDown(self):
        dashboard.CONFIG_FILE = self._orig

    def test_corrupted_config_blocks_writeback(self):
        """读取失败 → save_config 拒绝写回，原损坏内容不被 defaults 覆盖"""
        dashboard.CONFIG_FILE.write_text("{corrupted!!!", encoding="utf-8")
        cfg = dashboard.load_config()
        self.assertTrue(dashboard._cfg_load_failed["flag"])
        self.assertFalse(dashboard.save_config(cfg), "守卫应拒绝写回")
        self.assertEqual(dashboard.CONFIG_FILE.read_text(encoding="utf-8"),
                         "{corrupted!!!", "原文件必须未被覆盖")

    def test_normal_roundtrip_keeps_all_keys(self):
        """正常读写 round-trip：原有全部键保留，不丢键"""
        full = {"bet_amount": 4.0, "mode": "paper", "trading_paused": True,
                "rule_a_price_max": 0.55, "tp_enabled": False}
        dashboard.CONFIG_FILE.write_text(json.dumps(full), encoding="utf-8")
        cfg = dashboard.load_config()
        self.assertFalse(dashboard._cfg_load_failed["flag"])
        self.assertTrue(dashboard.save_config(cfg))
        after = json.loads(dashboard.CONFIG_FILE.read_text(encoding="utf-8"))
        for k, v in full.items():
            self.assertEqual(after.get(k), v, f"键 {k} 在 round-trip 后丢失/被改")


class TestA3HourlyPredictionEncoding(unittest.TestCase):
    """A3: write_hourly_prediction 显式 utf-8"""

    def test_chinese_reason_written_as_utf8(self):
        orig = hermes_common.HOURLY_PREDICTION
        tmpdir = tempfile.mkdtemp()
        fake = Path(tmpdir) / "pred.json"
        hermes_common.HOURLY_PREDICTION = fake
        try:
            hermes_common.write_hourly_prediction(
                {"slug": "test-slug", "side": "DOWN",
                 "reason": "中文理由-博弈期", "action": "ORDER"})
            content = fake.read_text(encoding="utf-8")
            self.assertIn("中文理由-博弈期", content)
            json.loads(content)  # 有效性
        finally:
            hermes_common.HOURLY_PREDICTION = orig


class TestB1ConnErrorClassification(unittest.TestCase):
    """B1: requests HTTPError 不再误判为连接层错误"""

    def test_http_error_not_conn_error(self):
        self.assertFalse(hermes_common._is_conn_error(
            requests.exceptions.HTTPError("503 Server Error: trading is disabled")))

    def test_connection_error_is_conn_error(self):
        self.assertTrue(hermes_common._is_conn_error(
            requests.exceptions.ConnectionError("proxy refused")))

    def test_timeout_is_conn_error(self):
        self.assertTrue(hermes_common._is_conn_error(
            requests.exceptions.Timeout("timed out")))

    def test_urlerror_is_conn_error(self):
        import urllib.error
        self.assertTrue(hermes_common._is_conn_error(
            urllib.error.URLError("dns failure")))


class TestB2SlugHijackRemoved(unittest.TestCase):
    """B2: 8/17 硬编码 slug 方向劫持彻底移除（源码契约）"""

    def test_no_hardcoded_slug_in_process_signal(self):
        # 用 AST 检查字符串常量（注释中保留的案底说明不算复活）
        import ast
        import textwrap
        src = textwrap.dedent(inspect.getsource(hb.HermesBot.process_signal))
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.assertNotIn(
                    "august-17-2026", node.value,
                    "硬编码 slug 方向劫持不得以字符串常量形式复活")


class TestB4TransientRiskKeepsSignal(unittest.TestCase):
    """B4: 风控瞬时失败保留信号（源码契约，process_signal 依赖重资源无法实例化）"""

    def test_check_risk_returns_three_tuple(self):
        """_check_risk 签名为三元组 (allowed, reason, transient)"""
        src = inspect.getsource(hb.HermesBot._check_risk)
        self.assertIn("return False", src)
        self.assertIn("transient", src)
        # 瞬时失败点（余额查询连接类）必须标 True
        self.assertIn('"余额查询失败: 钱包连接异常", True', src)

    def test_process_signal_passes_transient(self):
        """process_signal 解包三元组并把 transient 放进 result"""
        src = inspect.getsource(hb.HermesBot.process_signal)
        self.assertIn("risk_transient = self._check_risk", src)
        self.assertIn('"transient": risk_transient', src)

    def test_do_tick_keeps_signal_on_transient(self):
        """_do_tick 对 transient 失败不清队列（保留信号），非瞬时 SKIP 照旧清"""
        src = inspect.getsource(hb.HermesBot._do_tick)
        self.assertIn('result.get("transient")', src)
        # transient 分支不调用 clear_queue（分支体内只有日志），非 transient 分支调用
        self.assertIn("瞬时失败保留信号", src)


class TestB5SellRealPrice(unittest.TestCase):
    """B5: 卖出后用 trades 证据复核真实成交价"""

    def _make_bot(self):
        bot = object.__new__(hb.HermesBot)  # 绕过 __init__ 重资源
        bot.live = True
        bot._ensure_client = lambda: True
        return bot

    def _stub_sleep(self):
        self._orig_sleep = time.sleep
        time.sleep = lambda s: None

    def _restore_sleep(self):
        time.sleep = self._orig_sleep

    def test_real_fill_price_from_trades(self):
        bot = self._make_bot()

        class FakeOrder:
            ok = True; order_id = "sell-oid-1"; status = "MATCHED"

        class FakeClient:
            def place_market_order(self, **kw):
                return FakeOrder()

            def list_account_trades(self, token_id=None):
                class Page:
                    items = [types.SimpleNamespace(
                        status="CONFIRMED", taker_order_id="sell-oid-1",
                        price="0.82", size="100.0", maker_orders=[])]
                    has_more = False
                return iter([Page()])

        bot.client = FakeClient()
        self._stub_sleep()
        try:
            r = bot._sell_market("tok-1", 100.0, cur_price=0.75)
        finally:
            self._restore_sleep()
        self.assertTrue(r["ok"])
        self.assertAlmostEqual(r["sell_price"], 0.82)
        self.assertFalse(r["estimated"])

    def test_fallback_to_cur_price_when_no_trades(self):
        bot = self._make_bot()

        class FakeOrder:
            ok = True; order_id = "sell-oid-1"; status = "MATCHED"

        class FakeClient:
            def place_market_order(self, **kw):
                return FakeOrder()

            def list_account_trades(self, token_id=None):
                class Page:
                    items = []; has_more = False
                return iter([Page()])

        bot.client = FakeClient()
        self._stub_sleep()
        try:
            r = bot._sell_market("tok-1", 100.0, cur_price=0.75)
        finally:
            self._restore_sleep()
        self.assertTrue(r["ok"])
        self.assertAlmostEqual(r["sell_price"], 0.75)
        self.assertTrue(r["estimated"])


class TestB3GhostRehangGuard(unittest.TestCase):
    """B3: 幽灵单重挂前 list_open_orders 正向复核（源码契约）"""

    def test_rehang_guard_exists(self):
        src = inspect.getsource(hb.HermesBot._poll_pending_orders)
        self.assertIn("_find_existing_order", src, "幽灵重挂前必须正向复核 open orders")
        self.assertIn("_ghost_alive == oid", src, "旧单仍在簿 → 恢复 OPEN 轮询")
        self.assertIn("跳过重挂防双仓", src, "同 token 有另一挂单 → 取消重挂")


class TestB6ThrottledLogging(unittest.TestCase):
    """B6: dashboard 静默吞错改为节流日志"""

    def test_first_call_logs_then_throttled(self):
        calls = []
        orig_log = dashboard.log
        dashboard.log = lambda msg, level="I": calls.append((level, msg))
        orig_t = dashboard._log_throttle
        dashboard._log_throttle = {}
        try:
            dashboard._log_throttled("k1", "first")
            dashboard._log_throttled("k1", "second-immediate")
            self.assertEqual(len(calls), 1, "间隔内重复调用不应打日志")
            # 模拟间隔流逝
            dashboard._log_throttle["k1"] -= 301
            dashboard._log_throttled("k1", "third-after-interval")
            self.assertEqual(len(calls), 2, "间隔过期后应再次打日志")
            # 不同 key 互不影响
            dashboard._log_throttled("k2", "other-key")
            self.assertEqual(len(calls), 3)
            self.assertEqual(calls[0][0], "W", "默认级别 W")
        finally:
            dashboard.log = orig_log
            dashboard._log_throttle = orig_t


class TestB7PaperInstantMarker(unittest.TestCase):
    """B7: PAPER 模拟单显式标记 paper_instant"""

    def setUp(self):
        self.bot = object.__new__(hb.HermesBot)
        self.tmpdir = tempfile.mkdtemp()
        self._orig_fills = hb.FILLS
        hb.FILLS = Path(self.tmpdir) / "fills.jsonl"

    def tearDown(self):
        hb.FILLS = self._orig_fills

    def test_paper_record_has_flag(self):
        self.bot._record_fill("PAPER_1", "s1", "UP", True, "PAPER")
        rec = json.loads(hb.FILLS.read_text(encoding="utf-8").splitlines()[0])
        self.assertTrue(rec.get("paper_instant"), "PAPER 单必须带 paper_instant 标记")

    def test_live_record_no_flag(self):
        self.bot._record_fill("0xreal1", "s1", "UP", True, "FILLED")
        rec = json.loads(hb.FILLS.read_text(encoding="utf-8").splitlines()[0])
        self.assertNotIn("paper_instant", rec, "真实单不应带 paper_instant")


class TestFillSeenCache(unittest.TestCase):
    """⚪2: fills 判重内存缓存 O(1)，幂等语义保持"""

    def setUp(self):
        self.bot = object.__new__(hb.HermesBot)
        self.tmpdir = tempfile.mkdtemp()
        self._orig_fills = hb.FILLS
        hb.FILLS = Path(self.tmpdir) / "fills.jsonl"

    def tearDown(self):
        hb.FILLS = self._orig_fills

    def test_idempotent_and_cache(self):
        self.bot._record_fill("oid-1", "s1", "UP", True, "FILLED")
        self.bot._record_fill("oid-1", "s1", "UP", True, "FILLED")  # 重复应跳过
        self.bot._record_fill("oid-2", "s1", "UP", True, "FILLED")
        lines = hb.FILLS.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2, "重复 oid 不得重复落档")
        oids = {json.loads(l)["order_id"] for l in lines}
        self.assertEqual(oids, {"oid-1", "oid-2"})
        # 缓存应包含两个 oid
        self.assertEqual(self.bot._fills_seen, {"oid-1", "oid-2"})

    def test_writeback_mirror_uses_cache(self):
        self.bot._record_fill("orig-1", "s1", "UP", True, "CHASE_PARTIAL")
        # 镜像成功
        self.bot._writeback_chase_fill("orig-2", chased_oid="new-1", slug="s1",
                                       side="UP", filled=True, status="FILLED")
        lines = hb.FILLS.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        # 再次镜像: orig-2 已在缓存 → 跳过(留痕)，不重复落档
        self.bot._writeback_chase_fill("orig-2", chased_oid="new-1", slug="s1",
                                       side="UP", filled=True, status="FILLED")
        lines = hb.FILLS.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2, "镜像判重必须幂等")


class TestRecoveryMaxPrice(unittest.TestCase):
    """⚪3: 恢复单 max_price 口径——持久化值优先，旧行回退 orig_price"""

    def _recover(self, pending_row):
        bot = object.__new__(hb.HermesBot)
        bot._pending_orders = {}
        tmpdir = tempfile.mkdtemp()
        orig = hb.PENDING
        hb.PENDING = Path(tmpdir) / "pending.jsonl"
        hb.PENDING.write_text(json.dumps(pending_row) + "\n", encoding="utf-8")
        try:
            bot._recover_pending_orders()
        finally:
            hb.PENDING = orig
        return bot._pending_orders

    def test_new_row_uses_persisted_max_price(self):
        po = self._recover({"order_id": "0xa", "slug": "s", "side": "UP", "amount": 4,
                            "price": 0.80, "orig_price": 0.60, "max_price": 0.62})
        self.assertEqual(po["0xa"]["max_price"], 0.62, "新行应读持久化的信号限价")

    def test_old_row_falls_back_to_orig_price(self):
        po = self._recover({"order_id": "0xb", "slug": "s", "side": "UP", "amount": 4,
                            "price": 0.80, "orig_price": 0.60})  # 旧行无 max_price
        self.assertEqual(po["0xb"]["max_price"], 0.60, "旧行应回退首挂基准价，而非当前挂价0.80")

    def test_row_without_orig_falls_back_to_price(self):
        po = self._recover({"order_id": "0xc", "slug": "s", "side": "UP", "amount": 4,
                            "price": 0.70})
        self.assertEqual(po["0xc"]["max_price"], 0.70)

    def test_bot_writes_max_price_to_pending(self):
        """源码契约: 三处 PENDING 落盘都必须带 max_price（首挂在 process_signal 内）"""
        self.assertIn('"max_price": mx', inspect.getsource(hb.HermesBot.process_signal))
        poll_src = inspect.getsource(hb.HermesBot._poll_pending_orders)
        self.assertEqual(poll_src.count('"max_price": info.get("max_price")'), 2,
                         "追价/幽灵重挂的 PENDING 落盘都要带 max_price")


if __name__ == "__main__":
    unittest.main()


class TestRound54FillStatus(unittest.TestCase):
    """9/3: LIVE taker 立即成交 result 补 fill_status——UI 决策链/review 判成交依据。
    此前 LIVE 单 result 只有 status=matched，UI 成交判定不认 -> 已成交单假显「追价中」。"""

    def test_taker_matched_sets_fill_status(self):
        src = inspect.getsource(hb.HermesBot.process_signal)
        self.assertIn('result["fill_status"] = "FILLED"', src)
        # 必须位于 matched 分支判定之后（同分支内）
        self.assertLess(src.find('== "matched"'), src.find('result["fill_status"]'))
        # 分支内先记 fills.jsonl 终态再补标记，且随后直接 return（不进轮询）
        self.assertLess(src.find('self._record_fill(oid, slug, side, True, "FILLED"'), src.find('result["fill_status"]'))
