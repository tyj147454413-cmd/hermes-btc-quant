#!/usr/bin/env python3
"""round44.1 mock 测试：tf_summary 方向字段已收盘口径 + PM 动量权重 ×2。

验证项：
- A1: recent_dir 剔除进行中 K 线（旧口径 DOWN → 新口径 UP 的构造场）
- A2: 反向构造（旧 UP → 新 DOWN）
- A3: consecutive_up/down 只数已收盘 6 根（进行中阴线不再计入）
- A4: <4 根边界回退不崩、键齐全
- A5: 价格展示字段 last_close 仍取完整 closes（语义不变）
- B1-B4: pm_momentum ±2 / 微值 0 / 缺失 0
- C1-C3: 源码断言（W_PM_MOMENTUM=2、SCHEMA_VERSION=7、tf_summary 用 _closed_c）
- D1-D2: 掉头检测 note 仍工作
"""
import sys
from pathlib import Path

ROOT = Path("/Users/tangyijun/Desktop/hermes btc")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from hermes_collect import tf_summary, SCHEMA_VERSION  # noqa: E402
from signal_validator import extract_factors, W_PM_MOMENTUM  # noqa: E402

hc_src = (ROOT / "hermes_collect.py").read_text(encoding="utf-8")
passed, failed = 0, 0


def ok(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"✅ {label}" + (f" | {detail}" if detail else ""))
    else:
        failed += 1
        print(f"❌ {label}" + (f" | {detail}" if detail else ""))


def mk_candles(closed_closes, progress_close):
    """构造 parsed K 线列表：closed_closes 为已收盘 close 序列，progress 为进行中末根。
    全部已收盘为阳(o=c-1)，进行中按需设阴阳。"""
    kl = []
    n_closed = len(closed_closes)
    for i in range(n_closed + 1):
        if i < n_closed:
            c = closed_closes[i]
            o = c - 1.0
        else:
            c = progress_close
            o = c + 1.0  # 进行中设为阴线（o>c），用于 A3 计数排除验证
        kl.append({"o": o, "h": max(o, c) + 1.0, "l": min(o, c) - 1.0,
                   "c": c, "v": 10.0, "t": 1700000000000 + i * 3600000})
    return kl


# ---- A1: recent_dir 剔除进行中（旧口径 DOWN → 新口径 UP）----
# closed closes = [100,110,105]，进行中=95：旧逻辑 95>=110 False→DOWN；新逻辑 105>=100 True→UP
s = tf_summary(mk_candles([100, 110, 105], 95), "15m")
ok("A1 recent_dir 用已收盘(UP)", s["recent_dir"] == "UP", f"got={s['recent_dir']}")

# ---- A2: 反向构造（旧 UP → 新 DOWN）----
# closed=[110,90,100]，进行中=120：旧 120>=90 → UP；新 100>=110 False → DOWN
s = tf_summary(mk_candles([110, 90, 100], 120), "15m")
ok("A2 recent_dir 用已收盘(DOWN)", s["recent_dir"] == "DOWN", f"got={s['recent_dir']}")

# ---- A3: consecutive 只数已收盘（6 根全阳 + 进行中阴）----
kl = []
for i in range(7):
    if i < 6:
        kl.append({"o": 100.0 + i, "h": 102.0 + i, "l": 99.0 + i, "c": 101.0 + i,
                   "v": 10.0, "t": 1700000000000 + i * 3600000})
    else:
        kl.append({"o": 200.0, "h": 201.0, "l": 189.0, "c": 190.0,
                   "v": 10.0, "t": 1700000000000 + 6 * 3600000})
s = tf_summary(kl, "15m")
ok("A3a consec_up=6(不含进行中阴线)", s["consecutive_up"] == 6, f"got={s['consecutive_up']}")
ok("A3b consec_dn=0", s["consecutive_down"] == 0, f"got={s['consecutive_down']}")

# ---- A4: <4 根边界不崩 ----
try:
    s = tf_summary(mk_candles([100, 101], 102), "5m")
    ok("A4 短列表回退不崩且键齐", all(k in s for k in ("recent_dir", "consecutive_up", "last_3_candles")))
except Exception as e:
    ok("A4 短列表回退不崩且键齐", False, repr(e))

# ---- A5: last_close 仍为完整 closes 末值（价格展示字段语义不变）----
s = tf_summary(mk_candles([100, 110, 105], 95), "15m")
ok("A5 last_close=进行中价 95", abs(s["last_close"] - 95.0) < 1e-9, f"got={s['last_close']}")

# ---- B: pm_momentum ×2 ----
import json  # noqa: E402

REAL = ROOT / "data" / "snapshots" / "bitcoin-up-or-down-august-24-2026-9am-et_20260824_211602.json"
base = json.loads(REAL.read_text())


def mk(delta, up_price=0.80):
    d = json.loads(json.dumps(base))
    d.setdefault("polymarket", {})
    if delta is not None:
        d["polymarket"]["up_price_delta"] = delta
    # up_price 固定 0.80（看多绝对价）：B1-B4 计分不受影响；D1 掉头检测需要
    # 绝对价方向(看多) 与动量方向相反才成立
    d["polymarket"]["up_price"] = up_price
    return d


f, _, _, _ = extract_factors(mk(0.165))
# round49：W_PM_MOMENTUM 3→2（用户 8/28 决策「PM 动量还是都回到+2」，回退 round47），期望值同步
ok("B1 Δ+0.165 → +2（幅度=权重）", f.get("pm_momentum") == 2.0 and W_PM_MOMENTUM == 2,
   f"got={f.get('pm_momentum')}")
f, _, _, _ = extract_factors(mk(-0.05))
ok("B2 Δ-0.05 → -2", f.get("pm_momentum") == -2.0)
f, _, _, _ = extract_factors(mk(0.01))
ok("B3 Δ0.01 → 0", f.get("pm_momentum") == 0.0)
f, _, _, _ = extract_factors(mk(None))
ok("B4 缺失 → 0", f.get("pm_momentum") == 0.0)

f, _, pm_notes, _ = extract_factors(mk(-0.05))  # 注意返回序：factors, inp, notes, mode
ok("D1 掉头 note 仍产出", any("PM 动量掉头" in n for n in pm_notes),
   f"pm_momentum={f.get('pm_momentum')}")

# ---- C: 源码断言 ----
ok("C1 SCHEMA_VERSION=7", SCHEMA_VERSION == 7, f"got={SCHEMA_VERSION}")
ok("C2 recent_dir 用 _closed_c", '_closed_c[-1] >= _closed_c[-3]' in hc_src)
ok("C3 consecutive 用 _closed_c/_closed_o",
   "range(min(6, len(_closed_c)))" in hc_src and "_closed_o[-1-i]" in hc_src)
ok("C4 W_PM_MOMENTUM=2(round49回退)", W_PM_MOMENTUM == 2)

print(f"\nround44.1 mock：{passed}/{passed + failed} 通过")
sys.exit(1 if failed else 0)
