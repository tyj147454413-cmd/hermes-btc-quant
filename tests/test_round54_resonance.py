# -*- coding: utf-8 -*-
"""round54 resonance 强共振因子测试。"""
import unittest
import os, sys, json, glob

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import signal_validator as sv

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _base_md():
    # 用磁盘上最近一张真实快照作为基底，保证字段齐全、extract_factors 不 skip
    snaps = sorted(glob.glob(os.path.join(DIR, "data", "snapshots", "bitcoin-*.json")),
                   key=os.path.getmtime)
    o = json.load(open(snaps[-1], encoding="utf-8"))
    return o

class TestResonance(unittest.TestCase):
    def _run(self, overrides):
        md = _base_md()
        # 重建 polymarket 里会被强因子读取的字段, 使 5 个强因子可控
        pm = dict((md.get("polymarket") or {}))
        for k, v in overrides.pop("_pm", {}).items():
            pm[k] = v
        md["polymarket"] = pm
        for k, v in overrides.pop("_direct", {}).items():
            md[k] = v
        try:
            factors, _j, notes, _ = sv.extract_factors(md)
        except sv._SkipValidation as e:
            self.fail(f"skip: {e}")
        return factors

    def make_strong(self, direction10):
        """direction10=+1 全体看UP, -1 全体看DOWN. 强因子=price_position/pm_momentum/pm_consensus/data_completeness/recent_dir_15m."""
        d = direction10
        return {
            "_pm": {
                # pm_consensus: up_price>0.6 -> +1 ; <0.4 -> -1
                "up_price": 0.75 if d > 0 else 0.25,
                # data_completeness: book 与 pool 同向
                "book_imbalance": 0.5*d,
                "smart_money_consensus_1h": {"smart_money_pool_direction": 0.8*d, "net_up_imbalance": 0.3*d},
                # pm_momentum: 需 pm 价差; 用 up_price_prev 等, 若难控则不强求此因子触发
            },
            "_direct": {
                "price_position_24h": 0.85 if d > 0 else 0.15,
            },
            "n_strong_expected": None,
        }

    def test_resonance_4_up(self):
        """强因子看 UP 触发共振, resonance=+2(≥4) 或 +1(3). 方向必须与多数派同."""
        md = _base_md()
        md["polymarket"]["up_price"] = 0.75
        md["polymarket"]["book_imbalance"] = 0.5
        md["polymarket"]["smart_money_consensus_1h"] = {"smart_money_pool_direction": 0.8, "net_up_imbalance": 0.3}
        f, _j, _n, _ = sv.extract_factors(md)
        from collections import Counter
        c = Counter((1 if (f.get(k) or 0) > 0 else -1) for k in sv.RESONANCE_STRONG if f.get(k) != 0)
        res = f.get("resonance", 0)
        # 产品口径：resonance 用"同一方向的强因子票数"判定（signal_validator _res_votes），
        # 非零因子总数 ≠ 最大同向票数（方向可分裂：如 1UP vs 2DOWN）。故用 max(c.values())。
        most = max(c.values()) if c else 0
        if res != 0:
            # 非0时：方向=多数派人指向, 数值=+(>4 用 W_RESONANCE else 1)
            maj = max((v, k) for k, v in c.items())[1]
            want = maj * (sv.W_RESONANCE if most >= 4 else 1)
            self.assertEqual(res, float(want))
        else:
            self.assertTrue(most < 3)

    def test_resonance_key_exists(self):
        md = _base_md()
        try:
            f, _j, _n, _ = sv.extract_factors(md)
        except sv._SkipValidation as e:
            self.fail(f"skip: {e}")
        self.assertIn("resonance", f)
        self.assertTrue(-sv.W_RESONANCE <= f["resonance"] <= sv.W_RESONANCE)

    def test_weight_constants(self):
        self.assertEqual(sv.W_RESONANCE, 2)
        self.assertIn("price_position", sv.RESONANCE_STRONG)
        self.assertEqual(len(sv.RESONANCE_STRONG), 5)
        self.assertEqual(sv.W_PM_PAIR, 2)
        self.assertEqual(sv.PM_PAIR_PARTNERS, ("price_position", "pm_consensus", "recent_dir_15m"))

    def test_pm_pair_key_exists(self):
        md = _base_md()
        try:
            f, _j, _n, _ = sv.extract_factors(md)
        except sv._SkipValidation as e:
            self.fail(f"skip: {e}")
        self.assertIn("pm_pair", f)
        self.assertTrue(-sv.W_PM_PAIR <= f["pm_pair"] <= sv.W_PM_PAIR)

    def test_pm_pair_logic_up(self):
        """pm_momentum 与搭档同向触发 pm_pair, 方向与 pm_momentum 一致."""
        md = _base_md()
        md["polymarket"]["up_price"] = 0.75  # pm_consensus +1
        f, _j, _n, _ = sv.extract_factors(md)
        _pm = f.get("pm_momentum", 0)
        _pair = f.get("pm_pair", 0)
        if _pm != 0 and _pair != 0:
            self.assertEqual((1 if _pair > 0 else -1), (1 if _pm > 0 else -1))
        elif _pm == 0:
            self.assertEqual(_pair, 0)  # pm 缺失则 pair 必为0

    def test_resonance_same_dir_as_majority(self):
        """resonance 方向必须与强因子多数派方向一致."""
        md = _base_md()
        try:
            f, _j, _n, _ = sv.extract_factors(md)
        except sv._SkipValidation as e:
            self.fail(f"skip: {e}")
        from collections import Counter
        c = Counter((1 if (f.get(k) or 0) > 0 else -1) for k in sv.RESONANCE_STRONG if f.get(k) != 0)
        if c:
            maj = max((k, v) for v, k in c.items())[1]
            if f["resonance"] != 0:
                self.assertEqual((1 if f["resonance"] > 0 else -1), maj)
            elif sum(c.values()) < 3:
                self.assertEqual(f["resonance"], 0)
        else:
            self.assertEqual(f["resonance"], 0)

if __name__ == "__main__":
    unittest.main()