"""Hermes signal_validator 最小回归测试（D6/2026-08-23）。

测试纯辅助契约与 validate 的 fail-open 行为，不依赖磁盘市场数据。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import signal_validator as sv  # noqa: E402


class TestValidatorHelpers(unittest.TestCase):
    def test_sign(self):
        self.assertEqual(sv._sign(-1), -1)
        self.assertEqual(sv._sign(0), 0)
        self.assertEqual(sv._sign(3), 1)

    def test_f(self):
        self.assertEqual(sv._f("2.5"), 2.5)
        self.assertEqual(sv._f("abc"), None)
        self.assertEqual(sv._f(None), None)

    def test_norm_conf_invalid(self):
        self.assertIsNone(sv._norm_conf(None))
        self.assertIsNone(sv._norm_conf(1.0))

    def test_scale_rank(self):
        self.assertEqual(sv._scale_rank("min"), 0.0)
        self.assertEqual(sv._scale_rank(0.5), 0.5)
        self.assertEqual(sv._scale_rank(None), 1.0)

    def test_extract_ai_s0(self):
        # 保留符号: S=+3.5 → "+3.5"；负数保留负号
        self.assertEqual(sv._extract_ai_s0("S0=+3.5 净分顺"), "+3.5")
        self.assertEqual(sv._extract_ai_s0("S0=-2.0 ..."), "-2.0")
        self.assertEqual(sv._extract_ai_s0("无说明"), "未知")


class TestValidatorFailOpen(unittest.TestCase):
    def test_validate_skips_when_no_snapshot(self):
        # 伪造 slug → 无同 slug 快照 → fail-open 放行并记录 skipped（不抛错）
        sig = {"slug": "bitcoin-up-or-down-fake-9999",
               "signal": "UP", "side": "UP", "action": "ORDER",
               "source": "btc-agent", "probability": 0.6, "max_price": 0.60}
        out, ec = sv.validate(dict(sig))
        self.assertIn("skipped", ec)
        self.assertEqual(out.get("slug"), sig["slug"])


if __name__ == "__main__":
    unittest.main()