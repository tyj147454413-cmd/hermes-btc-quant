# -*- coding: utf-8 -*-
"""9/3 结算抢跑错账回归测试（1am 场 actual=DOWN 误判根因）。

根因：get_actual_result 用 `>0.5` 残价阈值判结算，收盘后 2 分钟读到 GAMMA
收盘前残价（DOWN=0.545）即误判 DOWN 赢，实际几分钟后市场 closed 归 [1,0]=UP 赢。

修复纪要：必须等官方 closed=True 或一侧已归 1(>0.9) 才判结算。
"""
import unittest
from unittest import mock

import hermes_common as hc
import hermes_review as hr


def _mk_market(closed, up, down, end_date="2026-09-03T06:00:00Z"):
    return {"closed": closed, "endDate": end_date,
            "outcomePrices": f'["{up}", "{down}"]'}


class TestGetActualResultSettleGuard(unittest.TestCase):
    def _patch_gamma(self, market):
        p = mock.patch.object(hc, "http_get_json", return_value=market)
        p.start()
        self.addCleanup(p.stop)

    def test_not_closed_mid_price_returns_none(self):
        """1am 错账场景：closed=False 且残价 DOWN=0.545，必须不判结算。"""
        self._patch_gamma(_mk_market(False, "0.455", "0.545"))
        side, desc = hr.get_actual_result(
            "bitcoin-up-or-down-september-3-2026-1am-et", None, "")
        self.assertIsNone(side)
        self.assertIn("未关闭", desc)

    def test_closed_up_returns_up(self):
        self._patch_gamma(_mk_market(True, "1", "0"))
        side, _ = hr.get_actual_result(
            "bitcoin-up-or-down-september-3-2026-1am-et", None, "")
        self.assertEqual(side, "UP")

    def test_closed_down_returns_down(self):
        self._patch_gamma(_mk_market(True, "0", "1"))
        side, _ = hr.get_actual_result(
            "bitcoin-up-or-down-september-3-2026-1am-et", None, "")
        self.assertEqual(side, "DOWN")

    def test_not_closed_but_resolved_up_returns_up(self):
        """老合约 closed 不显式标记，但一侧已归 1(>0.9) 仍可判。"""
        self._patch_gamma(_mk_market(False, "0.9995", "0.0005"))
        side, _ = hr.get_actual_result(
            "bitcoin-up-or-down-september-3-2026-1am-et", None, "")
        self.assertEqual(side, "UP")


if __name__ == "__main__":
    unittest.main()