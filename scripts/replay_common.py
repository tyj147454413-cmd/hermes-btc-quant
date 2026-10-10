#!/usr/bin/env python3
"""
Hermes BTC 回测共享基础设施（v1，P0-2 落地）。

被以下脚本依赖：
- scripts/regime_split_replay.py（四维胜率表官方校准）
- scripts/fill_bias_backtest.py（三宇宙成交偏差回测）
- scripts/candidate_counterfactual.py（P1-2 候选反事实回测）

提供：
- 路径常量（HERMES_DIR 沙箱兼容）
- 数据加载（results/fills/done/snapshots）
- Wilson 95% 区间
- EV 计算（二元合约固定注额口径）
- 引擎重放（两遍 extreme_wall 复算）

约束：
- 只读工具：不改引擎、不写信号队列、不写 drift_alerts（后者由 regime_split_replay 自身写）
- 不引入新外部依赖（jsonschema 不需要，signal_validator + rule_engine 已存在）
"""

from __future__ import annotations

import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

# ====================================================================
# 路径常量（HERMES_DIR 沙箱兼容）
# ====================================================================

_HERMES_DIR = os.environ.get("HERMES_DIR")
ROOT = Path(_HERMES_DIR) if _HERMES_DIR else Path("/Users/tangyijun/Desktop/hermes btc")

DATA_DIR = ROOT / "data"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
RESULTS_FILE = DATA_DIR / "hermes_results.jsonl"
FILLS_FILE = DATA_DIR / "hermes_fills.jsonl"
DONE_FILE = DATA_DIR / "hermes_done.jsonl"
ALERTS_FILE = DATA_DIR / "drift_alerts.jsonl"

# 引擎依赖（sys.path 注入供 signal_validator/rule_engine 找到）
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from signal_validator import extract_factors  # type: ignore
    from rule_engine import judge  # type: ignore
    _HAVE_ENGINE = True
except ImportError:
    _HAVE_ENGINE = False


# ====================================================================
# 数据加载
# ====================================================================

def load_results_actual(path: Path | None = None) -> dict[str, str]:
    """slug → actual（仅 'UP' / 'DOWN'）。regime_split_replay 等四维表统计用。"""
    out: dict[str, str] = {}
    p = path or RESULTS_FILE
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("actual") and r.get("slug"):
            out[r["slug"]] = r["actual"]
    return out


def load_results_full(path: Path | None = None) -> dict[str, dict]:
    """slug → 完整 results 记录（同 slug 多订单取最后）。fill_bias_backtest 三宇宙分析用。"""
    out: dict[str, dict] = {}
    p = path or RESULTS_FILE
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("actual") and r.get("slug"):
            out[r["slug"]] = r  # 后写覆盖先写（同 slug 取最后一条）
    return out


def load_fills(path: Path | None = None) -> dict[str, bool | None]:
    """order_id → filled 终态。供 results.filled=None 回填（架构文档 §5 P0 三件套）。"""
    out: dict[str, bool | None] = {}
    p = path or FILLS_FILE
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            f = json.loads(line)
        except json.JSONDecodeError:
            continue
        if f.get("order_id"):
            out[f["order_id"]] = f.get("filled")
    return out


def load_done(path: Path | None = None) -> dict[str, dict]:
    """slug → 实盘信号（AI side / probability_raw / strategy_version / model_version）。"""
    out: dict[str, dict] = {}
    p = path or DONE_FILE
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        sig = d.get("signal") or {}
        if sig.get("slug"):
            out[sig["slug"]] = sig
    return out


def get_latest_snapshots(snapshot_dir: Path | None = None) -> dict[str, Path]:
    """slug → 最新一份快照路径（按 mtime 选最新）。"""
    sd = snapshot_dir or SNAPSHOT_DIR
    slugs: dict[str, list[Path]] = defaultdict(list)
    if not sd.exists():
        return {}
    for p in sorted(sd.glob("*.json")):
        # 文件名格式 bitcoin-up-or-down-..._20260820_171200.json
        slug = p.name.split("_202")[0]
        slugs[slug].append(p)
    return {slug: sorted(paths, key=lambda x: x.stat().st_mtime)[-1] for slug, paths in slugs.items()}


