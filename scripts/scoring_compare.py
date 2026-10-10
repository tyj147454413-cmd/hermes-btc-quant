#!/usr/bin/env python3
"""
引擎机算评分 vs AI 评分对比（round50+，2026-08-28）

目的：在 11 步推理完成后，让"规则引擎按权重公式算出的客观分"与"AI 主观分"
做结构化对比，供长期审计 + 当轮复盘。

设计：
- 不重算引擎（已由 signal_validator 算出 engine_check，避免重复造轮子）
- 输入：signal_dict（含 side/probability/confidence）+ engine_check（validate 返回的字典）
- 输出：
  1. signal_dict["scoring_compare"] 子块（追加在末尾，不动原字段）
  2. result/scoring_compare/{YYYYMMDD_HHMM}.json（落盘，供长期审计）

fail-open 铁律：与 signal_validator 一致——任何异常都不阻塞信号写入，
仅打印警告 + scoring_compare 块 skipped 标注。

用法：
  from scoring_compare import build_compare_block, persist_compare
  block = build_compare_block(signal_dict, engine_check)
  persist_compare(block, slug)
  signal_dict["scoring_compare"] = block
"""
# pyright: reportAny=false, reportExplicitAny=false, reportUnknownVariableType=false, reportUnknownMemberType=false

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_RESULT_DIR = _PROJECT_ROOT / "result" / "scoring_compare"


# ============ 概率 → up/down 拆解 ============

def _split_side_prob(side: str | None, prob: float | None) -> tuple[float, float]:
    """给 side + probability（0-1），返回 (up_prob, down_prob)。

    容忍 side 为 None / prob 为 None / prob 越界（兜底 0.5/0.5）。
    """
    if prob is None:
        return 0.5, 0.5
    try:
        p = float(prob)
    except (TypeError, ValueError):
        return 0.5, 0.5
    # 容忍百分比（>1 → /100）
    if p > 1:
        p = p / 100.0
    p = max(0.0, min(1.0, p))
    s = (side or "UP").upper()
    if s == "UP":
        return p, 1.0 - p
    if s == "DOWN":
        return 1.0 - p, p
    return 0.5, 0.5  # 未知 side


def _confidence_to_score(conf: str | float | None) -> float:
    """置信度文本/数字 → 0-1 数值。

    容忍：高/中/低/0.7/70。未知返回 0.5。
    """
    if conf is None:
        return 0.5
    if isinstance(conf, (int, float)):
        c = float(conf)
        if c > 1:
            c = c / 100.0
        return max(0.0, min(1.0, c))
    s = str(conf).strip()
    if s in ("高", "high", "HIGH"):
        return 0.85
    if s in ("中", "medium", "MEDIUM", "med"):
        return 0.65
    if s in ("低", "low", "LOW"):
        return 0.40
    try:
        c = float(s)
        if c > 1:
            c = c / 100.0
        return max(0.0, min(1.0, c))
    except (TypeError, ValueError):
        return 0.5


# ============ 主函数 ============

