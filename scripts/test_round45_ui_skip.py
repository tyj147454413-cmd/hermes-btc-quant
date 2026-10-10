"""
Round 45 (2026-08-26 用户决策「UI 小金额=SKIP 开关」) mock 测试。
验证 hermes_1h_bot.py process_signal 在 UI bet_amount < min_bet_amount 时返回 SKIP 而非回退到默认 $10。

测试场景:
1. UI bet=$0.50 + min=$1.0 → 期望 SKIP (skip_reason 含 ui_bet_below_threshold)
2. UI bet=$10 + min=$1.0 → 期望 不 SKIP (继续到下单链路,network error 正常)
3. UI bet=$0 → 期望 不 SKIP (信号默认 amount 生效)
4. UI bet=$1.0 (边界等于) → 期望 不 SKIP

历史背景: 8/26 9AM ET 用户设 UI $0.50 想 SKIP,旧代码反而回退到 $10 成交 15.15 shares @ 0.66 亏 -$10,
新代码 round45 起 SKIP 路径阻断。
"""

import sys
import json
import importlib.util
import types
from pathlib import Path

HERMES_DIR = Path("/Users/tangyijun/Desktop/hermes btc")
# 沙箱映射到 /sessions/<sess>/mnt/hermes btc/
import os
if not HERMES_DIR.exists():
    HERMES_DIR = Path(os.environ.get("HERMES_DIR", "/sessions/inspiring-confident-tesla/mnt/hermes btc"))
CFG_PATH = HERMES_DIR / "hermes_config.json"
BOT_PATH = HERMES_DIR / "hermes_1h_bot.py"

_orig_cfg = CFG_PATH.read_text()


def _patch_bot():
    """Import hermes_1h_bot with _trading_pause_active stubbed out."""
    spec = importlib.util.spec_from_file_location("hermes_1h_bot", str(BOT_PATH))
    m = importlib.util.module_from_spec(spec)
    sys.modules["hermes_1h_bot"] = m
    m._trading_pause_active = lambda *a, **k: False
    # 添加 HERMES_DIR 到 path 让 hermes_common 可被 import
    sys.path.insert(0, str(HERMES_DIR))
    spec.loader.exec_module(m)
    return m


def _set_cfg(bet):
    d = json.loads(_orig_cfg)
    d["bet_amount"] = bet
    CFG_PATH.write_text(json.dumps(d))


def _restore():
    CFG_PATH.write_text(_orig_cfg)


def run():
    m = _patch_bot()
    results = []

    # Test 1: UI bet=$0.50 → SKIP
    try:
        _set_cfg(0.50)
        bot = m.HermesBot(live=False)
        sig = {"slug": "mock-skip-50", "amount": 10.0, "max_price": 0.60,
               "action": "ORDER", "side": "DOWN"}
        r = bot.process_signal(sig)
        ok = r.get("skipped") is True and "ui_bet_below_threshold" in r.get("skip_reason", "")
        results.append(("Test1 UI bet=0.50 SKIP", ok, r))
    finally:
        _restore()

    # Test 2: UI bet=$10 → 不 SKIP (继续到 network)
    try:
        _set_cfg(10.0)
        bot = m.HermesBot(live=False)
        sig = {"slug": "mock-normal-10", "amount": 10.0, "max_price": 0.60,
               "action": "ORDER", "side": "DOWN"}
        r = bot.process_signal(sig)
        ok = r.get("skipped") is not True
        results.append(("Test2 UI bet=10 不 SKIP", ok, r))
    finally:
        _restore()

    # Test 3: UI bet=$0 → 不 SKIP (回退到信号默认 $10)
    try:
        _set_cfg(0)
        bot = m.HermesBot(live=False)
        sig = {"slug": "mock-zero", "amount": 10.0, "max_price": 0.60,
               "action": "ORDER", "side": "DOWN"}
        r = bot.process_signal(sig)
        ok = r.get("skipped") is not True
        results.append(("Test3 UI bet=0 不 SKIP", ok, r))
    finally:
        _restore()

    # Test 4: UI bet=$1.0 (边界等于 min) → 不 SKIP
    try:
        _set_cfg(1.0)
        bot = m.HermesBot(live=False)
        sig = {"slug": "mock-edge-1", "amount": 10.0, "max_price": 0.60,
               "action": "ORDER", "side": "DOWN"}
        r = bot.process_signal(sig)
        ok = r.get("skipped") is not True
        results.append(("Test4 UI bet=1.0 边界 不 SKIP", ok, r))
    finally:
        _restore()

    # 汇总
    print("=" * 60)
    passed = 0
    for name, ok, r in results:
        status = "✅ PASS" if ok else "❌ FAIL"
        print(f"{status} | {name} | result={r}")
        if ok:
            passed += 1
    print("=" * 60)
    print(f"总计 {passed}/{len(results)} 通过")
    return passed == len(results)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)