"""fetch_book_momentum 旁路盘感逻辑单测（含诱空拉涨剧本判定）。"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "model"))

from predict_hourly import fetch_book_momentum  # noqa: E402

SLUG = "bitcoin-up-or-down-september-4-2026-6am-et"


def _snap(minutes_in_hour: int, up_ask: float, up_bid: float,
          down_ask: float, down_bid: float,
          down_bid_sz: float = 10.0) -> dict:
    sz = 20.0
    return {
        "ts_utc": "2026-09-04T10:00:00+00:00",
        "minutes_in_hour": minutes_in_hour,
        "slug": SLUG,
        "up": {"bids": [[up_bid, sz]], "asks": [[up_ask, sz]]},
        "down": {"bids": [[down_bid, down_bid_sz]], "asks": [[down_ask, sz]]},
    }


def _write(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "book_snapshots.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def test_dump_trap_detected(tmp_path: Path):
    """DOWN bid 巨额挂单(900手) + UP ask 快速上移 0.37→0.58 → DUMP_TRAP"""
    snaps = [
        _snap(30, 0.37, 0.36, 0.63, 0.62, down_bid_sz=10.0),
        _snap(32, 0.45, 0.44, 0.55, 0.54, down_bid_sz=500.0),
        _snap(34, 0.58, 0.57, 0.43, 0.42, down_bid_sz=900.0),
    ]
    p = _write(tmp_path, snaps)
    m = fetch_book_momentum(SLUG, 35, snap_path=p)
    assert m is not None
    assert m["flag"] == "DUMP_TRAP"
    assert m["samples"] == 3
    assert m["up_ask_delta"] == pytest.approx(0.21)
    assert m["max_down_bid1_sz"] == pytest.approx(900.0)


def test_ask_surge_without_big_wall(tmp_path: Path):
    """UP ask 上移但 DOWN bid 无大单 → ASK_SURGE（不是诱空）"""
    snaps = [
        _snap(30, 0.37, 0.36, 0.63, 0.62),
        _snap(32, 0.45, 0.44, 0.55, 0.54),
        _snap(34, 0.50, 0.49, 0.51, 0.50),
    ]
    p = _write(tmp_path, snaps)
    m = fetch_book_momentum(SLUG, 35, snap_path=p)
    assert m is not None
    assert m["flag"] == "ASK_SURGE"


def test_flat_when_no_delta(tmp_path: Path):
    """ask 波动 < 4 分 → FLAT"""
    snaps = [
        _snap(30, 0.50, 0.49, 0.50, 0.49),
        _snap(32, 0.51, 0.50, 0.49, 0.48),
        _snap(34, 0.51, 0.50, 0.49, 0.48),
    ]
    p = _write(tmp_path, snaps)
    m = fetch_book_momentum(SLUG, 35, snap_path=p)
    assert m is not None
    assert m["flag"] == "FLAT"


def test_ask_fade(tmp_path: Path):
    """UP ask 下移 → ASK_FADE"""
    snaps = [
        _snap(30, 0.60, 0.59, 0.40, 0.39),
        _snap(32, 0.52, 0.51, 0.48, 0.47),
        _snap(34, 0.50, 0.49, 0.50, 0.49),
    ]
    p = _write(tmp_path, snaps)
    m = fetch_book_momentum(SLUG, 35, snap_path=p)
    assert m is not None
    assert m["flag"] == "ASK_FADE"


def test_fewer_than_min_samples_returns_none(tmp_path: Path):
    """样本不足 → None（不污染推理主流程）"""
    p = _write(tmp_path, [_snap(30, 0.37, 0.36, 0.63, 0.62)])
    assert fetch_book_momentum(SLUG, 35, snap_path=p) is None


def test_future_snapshot_excluded(tmp_path: Path):
    """未来分钟快照（> dm）不能进入窗口（防决策点泄漏）"""
    snaps = [
        _snap(30, 0.37, 0.36, 0.63, 0.62),
        _snap(34, 0.40, 0.39, 0.60, 0.59),
        _snap(38, 0.95, 0.94, 0.05, 0.04),  # dm=35 决策点后的"未来"
    ]
    p = _write(tmp_path, snaps)
    m = fetch_book_momentum(SLUG, 35, snap_path=p)
    assert m is not None
    assert m["samples"] == 2  # 38 被剔除
    assert m["up_ask_delta"] == pytest.approx(0.03)


def test_missing_snap_file_returns_none(tmp_path: Path):
    """快照文件不存在 → None"""
    assert fetch_book_momentum(SLUG, 35, snap_path=tmp_path / "nope.jsonl") is None