"""Hermes rule_engine 最小回归测试（D6/2026-08-23）。

运行：
    python3 -m unittest discover -s tests -p 'test_*.py'
或装 pytest 后：
    pytest tests/

聚焦稳定契约：G1 数据失效→SKIP、方向符号/四舍五入、强方向→ORDER/对应方向，
不依赖具体因子权重（模块 8/… 的注释已声明权重可变，勿据此断言权重）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import rule_engine as re  # noqa: E402


class TestRuleEngineHelpers(unittest.TestCase):
    def test_sign(self):
        self.assertEqual(re._sign(-2.3), -1)
        self.assertEqual(re._sign(0), 0)
        self.assertEqual(re._sign(1.5), 1)

    def test_round_half_up(self):
        self.assertEqual(re._round_half_up(1.5), 2)
        self.assertEqual(re._round_half_up(2.5), 3)
        self.assertEqual(re._round_half_up(2.4), 2)
        self.assertEqual(re._round_half_up(-1.5), -2)


class TestRuleEngineJudge(unittest.TestCase):
    def _base(self, **kw):
        base = {
            "data_ok": True,
            "pm_up": 80.0,
            "deviation_pct": 0.5,
            "factors": {"deviation": 2.0, "taker_bs": 3.0, "taker_1m": 2.0,
                        "taker_1h": 0, "session": 0, "trend": 0,
                        "orderflow": 0, "thrust": 0, "shape": 0},
            "tie_line_active": False,
            "macd_1h_hist": 0,
            "macd_4h_hist": 0,
        }
        base.update(kw)
        return base

    def test_g1_data_not_ok_skips(self):
        d = re.judge({"data_ok": False, "factors": {}})
        self.assertEqual(d.action, "SKIP")

    def test_strong_up_orders_up(self):
        d = re.judge(self._base())
        self.assertEqual((d.action, d.final_direction), ("ORDER", "UP"))

    def test_strong_down_orders_down(self):
        d = re.judge(self._base(pm_up=20.0, deviation_pct=-0.5))
        self.assertEqual((d.action, d.final_direction), ("ORDER", "DOWN"))


if __name__ == "__main__":
    unittest.main()