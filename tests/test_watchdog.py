# -*- coding: utf-8 -*-
"""bot_watchdog.need_restart 判定逻辑单测（拉起决策 / 防风暴保护期）"""
import importlib.util
from pathlib import Path

_WD_PATH = Path(__file__).resolve().parent.parent / "bot_watchdog.py"


def _load():
    spec = importlib.util.spec_from_file_location("bot_watchdog", _WD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_alive_no_restart():
    """bot 存活 → 不拉起"""
    wd = _load()
    do, why = wd.need_restart(True, 0, 1000)
    assert do is False and why == "alive"


def test_dead_no_history_restart():
    """bot 死亡且从未拉起过 → 立即拉起"""
    wd = _load()
    do, why = wd.need_restart(False, 0, 1000)
    assert do is True and why == "dead"


def test_dead_within_guard_skip():
    """bot 死亡但在保护期内刚拉起过 → 跳过（防风暴）"""
    wd = _load()
    do, why = wd.need_restart(False, 950, 1000, guard_s=600)
    assert do is False and why == "guard"


def test_dead_after_guard_restart():
    """bot 死亡且保护期已过 → 再次拉起"""
    wd = _load()
    do, why = wd.need_restart(False, 950, 1700, guard_s=600)
    assert do is True and why == "dead"


def test_guard_boundary_exact():
    """恰好到保护期边界（now-last==guard_s）→ 允许拉起"""
    wd = _load()
    do, why = wd.need_restart(False, 400, 1000, guard_s=600)
    assert do is True and why == "dead"
