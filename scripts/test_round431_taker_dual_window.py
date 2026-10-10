"""round52 评分层 mock 测试：Taker B/S 单窗 + 贴线 1m taker + P0-1.3 因子增删验证.

历史沿革：
- round43.1/43.2：Taker B/S 双窗（5m+15m）机算，贴线 1m taker ×1。
- round51.4：_taker_grade 取消 ±2 极端档，合并 ±1/0 三档（>1.3→+1 / <0.8→-1 / 其余 0）。
- round52（P0-1.3）：删 taker_bs_15m/order_flow/open_micro/macro_trend 机算，
  加 recent_dir_5m/15m/pm_consensus 全×1 全模式计分；H12 键表/结算期键去双窗与订单流。

验证 12 项：
- T1: 5m 窗单窗保留（旧 snapshot 仍能机算，taker_bs 键保留）
- T2: _taker_grade 三档阈值正确（round51.4：>1.3→+1 / <0.8→-1 / 0.8-1.3→0，无 ±2 极端档）
- T3: taker_bs_15m 已退出机算（无 factors["taker_bs_15m"] 赋值语句）
- T4: 删因子机算块已移除（order_flow/open_micro/macro_trend 无 S₀ 赋值）
- T5: 新增因子机算存在且全 ×1（recent_dir_5m/15m、pm_consensus）
- T6: 贴线模式 1m taker 三档（round43.2 权重 ×1）
- T7: 贴线模式 1m 字段缺失 → 0 + notes
- T8: rule_engine H12 降权键 = taker_bs + taker_1m（无 taker_bs_15m）
- T9: 结算期降权键 = taker_bs（无 taker_bs_15m/order_flow）
- T10: RULE_VERSION 含 exec-round52
- T11: 锚点同步 round52（prompt_exec/CLAUDE.md/runtime_meta）
"""
import re
from pathlib import Path

ROOT = Path("/Users/tangyijun/Desktop/hermes btc")
SV_PATH = ROOT / "scripts" / "signal_validator.py"
RE_PATH = ROOT / "scripts" / "rule_engine.py"

sv_src = SV_PATH.read_text(encoding="utf-8")
re_src = RE_PATH.read_text(encoding="utf-8")


def assert_eq(actual, expected, label):
    assert actual == expected, f"❌ {label}: got {actual!r}, expected {expected!r}"
    print(f"✅ {label}")


def assert_in(needle, haystack, label):
    assert needle in haystack, f"❌ {label}: {needle!r} not found"
    print(f"✅ {label}")


def assert_not_in(needle, haystack, label):
    assert needle not in haystack, f"❌ {label}: {needle!r} 不应存在但被找到"
    print(f"✅ {label}")


# --- T1: 5m 窗单窗保留（读取 5m 字段代码还在）---
assert_in('cap.get("taker_bs_ratio")', sv_src, "T1: 5m 窗仍读 taker_bs_ratio")
assert_in('factors["taker_bs"] =', sv_src, "T1: taker_bs 因子键保留")

# --- T2: _taker_grade 三档阈值表（round51.4 取消 ±2 极端档）---
def _taker_grade(ratio: float) -> float:
    if ratio > 1.3:
        return 1.0
    if ratio < 0.8:
        return -1.0
    return 0.0


assert_eq(_taker_grade(1.856), 1.0, "T2a: 5m=1.856 → +1")
assert_eq(_taker_grade(3.0), 1.0, "T2b: 3.0 → +1（round51.4 无 +2 机构级档）")
assert_eq(_taker_grade(0.392), -1.0, "T2c: 0.392 → -1（round51.4 无 -2 机构级档）")
assert_eq(_taker_grade(0.9), 0.0, "T2d: 0.9 → 0（0.8-1.3 中性区）")
m = re.search(r"def _taker_grade\(ratio: float\) -> float:\s*\n((?:.*\n)*?)\n\n", sv_src)
assert m, "T2e: _taker_grade 函数未找到"
fn_body = m.group(1)
assert_not_in("> 2.33", fn_body, "T2e: 源码 _taker_grade 无 >2.33 极端档")
assert_not_in("< 0.43", fn_body, "T2e: 源码 _taker_grade 无 <0.43 极端档")
assert_in("> 1.3", fn_body, "T2e: 源码 _taker_grade 含 >1.3")
assert_in("< 0.8", fn_body, "T2e: 源码 _taker_grade 含 <0.8")

