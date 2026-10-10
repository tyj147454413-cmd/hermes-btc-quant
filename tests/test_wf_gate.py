"""C3 WF 关卡最小回归测试（2026-08-23）。

覆盖纯逻辑：verdict 判定边界、报告编号格式、索引读写锁、gate_status_for_major
（无报告/命中/文件丢失）、check_entry（md5 校验/require-pass）。全部隔离临时目录。
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import wf_gate as wg  # noqa: E402


class TestVerdict(unittest.TestCase):
    def test_pass(self):
        # n≥30 且 wilson 下界>50 且 ev>0
        self.assertEqual(wg.verdict(40, 50.1, 0.01), "PASS")

    def test_fail_on_low_lower_bound(self):
        self.assertEqual(wg.verdict(40, 49.9, 0.05), "CONDITIONAL")  # n≥30 但下界≤50

    def test_conditional_small_n_positive_ev(self):
        self.assertEqual(wg.verdict(20, 45.0, 0.02), "CONDITIONAL")

    def test_fail_tiny_sample(self):
        self.assertEqual(wg.verdict(10, 60.0, 0.10), "FAIL")

    def test_fail_negative_ev(self):
        self.assertEqual(wg.verdict(100, 55.0, -0.01), "FAIL")


class TestReportIdAndMajor(unittest.TestCase):
    def test_id_format(self):
        ts = datetime(2026, 8, 23, 12, 34, tzinfo=timezone.utc)
        rid = wg.make_report_id(ts, "seed")
        self.assertRegex(rid, r"^WF-20260823-1234-[0-9a-f]{4}$")

    def test_current_major_from_temp_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "prompt_exec.md"
            p.write_text("其他行 round99 不应命中\n**版本锚点** 当前=round41.4（说明）\n",
                         encoding="utf-8")
            self.assertEqual(wg.current_major(p), "41")

    def test_current_major_missing_file(self):
        self.assertIsNone(wg.current_major(Path("/nonexistent/prompt.md")))


class TestIndex(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.idx = self.tmp / "index.jsonl"

    def test_load_empty(self):
        self.assertEqual(wg.load_index(self.idx), [])

    def test_append_and_load_roundtrip(self):
        wg.append_index({"id": "WF-A", "for_major": "41"}, path=self.idx)
        wg.append_index({"id": "WF-B", "for_major": "42"}, path=self.idx)
        entries = wg.load_index(self.idx)
        self.assertEqual([e["id"] for e in entries], ["WF-A", "WF-B"])
        # 坏行不致命
        with open(self.idx, "a", encoding="utf-8") as f:
            f.write("{broken json\n")
        self.assertEqual(len(wg.load_index(self.idx)), 2)


class TestGateStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.idx = self.tmp / "index.jsonl"
        self.prompt = self.tmp / "prompt_exec.md"
        self.prompt.write_text("**版本锚点** 当前=round42.0\n", encoding="utf-8")
        self.report_dir = self.tmp / "reports"
        self.report_dir.mkdir()

    def test_no_reports(self):
        ok, detail = wg.gate_status_for_major("42", self.idx, self.prompt)
        self.assertFalse(ok)
        self.assertIn("无任何 WF 报告", detail)

    def test_major_uncovered(self):
        wg.append_index({"id": "WF-X", "for_major": "41"}, path=self.idx)
        ok, detail = wg.gate_status_for_major("42", self.idx, self.prompt)
        self.assertFalse(ok)
        self.assertIn("主版本42无WF报告", detail.replace(" ", ""))

    def test_covered_but_file_lost(self):
        wg.append_index({"id": "WF-Y", "for_major": "42"}, path=self.idx)
        with patch.object(wg, "REPORT_DIR", self.report_dir):
            ok, detail = wg.gate_status_for_major("42", self.idx, self.prompt)
        self.assertFalse(ok)
        self.assertIn("丢失", detail)

    def test_covered_ok(self):
        wg.append_index({"id": "WF-Z", "for_major": "42"}, path=self.idx)
        (self.report_dir / "WF-Z.md").write_text("# fake report", encoding="utf-8")
        with patch.object(wg, "REPORT_DIR", self.report_dir):
            ok, detail = wg.gate_status_for_major(None, self.idx, self.prompt)  # major 从 prompt 解析
        self.assertTrue(ok)
        self.assertIn("已有 1 份WF报告", detail)


class TestCheckEntry(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.rdir = self.tmp / "reports"
        self.rdir.mkdir()
        self.entry = {
            "id": "WF-T1", "for_major": "41",
            "candidates": {"production_engine": {"oos_n": 35, "oos_wr": 58.0,
                                                 "oos_ev": 0.03, "verdict": "PASS"}},
            "md5": None,
        }

    def _write_report(self, content="# report"):
        p = self.rdir / "WF-T1.md"
        p.write_text(content, encoding="utf-8")
        self.entry["md5"] = wg.file_md5(p)

    def test_missing_file(self):
        with patch.object(wg, "REPORT_DIR", self.rdir):
            ok, msg = wg.check_entry(self.entry)
        self.assertFalse(ok)
        self.assertIn("缺失", msg)

    def test_md5_mismatch(self):
        self._write_report("# v1")
        self.entry["md5"] = "deadbeefcafe"
        with patch.object(wg, "REPORT_DIR", self.rdir):
            ok, msg = wg.check_entry(self.entry)
        self.assertFalse(ok)
        self.assertIn("md5 不一致", msg)

    def test_ok_with_require_pass(self):
        self._write_report()
        with patch.object(wg, "REPORT_DIR", self.rdir):
            ok, msg = wg.check_entry(self.entry, require_pass="production_engine")
        self.assertTrue(ok)
        self.assertIn("require-pass[production_engine]=PASS", msg)

    def test_require_pass_not_met(self):
        self._write_report()
        self.entry["candidates"]["production_engine"]["verdict"] = "CONDITIONAL"
        with patch.object(wg, "REPORT_DIR", self.rdir):
            ok, msg = wg.check_entry(self.entry, require_pass="production_engine")
        self.assertFalse(ok)
        self.assertIn("未达 PASS", msg)


if __name__ == "__main__":
    unittest.main()