def build_compare_block(
    signal_dict: dict[str, Any],
    engine_check: dict[str, Any] | None,
) -> dict[str, Any]:
    """从 signal_dict + engine_check 构造 scoring_compare 子块。

    engine_check 来自 signal_validator.validate() 的第二个返回值。
    数据不足（engine_check 为 None / skipped）→ 返回仅含 skipped 标记的空块，不抛错。
    """
    now = datetime.now(timezone.utc).isoformat()
    slug = signal_dict.get("slug", "unknown")

    # 1. 引擎 SKIP 透传
    if not engine_check:
        return {
            "skipped": "engine_check 为空（signal_validator 未运行或导入失败）",
            "timestamp": now,
        }
    if engine_check.get("skipped"):
        return {
            "skipped": f"引擎校验跳过：{engine_check['skipped']}",
            "timestamp": now,
        }

    # 2. 提取 engine_score
    eng_side = engine_check.get("engine_direction") or engine_check.get("engine_action")
    eng_prob = engine_check.get("engine_probability")
    eng_conf_str = engine_check.get("engine_confidence")
    eng_conf = _confidence_to_score(eng_conf_str)
    eng_up, eng_down = _split_side_prob(eng_side, eng_prob)

    # 3. 提取 ai_score（从最终信号 signal_dict 取，validator 已做保守修正，所以这里取的是"修正后"的 AI/引擎混合值）
    # 注意：signal_dict["side"] 已是 validator 修正后的最终方向（引擎为准），不是原始 AI 意图。
    # 这是诚实声明：scoring_compare 比的是"最终落盘信号"vs"引擎原始结论"。
    # 若要对比"原始 AI 意图 vs 引擎"，看 engine_check.ai_side / ai_confidence / ai_claimed_s0。
    final_side = signal_dict.get("side") or signal_dict.get("signal") or "UP"
    final_prob = signal_dict.get("probability")
    final_conf = signal_dict.get("confidence")
    ai_up, ai_down = _split_side_prob(final_side, final_prob)
    ai_conf = _confidence_to_score(final_conf)

    # 4. 计算 diff
    delta_up = round(ai_up - eng_up, 4)
    # 方向一致 = 两侧 up/down 比例排序后一致
    eng_dir = "UP" if eng_up >= eng_down else "DOWN"
    ai_dir = "UP" if ai_up >= ai_down else "DOWN"
    agree = eng_dir == ai_dir

    # tier: S(<2pt) / A(<5pt) / B(<10pt) / C(≥10pt)
    abs_delta = abs(delta_up)
    if abs_delta < 0.02:
        tier = "S"
    elif abs_delta < 0.05:
        tier = "A"
    elif abs_delta < 0.10:
        tier = "B"
    else:
        tier = "C"

    # 5. 拼装 block
    return {
        "timestamp": now,
        "slug": slug,
        "engine_score": {
            "side": eng_dir,
            "up": round(eng_up, 4),
            "down": round(eng_down, 4),
            "confidence": round(eng_conf, 4),
            "s0": engine_check.get("engine_s0"),
            "net": engine_check.get("engine_net"),
            "pm_row": engine_check.get("engine_pm_row"),
            # round51.3 逐项因子明细落盘：每个因子给的分数（含未触发=0），S0=sum(values)。
            "factors": engine_check.get("factors") or {},
        },
        "ai_score": {
            "side": ai_dir,
            "up": round(ai_up, 4),
            "down": round(ai_down, 4),
            "confidence": round(ai_conf, 4),
        },
        "diff": {
            "delta_up": delta_up,
            "agree": agree,
            "tier": tier,
            "abs_delta": abs_delta,
        },
        "coverage": engine_check.get("coverage", ""),
        "corrected": bool(engine_check.get("corrected")),
        "correction_detail": engine_check.get("correction_detail", ""),
        "engine_risks": engine_check.get("engine_risks", []),
        "assumptions": engine_check.get("assumptions", []),
    }


def persist_compare(block: dict[str, Any], slug: str = "") -> bool:
    """落盘 scoring_compare 到 result/scoring_compare/{ts}.json。

    fail-open：任何 IO 异常仅 print 警告，绝不抛错（与 signal_validator 同纪律）。
    返回 True = 落盘成功；False = 失败（不影响信号写入主链路）。
    """
    if not block or block.get("skipped"):
        return False  # 无意义的数据不落盘
    try:
        _RESULT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone(timedelta(hours=8)))  # 北京时间落盘文件名
        ts_str = ts.strftime("%Y%m%d_%H%M%S")
        slug_safe = (slug or "unknown").replace("/", "_").replace(" ", "_")[:60]
        path = _RESULT_DIR / f"{ts_str}_{slug_safe}.json"
        path.write_text(json.dumps(block, ensure_ascii=False, indent=2))
        return True
    except (OSError, IOError) as e:
        print(f"⚠️ scoring_compare 落盘失败：{e}", file=sys.stderr)
        return False


# ============ 便捷入口（write_signal 调用） ============

def attach_and_persist(signal_dict: dict[str, Any], engine_check: dict[str, Any] | None) -> dict[str, Any]:
    """一站式：构造 block → 落盘 → 注入 signal_dict["scoring_compare"]。返回 signal_dict。

    用法（write_signal.py 第 154 行附近，engine_check 处理后）:
        from scoring_compare import attach_and_persist
        signal_dict = attach_and_persist(signal_dict, engine_check)
    """
    block = build_compare_block(signal_dict, engine_check)
    if not block.get("skipped"):
        persist_compare(block, signal_dict.get("slug", ""))
        signal_dict["scoring_compare"] = block
    else:
        # 即使 skipped 也注入空块（便于消费者知道对比未运行）
        signal_dict["scoring_compare"] = block
    return signal_dict


