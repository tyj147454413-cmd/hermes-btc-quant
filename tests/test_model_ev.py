# -*- coding: utf-8 -*-
"""模型 EV 信号公式测试：盈亏平衡买价 = 校准胜率。
买涨 EV = p*(1-pm_up) - (1-p)*pm_up；买跌 EV = (1-p)*(1-pm_down) - p*pm_down。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "model"))

from predict_hourly import compute_ev  # noqa: E402


def test_fair_price_equals_win_rate():
    fair_up, fair_down, _, _ = compute_ev(0.63, None, None)
    assert fair_up == 0.63
    assert fair_down == 0.37


def test_ev_up_positive_when_price_below_fair():
    _, _, ev_up, _ = compute_ev(0.63, 0.50, None)
    assert ev_up == 0.13


def test_ev_up_negative_when_price_above_fair():
    _, _, ev_up, _ = compute_ev(0.63, 0.72, None)
    assert ev_up == -0.09


def test_ev_down_positive():
    _, _, _, ev_down = compute_ev(0.14, None, 0.51)
    assert ev_down == 0.35


def test_breakeven_when_price_equals_p():
    _, _, ev_up, _ = compute_ev(0.63, 0.63, None)
    assert ev_up == 0.0


def test_missing_prices_yields_none_ev():
    _, _, ev_up, ev_down = compute_ev(0.8, None, None)
    assert ev_up is None
    assert ev_down is None
