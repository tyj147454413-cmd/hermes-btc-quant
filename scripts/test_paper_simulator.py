#!/usr/bin/env python3
"""
test_paper_simulator.py — paper_simulator.py 单元 mock 测试
覆盖 8 个 case:
1. paused=true + UP 信号 + book 正常 → 写入 entry_price = book_up_ask
2. paused=true + UP ask>max_price → entry_price = max_price(挂单)
3. paused=false → 拒绝(避免污染实盘)
4. action=SKIP → 拒绝
5. settle UP 赢 → p_l = shares - cost(>0)
6. settle DOWN 输 → p_l = -cost(<0)
7. settle DOWN 平(close == anchor) → FLAT 视 UP 胜(本场 DOWN 输)
8. report 无样本/有样本 全 settled 输出统计
"""
from __future__ import annotations
import json, os, sys, shutil, tempfile
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).parent
HERMES_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import paper_simulator as ps


def make_snapshot(slug: str, anchor: float, current: float,
                  book_up_ask: float = 0.5, book_up_bid: float = 0.5,
                  up_price: float = 0.5, down_price: float = 0.5) -> dict:
    """生成一个最小可用的快照,带 polymarket 块"""
    return {
        "slug": slug,
        "timestamp": "2026-08-28T20:00:00-04:00",
        "anchor_price": anchor,
        "current_price": current,
        "polymarket": {
            "up_price": up_price,
            "down_price": down_price,
            "book_up_ask": book_up_ask,
            "book_up_bid": book_up_bid,
            "book_up_mid": (book_up_ask + book_up_bid) / 2,
        },
    }


def make_signal(slug: str, side: str = "UP", action: str = "ORDER",
                max_price: float = 0.60, probability: float = 0.67) -> dict:
    return {
        "slug": slug,
        "signal": side,
        "side": side,
        "action": action,
        "source": "btc-agent",
        "probability": probability,
        "max_price": max_price,
        "strategy_version": "round47",
    }


def test_case_1():
    """paused=true + UP + book 正常 → entry_price = book_up_ask"""
    slug = "bitcoin-up-or-down-august-28-2026-8pm-et"
    sig = make_signal(slug, "UP", max_price=0.60)
    snap = make_snapshot(slug, anchor=80000, current=80200,
                         book_up_ask=0.55, book_up_bid=0.50, up_price=0.525)
    tmpdir = Path(tempfile.mkdtemp())
    try:
        with patch.object(ps, "SHADOW_FILE", tmpdir / "shadow.jsonl"), \
             patch.object(ps, "SIGNAL_FILE", tmpdir / "signal.jsonl"), \
             patch.object(ps, "SNAP_DIR", tmpdir / "snapshots"), \
             patch.object(ps, "CONFIG_FILE", tmpdir / "config.json"), \
             patch.object(ps, "is_paused", return_value=True), \
             patch.object(ps, "find_signal", return_value=sig), \
             patch.object(ps, "find_snapshot", return_value=snap):
            (tmpdir / "config.json").write_text(json.dumps({"trading_paused": True}))
            (tmpdir / "snapshots").mkdir()
            (tmpdir / "snapshots" / f"{slug}_x.json").write_text(json.dumps(snap))
            result = ps.cmd_enter(slug, amount=1.0)
        assert result["ok"], f"期望 ok=True, 实际 {result}"
        rec = result["record"]
        assert rec["entry_price"] == 0.55, f"entry_price 应为 book_up_ask=0.55, 实际 {rec['entry_price']}"
        assert rec["side"] == "UP"
        assert rec["shares"] == round(1.0 / 0.55, 4)
        assert rec["cost"] == round(rec["shares"] * 0.55, 4)
        assert rec["status"] == "pending"
        assert rec["max_price"] == 0.60
        print(f"✅ case1: UP entry_price={rec['entry_price']} shares={rec['shares']} cost=${rec['cost']}")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_case_2():
    """paused=true + UP ask>max_price → entry_price = max_price(挂单)"""
    slug = "bitcoin-up-or-down-august-28-2026-9pm-et"
    sig = make_signal(slug, "UP", max_price=0.60)
    snap = make_snapshot(slug, anchor=80000, current=80200,
                         book_up_ask=0.75, book_up_bid=0.70, up_price=0.725)
    tmpdir = Path(tempfile.mkdtemp())
    try:
        with patch.object(ps, "SHADOW_FILE", tmpdir / "shadow.jsonl"), \
             patch.object(ps, "SIGNAL_FILE", tmpdir / "signal.jsonl"), \
             patch.object(ps, "SNAP_DIR", tmpdir / "snapshots"), \
             patch.object(ps, "CONFIG_FILE", tmpdir / "config.json"), \
             patch.object(ps, "is_paused", return_value=True), \
             patch.object(ps, "find_signal", return_value=sig), \
             patch.object(ps, "find_snapshot", return_value=snap):
            (tmpdir / "config.json").write_text(json.dumps({"trading_paused": True}))
            (tmpdir / "snapshots").mkdir()
            (tmpdir / "snapshots" / f"{slug}_x.json").write_text(json.dumps(snap))
            result = ps.cmd_enter(slug, amount=1.0)
        assert result["ok"], f"期望 ok=True, 实际 {result}"
        rec = result["record"]
        assert rec["entry_price"] == 0.60, f"ask=0.75>max_price=0.60 应挂 0.60, 实际 {rec['entry_price']}"
        assert rec["shares"] == round(1.0 / 0.60, 4)
        print(f"✅ case2: ask>max_price → 挂 max_price=0.60, shares={rec['shares']}")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_case_3():
    """paused=false → 拒绝(避免污染实盘)"""
    slug = "bitcoin-up-or-down-august-28-2026-8pm-et"
    with patch.object(ps, "is_paused", return_value=False):
        result = ps.cmd_enter(slug, amount=1.0)
    assert not result["ok"], "paused=false 应拒绝"
    assert "trading_paused=false" in result["error"]
    print(f"✅ case3: paused=false 拒绝 — {result['error']}")
    return True


