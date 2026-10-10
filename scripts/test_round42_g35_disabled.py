"""
round42 mock 测试：G3.5 翻向禁用验证。

触发背景（2026-08-25 用户决策）：
- 实盘 11 笔翻向样本胜率 36.4%（vs 回测 79.2%）/ PnL -$67.65（vs 期望 +$69.90）
- 8/25 3AM ET 实场：S₀=-5 六维共振 + |偏差|=0.074%<0.15% 摆动市被 G3.5 无差别翻向

修复：双层禁用——
1. signal_validator._consensus_flip 翻向执行段全部 no-op（round41.6 validator 层）
2. rule_engine.judge() G3 证据不全硬覆盖分支改为保留 S₀（round41.3 judge 层）

G2 路径（PM≥80% 反向证据不足）按用户原始指令「G3.5 禁用」未涉及，保持原规则。

运行：python3 scripts/test_round42_g35_disabled.py
"""
import copy
import json
import os
import sys

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
                    'bitcoin-up-or-down-august-25-2026-3am-et_20260825_152657.json')
BASE = json.load(open(SNAP))

assert BASE['slug'].endswith('3am-et'), f'快照不是 3am-et: {BASE["slug"]}'
assert BASE['polymarket']['book_up_mid'] == 0.61, f'book_up_mid 应为 0.61'


# ---------- T1 复现 3AM ET 事故：AI DOWN → 引擎曾翻 UP → round42 后保留 DOWN ----------
# round41.6+41.3 旧行为: AI S₀=-5 DOWN, book_up_mid=61% UP 强共识 → G3 硬覆盖翻向 UP（应 LOSS）
# round42 新行为: 相同输入 → 应保留 DOWN, side 不变
sig_t1 = {
    "action": "ORDER",
    "signal": "DOWN",
    "side": "DOWN",
    "confidence": "中",
    "probability": 0.60,
    "max_price": 0.60,
    "ev": 0.0,
    "pm_down_price": 0.35,
    "position_scale": 0.5,
    "source": "btc-agent",
}
out_t1, ck_t1 = sv._validate_with_md(dict(sig_t1), copy.deepcopy(BASE), 'test')

# 核心断言
check("T1 [核心] side 保留 DOWN（未被 G3.5 翻向）", out_t1["side"], "DOWN")
check("T1 signal 保留 DOWN", out_t1["signal"], "DOWN")
check("T1 G3 触发锁低(G3证据不全禁用后保留原向锁低)", out_t1["confidence"], "低")
check("T1 max_price 保持 AI 原值 0.60", out_t1["max_price"], 0.60)
check("T1 pm_down_price 原值保留 0.35", out_t1.get("pm_down_price"), 0.35)
check("T1 engine direction = DOWN(judge 层未翻向)",
      ck_t1.get("engine_direction"), "DOWN")

# check 字段验证
check("T1 consensus_flip_disabled 字段存在",
      "consensus_flip_disabled" in ck_t1, True)
check("T1 consensus_flip_disabled 包含 PM=61%",
      "PM=61%" in ck_t1.get("consensus_flip_disabled", ""), True)
check("T1 consensus_flip_disabled 包含 round42",
      "round42" in ck_t1.get("consensus_flip_disabled", ""), True)
check("T1 consensus_flip_disabled_pm_cons=61.0",
      ck_t1.get("consensus_flip_disabled_pm_cons"), 61.0)
# 注意：原 consensus_flip 字段不再被填（G3.5 禁用）
check("T1 consensus_flip 字段不存在（G3.5 已禁用）",
      "consensus_flip" not in ck_t1, True)
# 引擎 G3 风险标注应包含「round42 禁用改判」
all_risks = ' '.join(ck_t1.get('engine_risks', []) + ck_t1.get('assumptions', []))
check("T1 engine_risks 含 round42 G3 禁用标注",
      "round42" in all_risks and ("G3 禁用" in all_risks or "禁用改判" in all_risks), True)


