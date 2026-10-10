"""C4/C5 研究工具统计层最小回归（2026-08-23）。

覆盖：二项双侧精确 p、McNemar、置换检验确定性、判定口径、阈值网格的
源码替换模块与敏感性分类。全部纯函数/临时目录，不触网。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import benchmark_significance as bs  # noqa: E402
import threshold_grid as tg  # noqa: E402


class TestBinomial(unittest.TestCase):
    def test_zero_discordant(self):
        self.assertEqual(bs.binom_two_sided_p(0, 0), 1.0)
        self.assertEqual(bs.mcnemar_exact(0, 0), 1.0)

    def test_extreme_asymmetry(self):
        # 10 场不一致里 9:1 → p 应小；0:10 → 最小值 ≈ 2*(0.5^10)≈0.00195
        p = bs.mcnemar_exact(0, 10)
        self.assertLess(p, 0.005)
        self.assertAlmostEqual(p, 2 * (0.5 ** 10), places=4)

    def test_known_value_8_2(self):
        # 点概率法双侧：pmf(2)=pmf(8) 对称 → 尾部 = (1+10+45)*2 / 1024 = 112/1024
        self.assertAlmostEqual(bs.mcnemar_exact(8, 2), 112 / 1024, places=6)


class TestPermutation(unittest.TestCase):
    def test_deterministic(self):
        a = bs.permutation_p(8, 2, n_perm=2000, seed=41)
        b = bs.permutation_p(8, 2, n_perm=2000, seed=41)
        self.assertEqual(a, b)

    def test_balanced_is_one(self):
        self.assertEqual(bs.permutation_p(5, 5, n_perm=100), 1.0)

    def test_strong_advantage_small_p(self):
        self.assertLess(bs.permutation_p(19, 1, n_perm=5000, seed=7), 0.001)


class TestVerdict(unittest.TestCase):
    def test_alpha_confirmed(self):
        self.assertEqual(bs.verdict_of(0.01, 0.02, regime_ok=True), "✅alpha成立")

    def test_regime_inconsistency_downgrades(self):
        self.assertEqual(bs.verdict_of(0.01, 0.02, regime_ok=False),
                         "🟡证据不足(regime间不一致)")

    def test_not_significant(self):
        self.assertEqual(bs.verdict_of(0.17, 0.18, regime_ok=True),
                         "❌beta嫌疑(与运气不可区分)")

    def test_borderline(self):
        self.assertEqual(bs.verdict_of(0.06, 0.07, regime_ok=True), "🟡证据不足(边缘)")


class TestPatchedEngineAndClassify(unittest.TestCase):
    def test_const_patch_changes_behavior(self):
        base = tg.make_patched_engine(None)
        patched = tg.make_patched_engine([("REV_PRICE_MIN = 0.23", "REV_PRICE_MIN = 0.35", 1)])
        inp = {"pm_up": 24.5}  # UP 价 0.245：现行门槛放行，收紧到 0.35 拦截
        self.assertFalse(base._price_blocked(inp, "UP"))
        self.assertTrue(patched._price_blocked(inp, "UP"))

    def test_bad_patch_count_raises(self):
        with self.assertRaises(RuntimeError):
            tg.make_patched_engine([("REV_PRICE_MIN = 0.23", "REV_PRICE_MIN = 0.3", 2)])

    def test_classify_spike(self):
        # 基准比最好相邻点高 ≥5pp 且 n≥30 → 尖峰
        v = tg.classify(base_wr=66.0, neighbor_wrs=[58.0, 60.0], flips=3, n=86)
        self.assertTrue(v.startswith("🚨"))

    def test_classify_robust(self):
        v = tg.classify(base_wr=61.6, neighbor_wrs=[61.0, 62.0], flips=2, n=86)
        self.assertTrue(v.startswith("✅"))

    def test_classify_zero_flip(self):
        self.assertTrue(tg.classify(61.6, [50.0], flips=0, n=86).startswith("⚪"))


if __name__ == "__main__":
    unittest.main()
