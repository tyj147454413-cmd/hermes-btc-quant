#!/usr/bin/env python3
"""round50 回测脚本 mock 测试 — 覆盖脚本逻辑路径不依赖真实数据

测试目标:
1. 三因子判据函数 (sig_trades / sig_holders / sig_large_trade) 各档位
2. wilson95 计算正确性
3. 主回测主路径能跑 (mock snapshot + mock results)
4. render_markdown 输出格式
5. Edge cases: eff_n=0 / 全部正确 / 全部错误
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.round50_factor_candidate_backtest import (
    sig_trades, sig_holders, sig_large_trade,
    wilson95, round50_backtest, render_markdown,
)


class TestRound50FactorSigs(unittest.TestCase):
    """三因子判据函数"""

    def test_sig_trades_positive(self):
        self.assertEqual(sig_trades({"trades_15m": {"net_up_imbalance": 0.5}}), 1)
        self.assertEqual(sig_trades({"trades_15m": {"net_up_imbalance": 0.31}}), 1)

    def test_sig_trades_negative(self):
        self.assertEqual(sig_trades({"trades_15m": {"net_up_imbalance": -0.5}}), -1)
        self.assertEqual(sig_trades({"trades_15m": {"net_up_imbalance": -0.31}}), -1)

    def test_sig_trades_neutral_zone(self):
        self.assertEqual(sig_trades({"trades_15m": {"net_up_imbalance": 0.3}}), 0)
        self.assertEqual(sig_trades({"trades_15m": {"net_up_imbalance": 0.0}}), 0)
        self.assertEqual(sig_trades({"trades_15m": {"net_up_imbalance": -0.3}}), 0)

    def test_sig_trades_missing(self):
        self.assertEqual(sig_trades({}), 0)  # 无 polymarket
        self.assertEqual(sig_trades({"polymarket": None}), 0)
        self.assertEqual(sig_trades({"polymarket": {}}), 0)
        self.assertEqual(sig_trades({"polymarket": {"trades_15m": None}}), 0)
        self.assertEqual(sig_trades({"polymarket": {"trades_15m": {"net_up_imbalance": None}}}), 0)

    def test_sig_holders_with_wallets(self):
        self.assertEqual(
            sig_holders({"smart_money_consensus_1h": {"net_up_imbalance": 0.5, "wallets_total": 5}}),
            1,
        )

    def test_sig_holders_no_wallets(self):
        """wallets_total=0 时不算信号 (无持仓大户)"""
        self.assertEqual(
            sig_holders({"smart_money_consensus_1h": {"net_up_imbalance": 0.5, "wallets_total": 0}}),
            0,
        )

    def test_sig_holders_missing(self):
        self.assertEqual(sig_holders({}), 0)

    def test_sig_large_trade_positive(self):
        self.assertEqual(sig_large_trade({"large_trade_5m": 0.5, "large_trade_count_5m": 3}, "5m"), 1)

    def test_sig_large_trade_no_count(self):
        """count=0 时不算信号"""
        self.assertEqual(sig_large_trade({"large_trade_5m": 0.5, "large_trade_count_5m": 0}, "5m"), 0)

    def test_sig_large_trade_missing(self):
        self.assertEqual(sig_large_trade({}, "5m"), 0)
        self.assertEqual(sig_large_trade({"large_trade_5m": None, "large_trade_count_5m": 5}, "5m"), 0)


class TestWilson95(unittest.TestCase):
    """Wilson 95% CI 计算正确性"""

    def test_wilson95_zero(self):
        lo, hi = wilson95(0, 0)
        self.assertEqual((lo, hi), (0.0, 0.0))

    def test_wilson95_all_correct(self):
        """100% 命中率: lo 应当接近 1, hi=1.0"""
        lo, hi = wilson95(30, 30)
        self.assertGreater(lo, 0.85)
        self.assertEqual(hi, 1.0)

    def test_wilson95_half(self):
        """50% 命中率: lo ≈ 0.32, hi ≈ 0.68 (近似)"""
        lo, hi = wilson95(15, 30)
        self.assertGreater(lo, 0.30)
        self.assertLess(lo, 0.40)
        self.assertGreater(hi, 0.60)
        self.assertLess(hi, 0.70)

    def test_wilson95_small_sample(self):
        """小样本: n=5, 3 正确 = 60%"""
        lo, hi = wilson95(3, 5)
        self.assertGreater(lo, 0.0)
        self.assertLess(hi, 1.0)


class TestRound50BacktestMain(unittest.TestCase):
    """主回测函数 + 渲染 (使用 mock 数据)"""

    def test_backtest_runs_with_no_data(self):
        """空数据时 backtest 不崩, 返回有效结构"""
        # 通过 monkeypatch 替换 load_results / load_snapshots
        from scripts import round50_factor_candidate_backtest as rb
        rb.load_results = lambda: {}
        rb.load_snapshots = lambda: {}
        report = round50_backtest()
        self.assertEqual(report["n_joined"], 0)
        self.assertEqual(len(report["per_field"]), 0)
        # 渲染不崩
        md = render_markdown(report)
        self.assertIn("数据不足", md)
        self.assertIn("当前样本 0", md)

    def test_backtest_with_mock_data(self):
        """3 场 mock 已结算样本 + 全部 UP, 测试回测主路径"""
        from scripts import round50_factor_candidate_backtest as rb
        import tempfile, os
        # mock 3 场全部 UP
        mock_results = {
            "test-slug-1": {"slug": "test-slug-1", "actual": "UP", "pnl": 5.0},
            "test-slug-2": {"slug": "test-slug-2", "actual": "UP", "pnl": 5.0},
            "test-slug-3": {"slug": "test-slug-3", "actual": "UP", "pnl": 5.0},
        }
        # 用临时目录写 mock snapshot, 避免污染 data/snapshots/
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_dir = Path(tmpdir)
            mock_snaps = {}
            for i in range(1, 4):
                path = mock_dir / f"_mock_test_{i}.json"
                snap = {
                    "polymarket": {
                        "trades_15m": {"net_up_imbalance": 0.5, "trade_count": 100},
                        "smart_money_consensus_1h": {"net_up_imbalance": 0.5, "wallets_total": 7, "wallet_hits": 5},
                    },
                    "large_trade_5m": 0.5,
                    "large_trade_count_5m": 3,
                    "large_trade_15m": 0.5,
                    "large_trade_count_15m": 5,
                }
                path.write_text(json.dumps(snap))
                mock_snaps[f"test-slug-{i}"] = path

            # patch load_results + load_snapshots 用我们的 mock 数据
            original_load_results = rb.load_results
            original_load_snaps = rb.load_snapshots
            rb.load_results = lambda: mock_results
            rb.load_snapshots = lambda: mock_snaps

            try:
                report = round50_backtest()
                self.assertEqual(report["n_joined"], 3)
                # 三因子全 +1, actual=UP → 全部命中
                for name, s in report["single_stats"].items():
                    self.assertEqual(s["correct"], 3)
                    self.assertEqual(s["wrong"], 0)
                    self.assertEqual(s["neutral"], 0)

                md = render_markdown(report)
                self.assertIn("100.0%", md)
                self.assertIn("PASS", md)
            finally:
                rb.load_results = original_load_results
                rb.load_snapshots = original_load_snaps


class TestRenderMarkdownEdgeCases(unittest.TestCase):
    """render_markdown edge cases"""

    def test_render_with_zero_eff_n(self):
        """全部 eff_n=0 时, 报告渲染不崩"""
        from scripts import round50_factor_candidate_backtest as rb
        rb.load_results = lambda: {}
        rb.load_snapshots = lambda: {}
        report = round50_backtest()
        md = render_markdown(report)
        # 所有因子都应标 "数据不足"
        self.assertIn("❌ 数据不足", md)
        # 不应崩在 None subscript
        self.assertIsInstance(md, str)


if __name__ == "__main__":
    import json
    print("=" * 60)
    print("round50 回测脚本 mock 测试")
    print("=" * 60)
    unittest.main(verbosity=2)
