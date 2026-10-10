# -*- coding: utf-8 -*-
"""round54 data_completeness 聪明订单因子回归测试。

构造最小 market_data 骨架，仅覆盖 extract_factors 中与 data_completeness
相关的判定路径（同向按方向±2/矛盾0/缺字段0/任一为0计0，round54 修正1+2 口径）；
其余字段以 0 兜底，以保证 extract_factors 能走到因子的代码分支而不抛异常。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import signal_validator as sv  # noqa: E402


def _base_md():
    """构造能通过 extract_factors 前置校验的最小 market_data。

    需要 anchor_price / current_price / collect_time_utc 存在（否则 _SkipValidation），
    并填 PM 关键字段避免其它因子对 None 不敏感; 本测试只断言 data_completeness。
    """
    return {
        "slug": "bitcoin-up-or-down-september-3-2026-4am-et",
        "anchor_price": 100.0,
        "current_price": 100.0,
        "timestamp": "2026-09-03T08:35:00Z",
        "collect_time_utc": "2026-09-03T08:35:00Z",
        "polymarket": {
            "book_up_mid": 0.55,
            "up_price": 55.0,
            "up_price_delta": 0.05,
            "book_imbalance": None,
            "smart_money_consensus_1h": {"smart_money_pool_direction": None},
        },
        "timeframes": {
            "1m": {}, "5m": {}, "15m": {},
            "1H": {"macd_hist": 0.0}, "4H": {"macd_hist": 0.0},
        },
        "volatility_5m": 0.1,
        "deviation_trend": {},
        "prev_hour": {},
        "historical": {},
    }


class TestDataCompletenessFactor(unittest.TestCase):
    def _factor(self, book_imb, sm_dir, net_up=None):
        md = _base_md()
        md["polymarket"]["book_imbalance"] = book_imb
        md["polymarket"]["smart_money_consensus_1h"] = {
            "smart_money_pool_direction": sm_dir,
            "net_up_imbalance": net_up,
        }
        try:
            factors, _judge, _notes, _mode = sv.extract_factors(md)
        except sv._SkipValidation as e:  # noqa: F841
            self.fail(f"extract_factors 意外触发 _SkipValidation: {e}")
        return factors.get("data_completeness", 0.0)

    # ---- 两票一致(book+pool同向), net 缺失/中立 ---
    def test_both_same_up(self):
        # book_imb>0 + pool>0 同向偏多(UP) → +2（net 缺失不惩罚）
        self.assertEqual(self._factor(0.5, 0.8), 2.0)

    def test_both_same_down(self):
        # book_imb<0 + pool<0 同向偏空(DOWN) → -2（net 缺失不惩罚）
        self.assertEqual(self._factor(-0.5, -0.8), -2.0)

    # ---- 两票矛盾(book 与 pool) ---
    def test_conflict(self):
        # book_imb>0 + pool<0 矛盾 → 0
        self.assertEqual(self._factor(0.5, -0.8), 0.0)

    def test_conflict_down_up(self):
        # book_imb<0 + pool>0 矛盾 → 0
        self.assertEqual(self._factor(-0.5, 0.8), 0.0)

    # ---- book+pool+net 同向（net 已不参与判定，仅确认不干扰） ---
    def test_three_up(self):
        # book+pool 看UP，net 同向 → +2
        self.assertEqual(self._factor(0.5, 0.8, 0.3), 2.0)

    def test_three_down(self):
        # book+pool 看DOWN，net 同向（含负值，实际采集口径不存在） → -2
        self.assertEqual(self._factor(-0.5, -0.8, -0.3), -2.0)

    # ---- net 已摘除证伪链（round54 修正3：恒正字段单边杀 DOWN 票） ---
    def test_net_falsify_down(self):
        # 9/3 6am 场: book=-0.498 pool=-1(看DOWN) 同向；net 恒正(幸存者偏差)不再证伪 → -2
        self.assertEqual(self._factor(-0.498, -1.0, 1.0), -2.0)

    def test_net_falsify_up(self):
        # book+pool 看UP 同向；net 反向值也不干扰（net 已不参与判定） → +2
        self.assertEqual(self._factor(0.5, 0.8, -0.9), 2.0)

    # ---- net 中立(0)不干扰 ---
    def test_net_zero_noise(self):
        # net==0 中立, 不证伪 → 仍按 book+pool 同向给分
        self.assertEqual(self._factor(-0.5, -0.8, 0.0), -2.0)

    # ---- 缺失场景 ---
    def test_missing_book(self):
        # book_imbalance 缺失 → 0
        self.assertEqual(self._factor(None, 0.8), 0.0)

    def test_missing_sm(self):
        # smart_money_pool_direction 缺失 → 0
        self.assertEqual(self._factor(0.5, None), 0.0)

    def test_zero_book(self):
        # book_imbalance == 0 → 判 0（避免把零当方向）
        self.assertEqual(self._factor(0.0, 0.8), 0.0)

    def test_zero_sm(self):
        # smart_money_pool_direction == 0 → 判 0
        self.assertEqual(self._factor(0.5, 0.0), 0.0)

    def test_constant_weight_is_2(self):
        self.assertEqual(sv.W_DATA_COMPLETENESS, 2)


if __name__ == "__main__":
    unittest.main()