# ====================================================================
# 统计工具
# ====================================================================

def wilson(w: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% 区间（百分比）。n=0 返回 (0, 0)。

    非 Wald 正态近似——小样本下 Wald 偏窄（架构文档 §5）。
    43.3% vs 52.5% 的差异即来自公式选择。
    """
    if n == 0:
        return 0.0, 0.0
    p = w / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (center - margin) / denom * 100), (center + margin) / denom * 100


def ev_at(win_rate: float, price: float) -> float:
    """二元合约 EV（小数口径）：wr*(1-price) - (1-wr)*price。

    注：架构文档 §6.3 简化假设——固定 $10 注额、成交价 0.5-0.6 区间下胜率差 ≈ EV 差。
    若注额/定价策略变化或进入 P2-3 walk-forward，须升级为 P&L 口径（replay 脚本扩展成交价字段）。
    """
    return win_rate * (1 - price) - (1 - win_rate) * price


def bucket_net(net: float) -> str:
    """|净分| → 桶：≥6 / 3-5 / 0-2（与 rule_engine._probability 表对齐）。"""
    an = abs(net)
    if an >= 6:
        return "≥6"
    if an >= 3:
        return "3-5"
    return "0-2"


# ====================================================================
# 引擎表镜像（rule_engine._probability 现行映射）
# ====================================================================

def engine_current(bucket: str, tie: bool, pm_same: bool, regime: str) -> float:
    """rule_engine._probability 现行映射（round35 后），按引擎回退逻辑取值。

    注意：改引擎表必须同步本函数（SYNC_MAP #11）；round35 后 0-2 档顺PM = 67.6/低。
    """
    if bucket == "≥6":
        if not tie and pm_same:
            return 84.6
        if not tie and not pm_same:
            return 60.0
        return 79.4
    if bucket == "3-5":
        if not tie and pm_same:
            return 88.9 if regime == "摆动市" else 75.0
        if not tie and not pm_same:
            return 62.5
        if tie and pm_same:
            return 56.2
        return 68.9
    # 0-2
    if not tie and pm_same:
        return 67.6
    if not tie and not pm_same:
        return 60.0
    if tie and pm_same:
        return 77.8
    return 82.1


# ====================================================================
# 引擎重放（两遍 extreme_wall 复算）
# ====================================================================

def replay_judge_full(md: dict[str, Any]) -> tuple | None:
    """对单份快照做两遍 extreme_wall 引擎复算，返回 (decision, factors, inp) 或 None（失败时）。

    与 regime_split_replay.py + fill_bias_backtest.py 的复算逻辑同源——抽到这里避免
    三处复制粘贴漂移。失败模式（extract_factors 抛错 / 无 final_direction）返回 None，
    由调用方决定 continue 或 raise。
    """
    if not _HAVE_ENGINE:
        raise RuntimeError("signal_validator / rule_engine 未导入，无法 replay")

    try:
        factors, inp, notes, mode = extract_factors(md)
    except Exception:
        return None

    buy_wall = inp.pop("_buy_wall_ratio", None)
    sell_wall = inp.pop("_sell_wall_ratio", None)
    taker_bs = inp.pop("taker_bs_ratio", None)

    prelim = judge(dict(inp, extreme_wall=False))
    if prelim.final_direction == "DOWN":
        extreme_wall = bool(sell_wall is not None and sell_wall > 3.0)
    elif prelim.final_direction == "UP":
        extreme_wall = bool(buy_wall is not None and buy_wall > 3.0)
    else:
        extreme_wall = False

    d = judge(dict(inp, extreme_wall=extreme_wall,
                   _buy_wall_ratio=buy_wall, _sell_wall_ratio=sell_wall,
                   taker_bs_ratio=taker_bs))
    if not d.final_direction:
        return None

    return d, factors, inp