def test_case_4():
    """action=SKIP → 拒绝"""
    slug = "bitcoin-up-or-down-august-28-2026-8pm-et"
    sig = make_signal(slug, "UP", action="SKIP")
    with patch.object(ps, "is_paused", return_value=True), \
         patch.object(ps, "find_signal", return_value=sig):
        result = ps.cmd_enter(slug, amount=1.0)
    assert not result["ok"], "SKIP 信号应拒绝"
    assert "SKIP" in result["error"]
    print(f"✅ case4: SKIP 信号拒绝 — {result['error']}")
    return True


def test_case_5():
    """settle UP 赢 → p_l = shares - cost(>0)"""
    slug = "bitcoin-up-or-down-august-28-2026-8pm-et"
    nxt = ps.next_slug(slug)
    assert nxt == "bitcoin-up-or-down-august-28-2026-9pm-et", f"next_slug 计算错: {nxt}"

    pending = [{
        "ts": "2026-08-28T00:32:00+00:00",
        "slug": slug, "side": "UP", "action": "ORDER",
        "max_price": 0.60, "anchor_price": 80000,
        "entry_price": 0.55, "shares": 1.8182, "amount": 1.0, "cost": 1.0,
        "status": "pending",
    }]
    # 下一小时 current_price > anchor → UP 胜
    snap_nxt = make_snapshot(nxt, anchor=80000, current=80300)
    tmpdir = Path(tempfile.mkdtemp())
    try:
        with patch.object(ps, "SHADOW_FILE", tmpdir / "shadow.jsonl"), \
             patch.object(ps, "SNAP_DIR", tmpdir / "snapshots"), \
             patch.object(ps, "find_snapshot", return_value=snap_nxt):
            (tmpdir / "shadow.jsonl").write_text("\n".join(json.dumps(r) for r in pending) + "\n")
            result = ps.cmd_settle()
            # 验证(必须在 with 内读,否则 mock 已失效)
            settled = ps.load_shadow()
        assert result["ok"], f"settle 应成功, 实际 {result}"
        s = settled[0]
        assert s["status"] == "settled"
        assert s["actual_dir"] == "UP"
        assert s["win"] is True
        # p_l = shares*1.0 - cost = 1.8182 - 1.0 = 0.8182
        expected_p_l = round(1.8182 * 1.0 - 1.0, 4)
        assert abs(s["p_l"] - expected_p_l) < 0.01, f"p_l 期望≈{expected_p_l}, 实际 {s['p_l']}"
        assert s["close_price"] == 80300
        print(f"✅ case5: UP 胜 p_l=${s['p_l']:.4f} (>0)")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_case_6():
    """settle DOWN 输 → p_l = -cost(<0)"""
    slug = "bitcoin-up-or-down-august-28-2026-8pm-et"
    pending = [{
        "ts": "2026-08-28T00:32:00+00:00",
        "slug": slug, "side": "DOWN", "action": "ORDER",
        "max_price": 0.60, "anchor_price": 80000,
        "entry_price": 0.50, "shares": 2.0, "amount": 1.0, "cost": 1.0,
        "status": "pending",
    }]
    nxt = ps.next_slug(slug)
    # current_price > anchor → 实际 UP,DOWN 输
    snap_nxt = make_snapshot(nxt, anchor=80000, current=80300)
    tmpdir = Path(tempfile.mkdtemp())
    try:
        with patch.object(ps, "SHADOW_FILE", tmpdir / "shadow.jsonl"), \
             patch.object(ps, "SNAP_DIR", tmpdir / "snapshots"), \
             patch.object(ps, "find_snapshot", return_value=snap_nxt):
            (tmpdir / "shadow.jsonl").write_text("\n".join(json.dumps(r) for r in pending) + "\n")
            result = ps.cmd_settle()
            settled = ps.load_shadow()
        assert result["ok"], result
        s = settled[0]
        assert s["actual_dir"] == "UP"
        assert s["win"] is False
        assert s["p_l"] == -1.0, f"p_l 应为 -1.0, 实际 {s['p_l']}"
        print(f"✅ case6: DOWN 输 p_l=${s['p_l']} (<0)")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_case_7():
    """settle close == anchor → FLAT 视 UP 胜(本场 DOWN 输)"""
    slug = "bitcoin-up-or-down-august-28-2026-8pm-et"
    pending = [{
        "ts": "2026-08-28T00:32:00+00:00",
        "slug": slug, "side": "DOWN", "action": "ORDER",
        "max_price": 0.60, "anchor_price": 80000,
        "entry_price": 0.50, "shares": 2.0, "amount": 1.0, "cost": 1.0,
        "status": "pending",
    }]
    nxt = ps.next_slug(slug)
    # close == anchor → FLAT
    snap_nxt = make_snapshot(nxt, anchor=80000, current=80000)
    tmpdir = Path(tempfile.mkdtemp())
    try:
        with patch.object(ps, "SHADOW_FILE", tmpdir / "shadow.jsonl"), \
             patch.object(ps, "SNAP_DIR", tmpdir / "snapshots"), \
             patch.object(ps, "find_snapshot", return_value=snap_nxt):
            (tmpdir / "shadow.jsonl").write_text("\n".join(json.dumps(r) for r in pending) + "\n")
            result = ps.cmd_settle()
            settled = ps.load_shadow()
        assert result["ok"]
        s = settled[0]
        assert s["actual_dir"] == "FLAT"
        assert s["win"] is False, "DOWN+FLAT 应输"
        assert s["p_l"] == -1.0
        print(f"✅ case7: FLAT 平 → DOWN 输, p_l=${s['p_l']}")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_case_8():
    """report 无样本/有样本 全 settled 输出"""
    tmpdir = Path(tempfile.mkdtemp())
    try:
        # 空 → 无样本
        with patch.object(ps, "SHADOW_FILE", tmpdir / "empty.jsonl"):
            r = ps.cmd_report(days=7)
        assert r["ok"]
        assert "无样本" in r.get("msg", "")

        # 有样本 — UP 胜 1 + DOWN 输 1
        records = [
            {"ts": "2026-08-28T01:00:00+00:00", "slug": "x1", "side": "UP", "status": "settled", "win": True, "p_l": 0.5, "cost": 1.0},
            {"ts": "2026-08-28T02:00:00+00:00", "slug": "x2", "side": "DOWN", "status": "settled", "win": False, "p_l": -1.0, "cost": 1.0},
        ]
        with patch.object(ps, "SHADOW_FILE", tmpdir / "full.jsonl"):
            (tmpdir / "full.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
            r = ps.cmd_report(days=7)
        assert r["ok"]
        assert r["settled"] == 2
        assert r["pending"] == 0
        assert r["win_rate"] == 0.5
        assert abs(r["total_p_l"] - (-0.5)) < 0.01
        assert r["up_n"] == 1 and r["up_win_rate"] == 1.0
        assert r["dn_n"] == 1 and r["dn_win_rate"] == 0.0
        print(f"✅ case8: report {r['settled']} 笔,胜率 {r['win_rate']*100}%, P&L ${r['total_p_l']}")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    tests = [test_case_1, test_case_2, test_case_3, test_case_4,
             test_case_5, test_case_6, test_case_7, test_case_8]
    passed = 0
    failed = 0
    for t in tests:
        try:
            if t():
                passed += 1
            else:
                failed += 1
                print(f"❌ {t.__name__}: 失败")
        except AssertionError as e:
            failed += 1
            print(f"❌ {t.__name__}: AssertionError {e}")
        except Exception as e:
            failed += 1
            print(f"❌ {t.__name__}: Exception {type(e).__name__}: {e}")
    print(f"\n=== {passed}/{passed+failed} 通过 ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())