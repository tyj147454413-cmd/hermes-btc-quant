#!/usr/bin/env python3
"""
Walk-Forward 效率比分析。

从 oos_history.jsonl 读取最新的样本外胜率，计算 Walk-Forward 效率比
（OOS胜率 / 样本内胜率），量化规则迭代的过拟合程度。

用法：
    python3 scripts/walk_forward_analysis.py          # 输出效率比 + 判定
    python3 scripts/walk_forward_analysis.py --json   # 输出 JSON 供脚本消费

输出：
    - Walk-Forward 效率比（WFER）
    - 判定：健康 / 关注 / 过拟合警告 / 规则失效
    - 退出码：0=健康, 1=关注, 2=过拟合, 3=失效
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent.parent
OOS_HISTORY = THIS_DIR / "data" / "oos_history.jsonl"

# 样本内基线（round29 回测结果）
IN_SAMPLE_WIN_RATE = 73.1
IN_SAMPLE_TOTAL = 145

# 判定阈值
HEALTHY_THRESHOLD = 0.85   # >= 85% 样本内 = 健康
WARNING_THRESHOLD = 0.75   # >= 75% = 关注
ALERT_THRESHOLD = 0.60     # >= 60% = 过拟合，< 60% = 失效


def load_latest() -> dict | None:
    """加载 oos_history.jsonl 中最新一条记录。"""
    if not OOS_HISTORY.exists():
        return None
    try:
        entries = []
        for line in OOS_HISTORY.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries[-1] if entries else None
    except OSError:
        return None


def analyze(latest: dict) -> dict:
    """计算 Walk-Forward 效率比并判定。"""
    oos_wr = latest["win_rate"]
    total = latest["total"]
    wfer = oos_wr / IN_SAMPLE_WIN_RATE

    if wfer >= HEALTHY_THRESHOLD:
        status = "healthy"
        status_label = "健康"
        exit_code = 0
    elif wfer >= WARNING_THRESHOLD:
        status = "warning"
        status_label = "关注"
        exit_code = 1
    elif wfer >= ALERT_THRESHOLD:
        status = "overfit"
        status_label = "过拟合警告"
        exit_code = 2
    else:
        status = "broken"
        status_label = "规则失效"
        exit_code = 3

    return {
        "oos_win_rate": oos_wr,
        "in_sample_win_rate": IN_SAMPLE_WIN_RATE,
        "in_sample_total": IN_SAMPLE_TOTAL,
        "oos_total": total,
        "wfer": round(wfer, 3),
        "status": status,
        "status_label": status_label,
        "exit_code": exit_code,
        "interpretation": _interpretation(status, wfer, oos_wr),
    }


def _interpretation(status: str, wfer: float, oos_wr: float) -> str:
    if status == "healthy":
        return (
            f"WFER={wfer:.2f}，OOS胜率{oos_wr:.1f}%接近样本内{IN_SAMPLE_WIN_RATE:.1f}%。"
            f"规则迭代过拟合风险低，系统在未见数据上表现稳健。"
        )
    elif status == "warning":
        return (
            f"WFER={wfer:.2f}，OOS胜率{oos_wr:.1f}%显著低于样本内{IN_SAMPLE_WIN_RATE:.1f}%。"
            f"需关注：可能开始过拟合，或样本外市场环境与训练期不同。"
        )
    elif status == "overfit":
        return (
            f"WFER={wfer:.2f}，OOS胜率{oos_wr:.1f}%大幅低于样本内{IN_SAMPLE_WIN_RATE:.1f}%。"
            f"严重过拟合信号：规则可能过度适配训练数据，在真实市场中失效。"
            f"建议暂停规则迭代，回退到上一个 OOS 表现良好的版本。"
        )
    else:
        return (
            f"WFER={wfer:.2f}，OOS胜率{oos_wr:.1f}%崩溃。"
            f"规则已失效，必须立即停止交易并重新设计。"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Walk-Forward 效率比分析")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式")
    args = parser.parse_args()

    latest = load_latest()
    if not latest:
        msg = "未找到 oos_history.jsonl 或文件为空，请先运行 backtest_oos.py --save"
        if args.json:
            print(json.dumps({"error": msg}, ensure_ascii=False))
        else:
            print(f"⚠️ {msg}")
        return 0

    if latest["total"] < 10:
        msg = f"样本外仅 {latest['total']} 场（<10），效率比不可靠，暂不判定"
        if args.json:
            print(json.dumps({"error": msg, "oos_total": latest["total"]}, ensure_ascii=False))
        else:
            print(f"ℹ️ {msg}")
        return 0

    result = analyze(latest)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("=" * 60)
        print("  Walk-Forward 效率比分析")
        print("=" * 60)
        print(f"  样本外胜率:     {result['oos_win_rate']:.1f}% ({result['oos_total']}场)")
        print(f"  样本内胜率:     {result['in_sample_win_rate']:.1f}% ({result['in_sample_total']}场)")
        print(f"  WFER:           {result['wfer']:.3f}")
        print(f"  判定:           {result['status_label']}")
        print()
        print(f"  {result['interpretation']}")
        print("=" * 60)

    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())