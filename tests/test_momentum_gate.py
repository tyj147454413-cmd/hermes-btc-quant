"""盘感覆盖层门控逻辑单测：momentum_conflict 冲突矩阵 + 开关默认行为。"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.mark.parametrize("flag,side,expected", [
    # 盘口看涨动能 vs 信号看跌 → 冲突（SKIP）
    ("DUMP_TRAP", "DOWN", True),
    ("ASK_SURGE", "DOWN", True),
    # UP 盘口走弱 vs 信号看涨 → 冲突（SKIP）
    ("ASK_FADE", "UP", True),
    # 同向/持平 → 不冲突（放行）
    ("ASK_SURGE", "UP", False),
    ("DUMP_TRAP", "UP", False),
    ("ASK_FADE", "DOWN", False),
    ("FLAT", "UP", False),
    ("FLAT", "DOWN", False),
    # 边界：大小写、非法 side、None
    ("ask_surge", "down", True),   # flag 大写化处理？当前实现按原样比较 → 直接穿回 False
    ("ASK_SURGE", "down", True),   # side 自动 upper
    ("ASK_SURGE", None, False),
    (None, "DOWN", False),
])
def test_momentum_conflict_matrix(flag, side, expected):
    from hermes_1h_bot import momentum_conflict
    assert momentum_conflict(flag, side) is expected


def test_switch_default_is_off():
    """momentum_skip_enabled 默认关闭（live 安全），避免未拍板就动真实链路。"""
    from hermes_1h_bot import HermesBot
    cfg = HermesBot._load_risk_cfg()
    assert cfg["momentum_skip_enabled"] is False