# ---------- T2 弱共识<60%：原本就不触发翻向，禁用后行为一致 ----------
sig_t2 = dict(sig_t1)
BASE_t2 = copy.deepcopy(BASE)
BASE_t2['polymarket']['book_up_mid'] = 0.55
BASE_t2['polymarket']['book_imbalance'] = 0.0
out_t2, ck_t2 = sv._validate_with_md(dict(sig_t2), BASE_t2, 'test')
check("T2 弱共识<60% side 保留 DOWN（未翻向）", out_t2["side"], "DOWN")
check("T2 弱共识不填 consensus_flip_disabled",
      "consensus_flip_disabled" not in ck_t2, True)


# ---------- T3 验证 _consensus_flip 直接调用（核心逻辑单测）----------
# 构造一个模拟 G3.5 触发条件（PM=70% 强共识 + 方向不一致）
mock_inp = {"book_up_mid": 70.0, "pm_up": 72.0}
mock_check = {}
mock_out = {"action": "ORDER", "side": "DOWN", "signal": "DOWN"}
annotations = []


def mock_annotate(msg):
    annotations.append(msg)


out_t3, ck_t3 = sv._consensus_flip(dict(mock_out), mock_inp, None, mock_check, mock_annotate)
check("T3 [核心] _consensus_flip 直调 side 保留 DOWN", out_t3["side"], "DOWN")
check("T3 直调无修正标记 corrected=False", ck_t3.get("corrected", False), False)
check("T3 直调填 consensus_flip_disabled", "consensus_flip_disabled" in ck_t3, True)
check("T3 至少 1 条 annotation 含 round42",
      any("round42" in a for a in annotations), True)
check("T3 不填原 consensus_flip 字段（已禁用）",
      "consensus_flip" not in ck_t3, True)


# ---------- T4 顺共识场景：_consensus_flip 不应触发任何翻向逻辑 ----------
mock_inp_t4 = {"book_up_mid": 70.0, "pm_up": 70.0}
mock_check_t4 = {}
mock_out_t4 = {"action": "ORDER", "side": "UP", "signal": "UP"}
annotations_t4 = []

out_t4, ck_t4 = sv._consensus_flip(dict(mock_out_t4), mock_inp_t4, None, mock_check_t4,
                                   lambda m: annotations_t4.append(m))
check("T4 顺共识 side 保留 UP（不应被填 disabled）", out_t4["side"], "UP")
check("T4 顺共识不填 consensus_flip_disabled",
      "consensus_flip_disabled" not in ck_t4, True)


# ---------- T5 SKIP 信号：_consensus_flip 守卫直接 return，不填任何字段 ----------
mock_inp_t5 = {"book_up_mid": 80.0, "pm_up": 85.0}
mock_check_t5 = {}
mock_out_t5 = {"action": "SKIP", "side": "DOWN", "signal": "DOWN"}

out_t5, ck_t5 = sv._consensus_flip(dict(mock_out_t5), mock_inp_t5, None, mock_check_t5, lambda m: None)
check("T5 SKIP 信号 side 保留 DOWN", out_t5["side"], "DOWN")
check("T5 SKIP 不填 consensus_flip_disabled（守卫生效）",
      "consensus_flip_disabled" not in ck_t5, True)


# ---------- T6 验证 mark 价失真仍可观测（仅标注不阻断）----------
mock_inp_t6 = {"book_up_mid": 50.0, "pm_up": 65.0}  # 差 15pt
mock_check_t6 = {}
mock_out_t6 = {"action": "ORDER", "side": "DOWN", "signal": "DOWN"}
annotations_t6 = []

out_t6, ck_t6 = sv._consensus_flip(dict(mock_out_t6), mock_inp_t6, None, mock_check_t6,
                                   lambda m: annotations_t6.append(m))
check("T6 mark 失真观测: annotation 含「标记价失真」",
      any("标记价失真" in a for a in annotations_t6), True)
check("T6 标记价失真不影响 side(保留 DOWN)", out_t6["side"], "DOWN")


print()
print(f"=== round42 mock 测试结果: {PASS}/{PASS+FAIL} 通过 ===")
if FAIL > 0:
    sys.exit(1)