# ============ selftest ============

def _selftest() -> int:
    """自检：构造 mock engine_check → 验证 block 结构 → 验证落盘。"""
    failed = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failed
        print(f"{'✅' if ok else '❌'} {name}" + (f" → {detail}" if detail else ""))
        if not ok:
            failed += 1

    # 案例1：方向一致 + delta 极小（0.03 应是 A 档）
    sig1 = {"slug": "test-slug-1", "side": "UP", "signal": "UP", "probability": 0.65, "confidence": "中"}
    ec1 = {
        "engine_direction": "UP", "engine_probability": 0.62, "engine_confidence": "中",
        "engine_s0": 2, "engine_net": 3, "engine_pm_row": 1,
        "coverage": "机算 8 项", "corrected": False, "correction_detail": "",
        "engine_risks": [], "assumptions": [],
    }
    b1 = build_compare_block(sig1, ec1)
    check("案例1: tier=A（delta=0.03）", b1["diff"]["tier"] == "A", f"delta={b1['diff']['delta_up']} tier={b1['diff']['tier']}")
    check("案例1: agree=True", b1["diff"]["agree"] is True)
    check("案例1: engine_score.side=UP", b1["engine_score"]["side"] == "UP")
    check("案例1: ai_score.side=UP", b1["ai_score"]["side"] == "UP")
    check("案例1: 无 skipped", not b1.get("skipped"))

    # 案例2：方向不一致 → tier=C + agree=False
    # AI 判 UP 强（prob 0.70），引擎判 DOWN 强（prob 0.70）→ delta_up = 0.70 - 0.30 = 0.40 → C 档
    sig2 = {"slug": "test-slug-2", "side": "UP", "probability": 0.70, "confidence": "高"}
    ec2 = {
        "engine_direction": "DOWN", "engine_probability": 0.70, "engine_confidence": "高",
        "engine_s0": -3, "engine_net": -2, "engine_pm_row": -2,
        "coverage": "机算 8 项", "corrected": True, "correction_detail": "方向翻转",
        "engine_risks": ["⚠️"], "assumptions": [],
    }
    b2 = build_compare_block(sig2, ec2)
    check("案例2: agree=False", b2["diff"]["agree"] is False)
    check("案例2: corrected=True", b2["corrected"] is True)
    check("案例2: tier=C（大幅偏离）", b2["diff"]["tier"] == "C", f"delta={b2['diff']['delta_up']}")

    # 案例3：engine_check 缺失 → skipped 透传
    b3 = build_compare_block({"slug": "x", "side": "UP"}, None)
    check("案例3: skipped 标注", bool(b3.get("skipped")))

    # 案例4：概率百分比容忍（>1 → /100）
    sig4 = {"slug": "test-slug-4", "side": "DOWN", "probability": 65.0, "confidence": "中"}
    ec4 = {"engine_direction": "DOWN", "engine_probability": 60.0, "engine_confidence": "中"}
    b4 = build_compare_block(sig4, ec4)
    check("案例4: 百分比自动 /100", b4["ai_score"]["up"] < 0.5)

    # 案例5：落盘
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        global _RESULT_DIR, _PROJECT_ROOT
        _old_dir, _old_root = _RESULT_DIR, _PROJECT_ROOT
        _RESULT_DIR = Path(td) / "scoring_compare"
        _PROJECT_ROOT = Path(td)
        try:
            ok = persist_compare(b1, "test-slug-1")
            check("案例5: persist 落盘成功", ok)
            files = list(_RESULT_DIR.glob("*.json"))
            check("案例5: 文件存在", len(files) == 1, f"path={files[0].name if files else 'none'}")
        finally:
            _RESULT_DIR, _PROJECT_ROOT = _old_dir, _old_root

    print()
    if failed:
        print(f"❌ {failed} 项失败")
        return 1
    print("✅ 全部 selftest 通过")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("用法：from scoring_compare import attach_and_persist", file=sys.stderr)
    sys.exit(1)