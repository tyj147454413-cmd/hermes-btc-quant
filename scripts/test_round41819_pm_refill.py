#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round41.8.19 功能测试：翻向后 PM 基准字段回填 + max_price 口径拆分。

事故场景（2026-08-25 1am ET 实证）：
  AI 原始 DOWN 写 pm_down_price=0.335；引擎机算 S₀=+4 翻向 UP 后无人回填 pm_up_price
  → 消费信号 side=UP 但 pm_up_price=None，bot 的「一般波动检测（跌幅>15% 记录）+
  round41.4 C4 影子分层」按 final side 取 pm_up_price，双双静默失效。
修复（validator）：_validate_with_md 包装层出口统一回填——源=快照 gamma 标记价
  （41.8.17 同源口径，绝不填 book_up_mid），已有有效值不覆盖，SKIP 不处理。
修复（bot）：save_last_signal 展示副本把 max_price 改写为实际挂单价时，分析层 EV 核算
  基准保留进新字段 max_price_signal（修「max_price=0.8 与 ev=(P−0.60)×100 同框」）。

运行：python3 scripts/test_round41819_pm_refill.py"""
import copy
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import signal_validator as sv  # noqa: E402

PASS, FAIL = 0, 0


def check(name, got, want):
    global PASS, FAIL
    ok = got == want
    PASS += int(ok)
    FAIL += int(not ok)
    print(f"{'✅' if ok else '❌'} {name}: got={got!r} want={want!r}")


# 离线：实时复核 fail-open（测试不联网）
sv._fetch_live_pm_book = lambda slug: {}

SNAP = os.path.join(ROOT, 'data', 'snapshots',
                    'bitcoin-up-or-down-august-25-2026-1am-et_20260825_132705.json')
BASE = json.load(open(SNAP))
MARK_UP = BASE['polymarket']['up_price']        # 0.665
assert abs(MARK_UP - 0.665) < 1e-9

# ---------- T1 还原 1am 事故全路径：AI DOWN → 引擎翻 UP → pm_up_price 回填 ----------
sig1 = {"action": "ORDER", "signal": "DOWN", "side": "DOWN", "confidence": "低",
        "probability": 0.55, "max_price": 0.60, "ev": -5.0, "pm_down_price": 0.335}
out1, ck1 = sv._validate_with_md(dict(sig1), copy.deepcopy(BASE), 'test')
check("T1 引擎翻向 UP（复现1am场机算）", out1["side"], "UP")
check("T1 pm_up_price 回填=mark", out1.get("pm_up_price"), MARK_UP)
check("T1 pm_down_price 原值保留", out1.get("pm_down_price"), 0.335)
check("T1 detail 含回填标注", "翻向基准回填" in ck1.get("correction_detail", ""), True)

# ---------- T2 已有有效基准 → 不覆盖 ----------
sig2 = dict(sig1, side="UP", signal="UP", pm_up_price=0.5)
out2, ck2 = sv._validate_with_md(dict(sig2), copy.deepcopy(BASE), 'test')
check("T2 已有有效 pm_up_price 不覆盖", out2.get("pm_up_price"), 0.5)

# ---------- T3 标记价缺失/非法 → 不造值（fail-open 保持 None）----------
for tag, mut in (("None", {"up_price": None}), ("zero", {"up_price": 0}),
                 ("one", {"up_price": 1})):
    md3 = copy.deepcopy(BASE)
    md3['polymarket'].update(mut)
    sig3 = {"action": "ORDER", "signal": "DOWN", "side": "DOWN", "confidence": "中",
            "probability": 0.55, "max_price": 0.60, "pm_down_price": 0.335}
    out3, _ = sv._validate_with_md(dict(sig3), md3, 'test')
    check(f"T3 mark={tag} → 不造值", out3.get("pm_up_price"), None)

# ---------- T4 SKIP 信号 / 非法方向 → 不处理 ----------
sig4 = {"action": "SKIP", "signal": "UP", "side": "UP"}
out4 = sv._sync_pm_price_fields(dict(sig4), BASE, {})
check("T4 SKIP 不回填", out4.get("pm_up_price"), None)

# ---------- T5 DOWN 方向回填 down 价（对称路径）----------
md5 = copy.deepcopy(BASE)
md5['polymarket']['down_price'] = 0.335
out5, ck5 = {}, {}
out5 = {"action": "ORDER", "side": "DOWN", "signal": "DOWN"}
ck5 = {"correction_detail": ""}
out5 = sv._sync_pm_price_fields(out5, md5, ck5)
check("T5 DOWN 回填 pm_down_price", out5.get("pm_down_price"), 0.335)
check("T5 detail 标注回填", "回填" in ck5["correction_detail"], True)

# ---------- T6 round42 G3.5 禁用后翻向不再触发，但 pm_{side}_price 回填仍生效 ----------
# 历史行为（round41.6/41.8.13/41.8.19）：book_mid=86.5 强共识 → 翻向 UP + pm_up_price 回填
# round42 新行为：G3.5 禁用保留 DOWN + pm_down_price 原值保留 + pm_up_price 不需要回填
from types import SimpleNamespace  # noqa: E402
out6 = {"signal": "DOWN", "side": "DOWN", "action": "ORDER", "confidence": "中",
        "probability": 0.55, "max_price": 0.60, "ev": -5.0,
        "pm_down_price": 0.335}
inp6 = {"book_up_mid": 86.5, "pm_up": 66.5}
d6 = SimpleNamespace(position_scale=0.5)
ck6 = {}
notes6 = []
out6, ck6 = sv._consensus_flip(out6, inp6, d6, ck6, notes6.append)
check("T6 round42 G3.5 禁用保留 DOWN", out6["side"], "DOWN")
check("T6 填 consensus_flip_disabled 插桩",
      "PM=86%" in ck6.get("consensus_flip_disabled", ""), True)
out6 = sv._sync_pm_price_fields(out6, BASE, ck6)
# side=DOWN → pm_down_price 已写,不需要回填;pm_up_price 也不需要
check("T6 pm_down_price 原值保留 0.335", out6.get("pm_down_price"), 0.335)
check("T6 pm_up_price 不应被填(side=DOWN)", out6.get("pm_up_price"), None)

# ---------- T7 bot save_last_signal：max_price 拆分（order_price vs 分析基准）----------
spec = importlib.util.spec_from_file_location("hb_41819", os.path.join(ROOT, "hermes_1h_bot.py"))
hb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hb)

tmpdir = tempfile.mkdtemp()
hb.LAST_SIGNAL = hb.Path(tmpdir) / "hermes_last_signal.json"
hb.CONFIG = hb.Path(tmpdir) / "hermes_config.json"  # 不存在 → 走默认金额分支
bot = hb.HermesBot.__new__(hb.HermesBot)            # 绕过 __init__（不连网/不开线程）
bot._load_risk_cfg = lambda: {"default_bet_amount": 3.0, "min_bet_amount": 3.0,
                              "position_scale_enabled": False}
bot._apply_position_scale = lambda sig, amt, mx, scale_enabled=True: (amt, None)

sig7 = {"slug": "bitcoin-up-or-down-august-25-2026-1am-et", "side": "UP",
        "max_price": 0.60, "ev": 5.7, "amount": 10.0, "pm_up_price": 0.665}
res7 = {"ok": True, "status": "live", "order_price": 0.8, "amount": 10.0}
bot.save_last_signal(sig7, res7)
saved = json.loads(hb.LAST_SIGNAL.read_text())["signal"]
check("T7 展示 max_price=实际挂单价", saved.get("max_price"), 0.8)
check("T7 max_price_signal 保留分析基准", saved.get("max_price_signal"), 0.60)
check("T7 ev 不被改写（仍按 0.60 口径）", saved.get("ev"), 5.7)

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
