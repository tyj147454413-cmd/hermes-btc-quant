"""盘感代理特征 fetch_pm_feats 单测（9/4 盘感进模型试点）。

场景矩阵：
  1. 正常两行（:10/:30）→ pm_up_dec=最新, pm_delta=演变
  2. 单行 → delta=0（无演变信号）
  3. 只有决策点之后的行（dm40 请求，:45 才有）→ None（防未来泄漏）
  4. 文件缺失 → None
  5. 坏 JSONL 行 / pm_up 为空 → 跳过不污染
  6. slug 不匹配 → None
"""
import json

import pytest

from hermes_common import fetch_pm_feats

SLUG = "bitcoin-up-or-down-september-3-2026-8pm-et"


def _write(tmp_path, rows):
    p = tmp_path / "shadow.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                 encoding="utf-8")
    return p


def _rec(decision_min, pm_up, slug=SLUG):
    return {"slug": slug, "decision_min": decision_min, "pm_up": pm_up,
            "pm_down": None if pm_up is None else round(1 - pm_up, 4), "hours_ago": 0}


def test_normal_two_points(tmp_path):
    p = _write(tmp_path, [_rec(10, 0.40), _rec(30, 0.60)])
    out = fetch_pm_feats(SLUG, 35, shadow_path=p)
    assert out is not None
    assert out["pm_up_dec"] == 0.60
    assert out["pm_delta"] == pytest.approx(0.20)


def test_single_point_delta_zero(tmp_path):
    p = _write(tmp_path, [_rec(40, 0.55)])
    out = fetch_pm_feats(SLUG, 40, shadow_path=p)
    assert out["pm_up_dec"] == 0.55
    assert out["pm_delta"] == 0.0


def test_future_point_excluded(tmp_path):
    # dm40 请求时只有 :45 的决策点（未来不可见）→ None
    p = _write(tmp_path, [_rec(45, 0.70)])
    assert fetch_pm_feats(SLUG, 40, shadow_path=p) is None


def test_missing_file(tmp_path):
    assert fetch_pm_feats(SLUG, 40, shadow_path=tmp_path / "nope.jsonl") is None


def test_bad_lines_and_null_pm_skipped(tmp_path):
    p = tmp_path / "shadow.jsonl"
    good = _rec(30, 0.50)
    p.write_text("{corrupt json}\n" + json.dumps(_rec(10, None)) + "\n"
                 + json.dumps(good) + "\n", encoding="utf-8")
    out = fetch_pm_feats(SLUG, 35, shadow_path=p)
    assert out["pm_up_dec"] == 0.50
    assert out["pm_delta"] == 0.0


def test_wrong_slug_ignored(tmp_path):
    p = _write(tmp_path, [_rec(30, 0.60, slug="bitcoin-up-or-down-september-2-2026-8pm-et")])
    assert fetch_pm_feats(SLUG, 35, shadow_path=p) is None


def test_dm_boundary_inclusive(tmp_path):
    # 边界：dm=30 请求时 :30 本身可见
    p = _write(tmp_path, [_rec(10, 0.40), _rec(30, 0.55)])
    out = fetch_pm_feats(SLUG, 30, shadow_path=p)
    assert out["pm_delta"] == pytest.approx(0.15)