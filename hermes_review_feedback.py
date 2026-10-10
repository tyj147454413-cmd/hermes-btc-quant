# pyright: reportAny=false, reportExplicitAny=false, reportUnknownVariableType=false, reportUnknownParameterType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnusedCallResult=false, reportMissingParameterType=false
"""复盘教训 → 指标权重反馈模块（v2 — 修复小样本/阶跃跳变/权重下限）

读取 data/review-lessons.json 的 patterns 字段（各指标 correct/wrong 统计），
计算每个指标的 Wilson 下界准确率，映射成权重系数，反馈给 analyze_* 函数。

v2 修复（2026-07-31 审计）：
1. 【高危·4】MIN_SAMPLES 5→15，加入 Wilson 下界避免小样本虚高
2. 【中危·6】阶梯式映射 → 连续映射，消除边界跳变
3. 【高危·9】加入权重变化上限 ±0.15/次，防滞后反转
4. 【高危·10】权重下限 0.30→0.50，防极端行情永久 SKIP

权重映射（连续）：
  weight = clamp(1.0 + (wilson_lower - 0.5) * 1.5, 0.50, 1.20)
  —— Wilson 下界 0.50 → 权重 1.00（中性，随机猜测水平）
  —— Wilson 下界 0.63 → 权重 1.20（加成上限）
  —— Wilson 下界 0.37 → 权重 0.80
  —— Wilson 下界 0.17 → 权重 0.50（下限，不归零）
"""
try:
    import fcntl as _flock_mod
    _HAS_FCNTL = True
except ImportError:
    _flock_mod = None
    _HAS_FCNTL = False
# 9/2 修复: 与 hermes_review.py 同坑——fcntl/portalocker 必须分开 try，
# 合并时 Windows 上 fcntl 先崩致 _pocker=None，锁操作 AttributeError
try:
    import portalocker as _pocker
except ImportError:
    _pocker = None
import json
import math
import time
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
LESSONS_FILE = THIS_DIR / "data" / "review-lessons.json"
WEIGHTS_CACHE_FILE = THIS_DIR / "data" / "hermes_indicator_weights.json"

MIN_SAMPLES = 5          # 8→5: 小样本阶段（<30笔交易）Wilson过于保守，降到5让更多指标参与调整
CACHE_TTL_SECONDS = 30  # 30 秒缓存（逐笔即时反馈，降低到 30s 让分析器快速感知权重变化）
MAX_WEIGHT_DELTA = 0.15  # 单次权重变化上限（防滞后反转）
WEIGHT_FLOOR = 0.50      # 权重下限（防极端行情永久 SKIP）
WEIGHT_CEIL = 1.20       # 权重上限

# 断点3: 教训模式 → 热失效惩罚
LESSON_HOT_WINDOW = 5     # 查看最近 N 条教训
LESSON_HOT_THRESHOLD = 3  # 同一指标在最近 N 条中出现 ≥ M 次 → 视为"热失效"
LESSON_HOT_PENALTY = 0.70 # 热失效指标权重再乘以此系数（额外 -30%）

_cache: dict[str, Any] = {"ts": 0.0, "weights": {}, "regime": "normal", "source": "v5"}


def _wilson_lower_bound(correct: int, wrong: int, z: float = 1.28) -> float:
    """Wilson 80% 置信区间下界（z=1.28，原 95% z=1.96 在小样本下过于保守）。

    比纯 correct/(correct+wrong) 更保守但不过度：
    - 8/8 (100%) → 下界 0.68（而非 1.0）
    - 15/15 (100%) → 下界 0.81
    - 30/30 (100%) → 下界 0.90
    - 6/9 (67%) → 下界 0.40（原 95%: 0.35）
    """
    n = correct + wrong
    if n == 0:
        return 0.5
    phat = correct / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - margin)