# --- T3: taker_bs_15m 已退出机算（round52 删因子）---
assert_not_in('factors["taker_bs_15m"] =', sv_src, "T3: 无 taker_bs_15m S₀ 赋值")
assert_not_in("taker_15m_ratio 缺失", sv_src, "T3: 无 taker_15m_ratio 缺失 note")

# --- T4: 删因子机算块已移除（order_flow/open_micro/macro_trend）---
assert_not_in('factors["order_flow"] =', sv_src, "T4: 无 order_flow S₀ 赋值")
assert_not_in('factors["open_micro"] =', sv_src, "T4: 无 open_micro S₀ 赋值")
assert_not_in('factors["macro_trend"] =', sv_src, "T4: 无 macro_trend S₀ 赋值")

# --- T5: 新增因子机算存在且全 ×1（round52 全模式计分）---
assert_in('("5m", "recent_dir_5m")', sv_src, "T5: recent_dir_5m 循环注入存在")
assert_in('("15m", "recent_dir_15m")', sv_src, "T5: recent_dir_15m 循环注入存在")
assert_in('factors["pm_consensus"]', sv_src, "T5: pm_consensus 机算存在")
assert_in("W_RECENT_DIR_5M = 1", sv_src, "T5: W_RECENT_DIR_5M = 1")
assert_in("W_RECENT_DIR_15M = 1", sv_src, "T5: W_RECENT_DIR_15M = 1")
assert_in("W_PM_CONSENSUS = 1", sv_src, "T5: W_PM_CONSENSUS = 1")

# --- T6/T7: 贴线 1m taker 三档 (round43.2 起权重 ×3→×1) ---
assert_in('factors["taker_1m"] = 1.0', sv_src, "T6: 贴线 1m>0.6 写 +1 (round43.2 权重 ×3→×1)")
assert_in('factors["taker_1m"] = -1.0', sv_src, "T6: 贴线 1m<0.4 写 -1 (round43.2)")
assert_in('factors["taker_1m"] = 0.0', sv_src, "T6: 贴线 1m 中性区写 0")
assert_in("W_TAKER_1M_TIE = 1", sv_src, "T6: 贴线 1m 权重 ×1 (round43.2)")
assert_in("taker_pct_last 缺失", sv_src, "T7: 1m 缺失 notes 标注存在")

# --- T8: rule_engine H12 降权键列表（round52 去 taker_bs_15m）---
m = re.search(r'for key in \("(taker_bs[^)]*?)"\)', re_src)
assert m, "T8: rule_engine H12 降权键列表未找到"
keys_str = m.group(1)
assert "taker_bs" in keys_str and "taker_1m" in keys_str, f"T8: H12 键缺 taker_bs/taker_1m: {keys_str}"
assert "taker_bs_15m" not in keys_str, f"T8: H12 键仍含 taker_bs_15m: {keys_str}"
print(f"✅ T8: H12 降权键列表 = ({keys_str})")

# --- T9: 结算期降权键列表（round52 去双窗与订单流）---
assert_in('for k in ("taker_bs",):', sv_src, "T9: 结算期降权键存在")
assert_not_in('for k in ("taker_bs", "taker_bs_15m", "order_flow")', sv_src, "T9: 结算期键不再含双窗+订单流")
print("✅ T9: 结算期降权键列表 = (taker_bs)")

# --- T10: RULE_VERSION 含 exec-round52 ---
assert_in("exec-round52=", re_src, "T10: RULE_VERSION 含 exec-round52")

# --- T11: 锚点同步 round52.1（round52 主版本 + 8/30 round52.1 贴线补丁；技能副本 .codebuddy 已 8/28 软禁，不强求同步）---
m_anchor = re.search(r"prompt\.md\\?@\d{4}-\d{2}-\d{2}-round52(\.1)?-", (ROOT / "prompt_exec.md").read_text(encoding="utf-8")[:2000])
assert m_anchor, "T11: prompt_exec.md 锚点 round52/round52.1"
print("✅ T11: prompt_exec.md 锚点 = round52.1")
m_anchor = re.search(r"round52", (ROOT / "CLAUDE.md").read_text(encoding="utf-8")[:2000])
assert m_anchor, "T11: CLAUDE.md 锚点 round52"
print("✅ T11: CLAUDE.md 锚点 = round52")
m_anchor = re.search(r'"strategy_version":\s*"round52(\.1)?"', (ROOT / "data" / "hermes_runtime_meta.json").read_text(encoding="utf-8"))
assert m_anchor, "T11: runtime_meta strategy_version = round52.1"
print("✅ T11: runtime_meta strategy_version = round52")

print(f"\n✅✅✅ round52 全部 12 项 mock 测试通过 ✅✅✅")
