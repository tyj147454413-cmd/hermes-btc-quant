# -*- coding: utf-8 -*-
"""round55: dashboard /api/config 支持 analyze_min（bot 分析时刻热读，UI 可调）。

覆盖后端 API 校验：合法整数通过、bool 拒绝（bool 是 int 子类，true→1 静默写入是已知坑）、
越界/非法拒绝、config 无 analyze_min 时 GET 返回默认 40。
用 Flask test_client 直测，patch CONFIG_FILE 指向临时文件，避免动真实配置。
"""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import dashboard as dash  # noqa: E402


class TestDashboardAnalyzeMinAPI(unittest.TestCase):

    def setUp(self):
        self._tmp = TemporaryDirectory()
        tmp_cfg = Path(self._tmp.name) / "hermes_config_test.json"
        # 无 analyze_min 的存量配置（验证 defaults merge）
        tmp_cfg.write_text(json.dumps({"bet_amount": 2.0, "mode": "paper"}), encoding="utf-8")
        self._tmp_cfg = tmp_cfg
        self._patches = [
            patch.object(dash, "CONFIG_FILE", tmp_cfg),
            patch.object(dash, "DASH_PASS", ""),  # 本地无密码，require_auth 放行
            patch.object(dash, "_sync_engine_schedule", return_value=(True, "")),  # round55.1: 任务联动默认成功
            patch.object(dash, "_verify_engine_schedule", return_value=(True, "")),  # 复核默认通过
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self._sync_mock = dash._sync_engine_schedule
        self.client = dash.app.test_client()

    def _post(self, payload):
        return self.client.post("/api/config", json=payload)

    # —— 合法值 ——
    def test_accepts_valid_analyze_min(self):
        r = self._post({"analyze_min": 50})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["analyze_min"], 50)

    def test_accepts_analyze_min_zero(self):
        r = self._post({"analyze_min": 0})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])

    # —— round55.1: 信号时刻联动 ——
    def test_analyze_min_syncs_engine_task(self):
        """合法值写入时同步调用 _sync_engine_schedule，分钟位与 analyze_min 一致。"""
        self._post({"analyze_min": 25})
        self._sync_mock.assert_called_once()
        self.assertEqual(self._sync_mock.call_args[0][0], 25)

    def test_engine_sync_failure_blocks_write(self):
        """引擎任务联动失败（如权限拒绝）→ 500 且 config 不写入，避免 UI 假生效。"""
        with patch.object(dash, "save_config", wraps=dash.save_config) as sc, \
             patch.object(dash, "_sync_engine_schedule", return_value=(False, "denied")):
            r = self._post({"analyze_min": 30})
        self.assertEqual(r.status_code, 500)
        self.assertFalse(r.get_json()["ok"])
        sc.assert_not_called()  # config 未被写回，与旧任务时刻保持一致的停止状态

    def test_engine_verify_failure_blocks_write(self):
        """联动已执行但复核不过（Next Run 分钟位与目标不符）→ 500 且不写 config。"""
        with patch.object(dash, "save_config", wraps=dash.save_config) as sc, \
             patch.object(dash, "_verify_engine_schedule", return_value=(False, "minute mismatch")):
            r = self._post({"analyze_min": 30})
        self.assertEqual(r.status_code, 500)
        self.assertFalse(r.get_json()["ok"])
        sc.assert_not_called()

    # —— bool 注入必须拒绝（true 是 int 子类，int(True)=1 会静默改分析时刻） ——
    def test_rejects_bool_true(self):
        r = self._post({"analyze_min": True})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])

    def test_rejects_bool_false(self):
        r = self._post({"analyze_min": False})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])

    # —— 越界 ——
    def test_rejects_out_of_range_high(self):
        r = self._post({"analyze_min": 56})
        self.assertEqual(r.status_code, 400)

    def test_rejects_out_of_range_low(self):
        r = self._post({"analyze_min": -1})
        self.assertEqual(r.status_code, 400)

    # —— 非法类型 ——
    def test_rejects_non_numeric(self):
        r = self._post({"analyze_min": "abc"})
        self.assertEqual(r.status_code, 400)

    def test_rejects_null(self):
        r = self._post({"analyze_min": None})
        self.assertEqual(r.status_code, 400)

    # —— GET 默认值：存量 config 无 analyze_min 时返回默认 40 ——
    def test_get_returns_default_40(self):
        r = self.client.get("/api/config")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertIn("analyze_min", d)
        self.assertEqual(d["analyze_min"], 40)


if __name__ == "__main__":
    unittest.main()