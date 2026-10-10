# -*- coding: utf-8 -*-
"""round54: bot 推迟到整点后 :40 才分析下单（9/3 用户拍板）。

动机：此前 bot 收到引擎信号(≈:36)即立刻下单；本次新增 ANALYZE_AT_MIN=40 门槛，
未到 :40 只确认信号存在、不触发首挂，:40 到后下一轮 tick 再处理。

测试用 object.__new__ 绕过 __init__ 重资源，patch 掉 tick() 内依赖。
"""
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import hermes_1h_bot as hb  # noqa: E402


def make_bot():
    b = object.__new__(hb.HermesBot)
    b._processing_slug = None
    b.last_slug = None
    b._window_retries = {}
    b._shutdown = False
    b._retry_backoff = 0
    return b


def _win(et_minute):
    return {
        "slug": "s1", "et_hour": 10, "et_minute": et_minute,
        "et_time": "t", "in_window": True, "window_str": "0-55min",
    }


class TestAnalyzeAt40(unittest.TestCase):

    def _run_tick(self, et_minute, sig, analyze_min=40):
        """round55.2 测试隔离修复：默认 patch _analyze_at_min 固定返回 40——
        此前直接依赖真实 hermes_config.json 的 analyze_min，用户把真实值调离 40 后
        :40 边界用例必挂（20260906 实证：真实值 45 时 test_processes_at_40 失败）。
        analyze_min=None 时不 patch，走真实 config 热读（供 hot_reload 用例）。"""
        b = make_bot()
        gate = (patch.object(hb, "_analyze_at_min", return_value=analyze_min)
                if analyze_min is not None else nullcontext())
        with gate, \
             patch.object(b, "_record_pm_prices"), \
             patch.object(b, "_poll_pending_orders"), \
             patch.object(b, "_take_profit_check"), \
             patch.object(b, "_slug_already_done", return_value=False), \
             patch.object(b, "find_signal_for_slug", return_value=sig), \
             patch.object(b, "_do_tick") as do_tick, \
             patch.object(hb, "get_current_window", return_value=_win(et_minute)):
            b.tick()
        return b, do_tick

    # —— 新门槛三态 ——
    def test_defers_before_40(self):
        """et_minute<40 时即使有信号也延后，不进 _do_tick（不下单）。"""
        b, do_tick = self._run_tick(30, {"side": "UP"})
        do_tick.assert_not_called()
        self.assertIsNone(b._processing_slug)

    def test_processes_at_40(self):
        """et_minute==40 立即处理（:40 一到下一轮 tick）。"""
        b, do_tick = self._run_tick(40, {"side": "UP"})
        do_tick.assert_called_once()

    def test_processes_after_40(self):
        """et_minute>=40 正常处理。"""
        b, do_tick = self._run_tick(50, {"side": "UP"})
        do_tick.assert_called_once()

    # —— 回归：无信号仍等待，不受新门槛影响 ——
    def test_no_signal_still_waits(self):
        b = make_bot()
        with patch.object(b, "_record_pm_prices"), \
             patch.object(b, "_poll_pending_orders"), \
             patch.object(b, "_take_profit_check"), \
             patch.object(b, "_slug_already_done", return_value=False), \
             patch.object(b, "find_signal_for_slug", return_value=None), \
             patch.object(b, "_do_tick") as do_tick, \
             patch.object(hb, "get_current_window", return_value=_win(50)):
            b.tick()
        do_tick.assert_not_called()

    # —— 源码契约：门槛确实落地在 tick（防未来误删） ——
    # round55: 门槛值从常量 ANALYZE_AT_MIN 改为 config analyze_min 热读 _analyze_at_min()
    def test_tick_source_contains_analyze_at_min_gate(self):
        src = __import__("inspect").getsource(hb.HermesBot.tick)
        self.assertIn("_analyze_at_min", src)
        self.assertIn('win["et_minute"]', src)

    # —— round55: config analyze_min 热读生效（UI 可调，无需重启 bot） ——
    def test_analyze_min_hot_reload(self):
        import json
        import os
        import tempfile
        orig_cfg = hb.CONFIG
        tmp = Path(tempfile.gettempdir()) / "_hermes_cfg_test.json"
        try:
            tmp.write_text(json.dumps({"analyze_min": 50}), encoding="utf-8")
            hb.CONFIG = tmp
            hb._analyze_cache["ts"] = 0.0  # 强制刷新缓存
            b, do_tick = self._run_tick(40, {"side": "UP"}, analyze_min=None)
            do_tick.assert_not_called()  # 40 < 50 → 延后（原行为:40 会处理）
            b, do_tick = self._run_tick(50, {"side": "UP"}, analyze_min=None)
            do_tick.assert_called_once()  # 50 == 50 → 处理
        finally:
            tmp.unlink(missing_ok=True)
            hb.CONFIG = orig_cfg
            hb._analyze_cache["ts"] = 0.0


if __name__ == "__main__":
    unittest.main()