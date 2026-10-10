"""Hermes write_signal 最小回归测试（D6/2026-08-23）。

隔离在临时目录测试：同 slug 去重只留最新、不同 slug 追加、D4 锁文件创建。
不触碰真实数据/claude_signal.jsonl。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import write_signal as ws  # noqa: E402


def _sig(slug: str, side: str = "UP", prob: float = 0.6):
    return {"slug": slug, "signal": side, "side": side, "action": "ORDER",
            "source": "btc-agent", "probability": prob, "max_price": 0.60}


class TestWriteSignal(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._sig_path, self._ana_path, self._pred_path = ws.SIGNAL, ws.ANALYSIS, ws.PREDICTION
        ws.SIGNAL = self.tmp / "claude_signal.jsonl"
        ws.ANALYSIS = self.tmp / "hermes_analysis.json"
        ws.PREDICTION = self.tmp / "hermes_hourly_prediction.json"

    def tearDown(self):
        ws.SIGNAL, ws.ANALYSIS, ws.PREDICTION = self._sig_path, self._ana_path, self._pred_path

    def _rows(self):
        return [json.loads(l) for l in ws.SIGNAL.read_text(encoding="utf-8").splitlines() if l.strip()]

    def test_append_diff_then_dedup_same_slug(self):
        r1 = ws.write_signal(_sig("bitcoin-test-a", "UP"))
        r2 = ws.write_signal(_sig("bitcoin-test-a", "DOWN", 0.4))
        r3 = ws.write_signal(_sig("bitcoin-test-b", "UP"))
        self.assertTrue(all(x["ok"] for x in (r1, r2, r3)))
        rows = self._rows()
        self.assertEqual([x["slug"] for x in rows], ["bitcoin-test-a", "bitcoin-test-b"])
        a = [x for x in rows if x["slug"] == "bitcoin-test-a"]
        self.assertEqual(len(a), 1, "同 slug 应只保留一条")
        self.assertEqual(a[0]["side"], "DOWN", "同 slug 应留最新一条")

    def test_lock_file_created_and_symmetric(self):
        ws.write_signal(_sig("bitcoin-test-lock"))
        self.assertTrue((self.tmp / "claude_signal.lock").exists())

    def test_write_produces_wellformed_jsonl(self):
        ws.write_signal(_sig("bitcoin-test-json"))
        for row in self._rows():
            self.assertIn("slug", row)

    def test_invalid_side_rejected_no_default(self):
        """P1-5(9/3): side=None/非法值/双缺省一律拒绝——禁止默认 UP 编造方向;
        合法但带空格/小写的值应规范化后写入"""
        with self.assertRaises(ValueError):
            ws.write_signal({"slug": "s-none", "side": None, "action": "ORDER"})
        with self.assertRaises(ValueError):
            ws.write_signal({"slug": "s-dirty", "side": "LONG", "action": "ORDER"})
        with self.assertRaises(ValueError):
            ws.write_signal({"slug": "s-both", "action": "ORDER"})  # side/signal 双缺省
        with self.assertRaises(ValueError):
            ws.write_signal({"slug": "s-empty", "side": "", "action": "ORDER"})
        self.assertFalse((self.tmp / "claude_signal.jsonl").exists(),
                         "非法 side 不得落盘任何信号")
        # 规范化: 带空格/小写 → strip+upper 后正常写入
        r = ws.write_signal({"slug": "s-norm", "side": " up ", "action": "ORDER",
                             "source": "btc-agent", "probability": 0.6, "max_price": 0.6})
        self.assertTrue(r["ok"])
        row = self._rows()[-1]
        self.assertEqual(row["side"], "UP")
        self.assertEqual(row["signal"], "UP")


if __name__ == "__main__":
    unittest.main()