def _accuracy_to_weight(wilson_lower: float) -> float:
    """Wilson 下界 → 连续权重（无阶跃跳变）

    Bug-修复: 公式基线从 0.5 修正为 1.0，使 Wilson=0.50（随机）→ weight=1.00（中性）。
    旧公式 0.5+(w-0.5)*1.5 在 w=0.5 时给出 0.50 而非 1.00，导致所有指标被系统性压到下限。
    """
    raw = 1.0 + (wilson_lower - 0.5) * 1.5
    return max(WEIGHT_FLOOR, min(WEIGHT_CEIL, raw))


def _get_hot_failure_indicators(regime: str = "normal", source: str = "v5") -> dict[str, float]:
    """断点3: 从最近教训中提取"热失效"指标，返回 {指标名: 惩罚系数}。

    逻辑：如果同一指标在最近 N 条同 regime + 同 source 错误预测中出现 ≥ M 次，
    说明该指标在当前行情下系统性地失效，额外乘以 LESSON_HOT_PENALTY（0.70）。

    v6: 加入 source 过滤 — 只在同 source 的教训中检测热失效，
    避免 AI 时代的失效指标被误判为 v5 的热失效。
    """
    if not LESSONS_FILE.exists():
        return {}
    try:
        data = json.loads(LESSONS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    lessons = data.get("lessons", [])
    if not isinstance(lessons, list) or len(lessons) < LESSON_HOT_THRESHOLD:
        return {}

    # 按 regime + source 过滤：只看同 regime + 同 source 的教训
    same_filter = [l for l in lessons
                   if l.get("regime", "normal") == regime
                   and l.get("source", "legacy") == source]
    if len(same_filter) < LESSON_HOT_THRESHOLD:
        return {}

    recent = same_filter[-LESSON_HOT_WINDOW:]  # 最近 N 条同 regime + source 教训
    indicator_counts: dict[str, int] = {}
    for l in recent:
        for ind in l.get("wrong_indicators", []):
            indicator_counts[ind] = indicator_counts.get(ind, 0) + 1

    hot: dict[str, float] = {}
    for ind, count in indicator_counts.items():
        if count >= LESSON_HOT_THRESHOLD:
            hot[ind] = LESSON_HOT_PENALTY
    return hot


def _load_prev_weights() -> dict[str, float]:
    """加载上一次的权重（用于平滑变化）"""
    if not WEIGHTS_CACHE_FILE.exists():
        return {}
    try:
        data = json.loads(WEIGHTS_CACHE_FILE.read_text(encoding="utf-8"))
        return {k: float(v) for k, v in data.get("weights", {}).items()}
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return {}


def _save_current_weights(weights: dict[str, float]) -> None:
    """原子写入当前权重（供下次平滑用）。加 fcntl 排他锁防并发写。"""
    path = WEIGHTS_CACHE_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        with open(lock_path, "w", encoding="utf-8") as lf:
            if _HAS_FCNTL:
                _flock_mod.flock(lf.fileno(), _flock_mod.LOCK_EX)
            else:
                _pocker.lock(lf, _pocker.LOCK_EX)
            try:
                payload = {
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "weights": weights,
                }
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp.replace(path)
            finally:
                if _HAS_FCNTL:
                    _flock_mod.flock(lf.fileno(), _flock_mod.LOCK_UN)
    except OSError:
        pass


def load_indicator_weights(regime: str = "normal", source: str = "v5") -> dict[str, float]:
    """加载所有指标的权重系数。出错或无数据返回空 dict（调用方默认 1.0）。

    v6 改动（2026-08-02）: 按 source 隔离读取，解决 AI 时代 correct 稀释 v5 时代 wrong 的问题。
    - 优先读当前 source 桶（默认 v5）
    - source 桶样本不足（<MIN_SAMPLES）时，合并 legacy 桶（旧数据）
    - 若合并后仍不足，跳过该指标（调用方默认 1.0）

    Args:
        regime: 行情 regime 桶（trending/ranging/normal/extreme_sentiment/unknown）。
        source: 信号来源桶（v5/AI/fallback/legacy），默认 v5。
    """
    now = time.time()
    if (now - _cache["ts"] < CACHE_TTL_SECONDS and _cache["weights"]
            and _cache.get("regime") == regime and _cache.get("source") == source):
        return _cache["weights"]

    weights: dict[str, float] = {}
    if not LESSONS_FILE.exists():
        _cache["ts"] = now
        _cache["weights"] = weights
        _cache["regime"] = regime
        _cache["source"] = source
        return weights

    try:
        data = json.loads(LESSONS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        _cache["ts"] = now
        _cache["weights"] = weights
        _cache["regime"] = regime
        _cache["source"] = source
        return weights

    patterns = data.get("patterns", {})
    # v4: 优先使用 regime_patterns（如果有），兼容旧格式 patterns
    regime_patterns = data.get("regime_patterns", {})
    if isinstance(regime_patterns, dict) and regime_patterns:
        patterns = regime_patterns
    if not isinstance(patterns, dict):
        _cache["ts"] = now
        _cache["weights"] = weights
        _cache["regime"] = regime
        _cache["source"] = source
        return weights

    prev_weights = _load_prev_weights()

    for name, stat in patterns.items():
        if not isinstance(stat, dict):
            continue
        # 兼容旧格式:stat 是 {"correct":..,"wrong":..} 且无任何 regime 子桶
        if "correct" in stat and "wrong" in stat and not any(
            k in stat for k in ("trending", "ranging", "normal", "extreme_sentiment", "unknown")
        ):
            bucket = stat
        else:
            regime_bucket = stat.get(regime, stat.get("normal", {}))
            if not isinstance(regime_bucket, dict):
                continue
            # v6: regime_bucket 可能是 {correct, wrong}（旧格式）或 {source: {correct, wrong}}（新格式）
            if "correct" in regime_bucket or "wrong" in regime_bucket:
                # 旧格式：直接用
                bucket = regime_bucket
            else:
                # 新格式：按 source 隔离
                source_bucket = regime_bucket.get(source, {})
                source_c = int(source_bucket.get("correct", 0)) if isinstance(source_bucket, dict) else 0
                source_w = int(source_bucket.get("wrong", 0)) if isinstance(source_bucket, dict) else 0
                source_total = source_c + source_w

                if source_total >= MIN_SAMPLES:
                    # 当前 source 样本充足，只用 source 桶（不被旧数据稀释）
                    correct = source_c
                    wrong = source_w
                else:
                    # 样本不足，合并 legacy 桶（旧数据 fallback）
                    legacy_bucket = regime_bucket.get("legacy", {})
                    legacy_c = int(legacy_bucket.get("correct", 0)) if isinstance(legacy_bucket, dict) else 0
                    legacy_w = int(legacy_bucket.get("wrong", 0)) if isinstance(legacy_bucket, dict) else 0
                    correct = source_c + legacy_c
                    wrong = source_w + legacy_w

                total = correct + wrong
                if total < MIN_SAMPLES:
                    continue  # 合并后仍不足，跳过

                wilson_lb = _wilson_lower_bound(correct, wrong)
                target_weight = _accuracy_to_weight(wilson_lb)

                # 平滑变化
                prev = prev_weights.get(name)
                if prev is not None:
                    delta = target_weight - prev
                    if abs(delta) > MAX_WEIGHT_DELTA:
                        target_weight = prev + (MAX_WEIGHT_DELTA if delta > 0 else -MAX_WEIGHT_DELTA)

                weights[name] = round(target_weight, 3)
                continue  # 跳过下面的旧格式 bucket 读取

        # 旧格式 bucket 读取（{correct, wrong}）
        correct = int(bucket.get("correct", 0))
        wrong = int(bucket.get("wrong", 0))
        total = correct + wrong
        if total < MIN_SAMPLES:
            continue

        wilson_lb = _wilson_lower_bound(correct, wrong)
        target_weight = _accuracy_to_weight(wilson_lb)

        # 平滑变化
        prev = prev_weights.get(name)
        if prev is not None:
            delta = target_weight - prev
            if abs(delta) > MAX_WEIGHT_DELTA:
                target_weight = prev + (MAX_WEIGHT_DELTA if delta > 0 else -MAX_WEIGHT_DELTA)

        weights[name] = round(target_weight, 3)

    # 断点3: 热失效指标额外惩罚
    hot_failures = _get_hot_failure_indicators(regime, source)
    if hot_failures:
        for ind, penalty in hot_failures.items():
            if ind in weights:
                old_w = weights[ind]
                new_w = round(max(WEIGHT_FLOOR, old_w * penalty), 3)
                weights[ind] = new_w
                if new_w < old_w:
                    _cache.setdefault("_hot_penalties", {})[ind] = {
                        "from": old_w, "to": new_w, "penalty": penalty}
                elif new_w == old_w and old_w * penalty < old_w:
                    hp = _cache.setdefault("_hot_penalties", {})
                    if ind not in hp:
                        hp[ind] = {"from": old_w, "to": old_w, "penalty": penalty,
                                   "note": "已触底，惩罚无实际效果"}

    _cache["ts"] = now
    _cache["weights"] = weights
    _cache["regime"] = regime
    _cache["source"] = source

    if weights != prev_weights:
        _save_current_weights(weights)

    return weights


def force_refresh(regime: str = "normal", source: str = "v5") -> dict[str, float]:
    """强制刷新权重缓存（逐笔即时反馈用）。
    复盘处理完一笔交易后调用此函数，立即重新计算并保存权重，
    确保下个小时的分析使用最新反馈。

    v6: 加 source 参数，按当前 source 桶刷新权重。
    """
    _cache["ts"] = 0.0  # 清除缓存，强制重新计算
    return load_indicator_weights(regime, source)


def get_indicator_weight(name: str) -> float:
    """获取单个指标权重，默认 1.0"""
    return load_indicator_weights().get(name, 1.0)


def get_weights_summary() -> dict[str, Any]:
    """返回权重摘要（供日志/看板展示）"""
    weights = load_indicator_weights()
    if not LESSONS_FILE.exists():
        return {"source": "missing", "weights": {}, "note": "无复盘文件，全部默认权重 1.0"}

    try:
        data = json.loads(LESSONS_FILE.read_text(encoding="utf-8"))
        # v4: 优先使用 regime_patterns，兼容旧格式 patterns
        patterns = data.get("regime_patterns", {}) or data.get("patterns", {})
    except (json.JSONDecodeError, OSError):
        return {"source": "corrupted", "weights": {}, "note": "复盘文件损坏"}

    summary: dict[str, Any] = {}
    for name, stat in patterns.items():
        if not isinstance(stat, dict):
            continue
        # v4: regime_patterns 格式 {indicator: {regime: {correct, wrong}}}
        # 汇总所有 regime 的 correct/wrong
        total_correct, total_wrong = 0, 0
        has_regime_buckets = any(k in stat for k in ("trending", "ranging", "normal"))
        if has_regime_buckets:
            for regime_bucket in stat.values():
                if isinstance(regime_bucket, dict):
                    total_correct += int(regime_bucket.get("correct", 0))
                    total_wrong += int(regime_bucket.get("wrong", 0))
        else:
            # 旧格式 {indicator: {correct, wrong}}
            total_correct = int(stat.get("correct", 0))
            total_wrong = int(stat.get("wrong", 0))
        total = total_correct + total_wrong
        acc = total_correct / total if total > 0 else 0.0
        wilson_lb = _wilson_lower_bound(total_correct, total_wrong) if total > 0 else 0.5
        summary[name] = {
            "correct": total_correct,
            "wrong": total_wrong,
            "accuracy": round(acc, 3),
            "wilson_lower": round(wilson_lb, 3),
            "weight": weights.get(name, 1.0),
            "samples": total,
            "adjusted": name in weights,
        }

    # 断点3: 标注热失效惩罚
    hot_penalties = _cache.get("_hot_penalties", {})
    for ind, info in hot_penalties.items():
        if ind in summary:
            summary[ind]["hot_failure"] = True
            summary[ind]["hot_penalty_from"] = info["from"]
            summary[ind]["hot_penalty_mult"] = info["penalty"]

    return {"source": "review-lessons", "weights": summary,
            "note": f"共 {len(weights)} 个指标已调整权重",
            "hot_failures": list(hot_penalties.keys()) if hot_penalties else []}


# ============================================================
# Phase 3: 组合效应分析
# ============================================================

MIN_PAIR_SAMPLES = 10        # 组合至少需要 10 个样本才参与调整
PAIR_BASELINE_ACC = 0.55     # 组合准确率基准线（超过此值视为正效应）
PAIR_BOOST_MAX = 0.08        # 组合正效应最大加成（+8% 置信度）
PAIR_PENALTY_MAX = 0.08      # 组合负效应最大惩罚（-8% 置信度）


def _make_pair_key(indicators: list[str]) -> list[str]:
    """生成指标的所有两两组合键（排序后连接）"""
    if len(indicators) < 2:
        return []
    sorted_inds = sorted(set(indicators))
    pairs = []
    for i in range(len(sorted_inds)):
        for j in range(i + 1, len(sorted_inds)):
            pairs.append(f"{sorted_inds[i]}&{sorted_inds[j]}")
    return pairs


def get_pair_adjustment(indicators: list[str], regime: str = "normal") -> float:
    """Phase 3: 根据当前活跃指标组合的历史准确率，返回置信度微调因子。

    Args:
        indicators: 当前信号中活跃的指标列表
        regime: 当前行情 regime

    Returns:
        调整因子：>1.0 表示组合效应正面（提升置信度），<1.0 表示负面（降低置信度）
        1.0 表示无足够数据或无显著效应
    """
    if len(indicators) < 2:
        return 1.0

    if not LESSONS_FILE.exists():
        return 1.0

    try:
        data = json.loads(LESSONS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 1.0

    pair_patterns = data.get("pair_patterns", {})
    if not isinstance(pair_patterns, dict) or not pair_patterns:
        return 1.0

    active_pairs = _make_pair_key(indicators)
    if not active_pairs:
        return 1.0

    adjustments = []
    for pair in active_pairs:
        pair_data = pair_patterns.get(pair, {})
        if not isinstance(pair_data, dict):
            continue
        # 优先取当前 regime 的数据，否则取 normal，否则跳过
        bucket = pair_data.get(regime, pair_data.get("normal"))
        if not isinstance(bucket, dict):
            continue
        correct = int(bucket.get("correct", 0))
        wrong = int(bucket.get("wrong", 0))
        total = correct + wrong
        if total < MIN_PAIR_SAMPLES:
            continue

        acc = correct / total
        if acc > PAIR_BASELINE_ACC:
            # 正效应：组合准确率高于基准 → 提升置信度
            # 效应大小与 (acc - baseline) 成正比，上限 PAIR_BOOST_MAX
            boost = min(PAIR_BOOST_MAX, (acc - PAIR_BASELINE_ACC) * 0.4)
            adjustments.append(1.0 + boost)
        elif acc < (1.0 - PAIR_BASELINE_ACC):
            # 负效应：组合准确率低于 45% → 降低置信度
            penalty = min(PAIR_PENALTY_MAX, ((1.0 - PAIR_BASELINE_ACC) - acc) * 0.4)
            adjustments.append(1.0 - penalty)

    if not adjustments:
        return 1.0

    # 多个组合的效应取平均（避免叠加过度）
    return round(sum(adjustments) / len(adjustments), 4)


# ============================================================
# Phase 4: 行情突变预警 — 健康度历史追踪 + 熔断
# ============================================================

HEALTH_HISTORY_FILE = THIS_DIR / "data" / "hermes_health_history.json"
HEALTH_HISTORY_MAX = 20          # 保留最近 20 条健康度记录
HEALTH_DEGRADE_WARN = 0.12       # 健康度下降 12% → 警告
HEALTH_DEGRADE_MELTDOWN = 0.20   # 健康度下降 20% → 熔断（强制 SKIP）
HEALTH_DEGRADE_LOOKBACK = 3      # 看最近 3 次权重更新


def _load_health_history() -> list[dict]:
    """加载健康度历史"""
    if not HEALTH_HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(HEALTH_HISTORY_FILE.read_text(encoding="utf-8"))
        return data.get("history", []) if isinstance(data, dict) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_health_history(history: list[dict]) -> None:
    """保存健康度历史"""
    try:
        HEALTH_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    payload = {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "history": history[-HEALTH_HISTORY_MAX:],
    }
    tmp = HEALTH_HISTORY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(HEALTH_HISTORY_FILE)


def record_health_snapshot(weights: dict[str, float], regime: str) -> float:
    """记录当前健康度快照，返回整体健康度分数。

    健康度 = 所有已调整权重的平均值（未调整的按 1.0 计）。
    如果没有任何权重被调整，返回 1.0。
    """
    if not weights:
        return 1.0
    health = sum(weights.values()) / len(weights) if weights else 1.0
    health = round(health, 4)

    history = _load_health_history()
    history.append({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "health": health,
        "regime": regime,
        "n_indicators": len(weights),
    })
    _save_health_history(history)
    return health


def check_health_degradation() -> dict:
    """Phase 4: 检查健康度是否快速退化，返回熔断状态。

    Returns:
        {
            "status": "normal" | "warn" | "meltdown",
            "current_health": float,
            "degradation": float,        # 负值表示下降幅度
            "trend": "stable" | "declining" | "improving",
            "message": str,
        }
    """
    history = _load_health_history()
    if len(history) < HEALTH_DEGRADE_LOOKBACK:
        return {"status": "normal", "current_health": 1.0, "degradation": 0.0,
                "trend": "stable", "message": "健康度历史不足，跳过检测"}

    recent = history[-HEALTH_DEGRADE_LOOKBACK:]
    current = recent[-1]["health"]
    oldest = recent[0]["health"]
    degradation = current - oldest  # 负值 = 下降

    # 趋势判断
    if degradation > 0.02:
        trend = "improving"
    elif degradation < -HEALTH_DEGRADE_WARN:
        trend = "declining"
    else:
        trend = "stable"

    # 熔断判断
    if degradation <= -HEALTH_DEGRADE_MELTDOWN:
        return {
            "status": "meltdown",
            "current_health": current,
            "degradation": round(degradation, 4),
            "trend": trend,
            "message": (f"⚠️ 熔断：健康度从 {oldest:.3f} 骤降至 {current:.3f} "
                        f"({degradation:+.1%})，指标权重快速失效，建议全 SKIP"),
        }
    elif degradation <= -HEALTH_DEGRADE_WARN:
        return {
            "status": "warn",
            "current_health": current,
            "degradation": round(degradation, 4),
            "trend": trend,
            "message": (f"⚡ 警告：健康度从 {oldest:.3f} 降至 {current:.3f} "
                        f"({degradation:+.1%})，行情可能突变，降低置信度"),
        }

    return {
        "status": "normal",
        "current_health": current,
        "degradation": round(degradation, 4),
        "trend": trend,
        "message": f"健康度稳定 ({current:.3f}, {trend})",
    }


if __name__ == "__main__":
    summary = get_weights_summary()
    print(f"来源: {summary['source']}")
    print(f"说明: {summary['note']}")
    print()
    print(f"{'指标':<16} {'准确率':>8} {'Wilson':>8} {'样本':>6} {'权重':>6} {'状态':<8}")
    print("-" * 60)
    for name, info in sorted(summary["weights"].items(), key=lambda kv: kv[1]["wilson_lower"]):
        status = "已调整" if info["adjusted"] else f"<{MIN_SAMPLES}样本"
        print(f"{name:<16} {info['accuracy']*100:>7.1f}% {info['wilson_lower']*100:>7.1f}% {info['samples']:>6} {info['weight']:>6.2f} {status:<8}")
