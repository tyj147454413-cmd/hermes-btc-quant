#!/usr/bin/env python3
"""
Hermes BTC 候选规则管道核心（v1，P0-1 落地）。

职责：
1. 加载/校验/原子追加候选到 data/candidate_patches.json
2. 状态机迁移（pending → proposed → accepted → deployed → monitoring → kept/reverted，任意 → rejected）
3. 目标文件 allowlist 校验（脚本白名单；prompt_exec.md / prompt.md 永不允许）
4. ID 幂等（同 slug+strategy_version+hash 的候选重复入队不产生新记录）
5. 历史流水自动追加（每次状态迁移写入 history[]）

配套：
- JSON Schema 权威定义：scripts/candidate_schema.json
- 架构合约：docs/hermes_learning_architecture.md §6 / docs/hermes_candidate_pipeline_plan.md（plan-v1 锚点）
- 上游：hermes_review.py（P1-1 入队调用 enqueue_review_lesson；本轮未接线）
- 下游：candidate_counterfactual.py（P1-2 回放，pending→proposed）/ candidate_approve.py（P1-3）/ candidate_monitoring.py（P1-5）

约束：
- 不修改任何规则文件（prompt_exec.md / rule_engine.py / signal_validator.py / bot / write_signal.py）
- 文件锁用 fcntl.flock（POSIX stdlib，无外部依赖）
- jsonschema 库若可用做完整 JSON Schema 校验；不可用时降级必填字段检查

用法（CLI 调试）：
    python3 scripts/candidate_pipeline.py --list
    python3 scripts/candidate_pipeline.py --show <candidate_id>
    python3 scripts/candidate_pipeline.py --enqueue-test
"""

from __future__ import annotations

import argparse
import contextlib
try:
    import fcntl
except ImportError:  # 9/2: Windows 无 fcntl，_file_lock 降级无锁
    fcntl = None
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ====================================================================
# 路径常量 + allowlist
# ====================================================================

# 沙箱兼容：优先 $HERMES_DIR（hermes_env.sh 导出），否则 macOS 绝对路径
_HERMES_DIR = os.environ.get("HERMES_DIR")
ROOT = Path(_HERMES_DIR) if _HERMES_DIR else Path("/Users/tangyijun/Desktop/hermes btc")
DATA_DIR = ROOT / "data"
STORE_FILE = DATA_DIR / "candidate_patches.json"
SCHEMA_FILE = Path(__file__).resolve().parent / "candidate_schema.json"

# 目标文件 allowlist（架构文档 §6.1 v1 关键变化 #5）
ALLOWED_TARGET_FILES = frozenset({
    "scripts/rule_engine.py",
    "scripts/signal_validator.py",
    "scripts/hermes_1h_bot.py",
    "scripts/write_signal.py",
})

# 状态机迁移白名单（架构文档 §6.2）
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "pending":    {"proposed", "rejected"},
    "proposed":   {"accepted", "rejected"},
    "accepted":   {"deployed", "rejected"},
    "deployed":   {"monitoring", "rejected", "reverted"},  # deployed 可直接 revert（部署即失败）
    "monitoring": {"kept", "reverted"},
    "kept":       set(),
    "reverted":   set(),
    "rejected":   set(),
}

ID_PATTERN = re.compile(r"^cp-(\d{4})-(\d{4})-(\d{3})$")

# ====================================================================
# JSON Schema 校验（兼容模式：有 jsonschema 用完整校验，无则降级必填检查）
# ====================================================================

try:
    import jsonschema  # type: ignore

    _HAVE_JSONSCHEMA = True
except ImportError:
    _HAVE_JSONSCHEMA = False


def _load_schema() -> dict[str, Any]:
    if not SCHEMA_FILE.exists():
        raise RuntimeError(f"JSON Schema 文件缺失：{SCHEMA_FILE}")
    return json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))


def _validate_required_fields(candidate: dict[str, Any]) -> list[str]:
    """降级校验：必填字段存在性 + 类型最简检查。无 jsonschema 时使用。"""
    errors: list[str] = []
    schema = _load_schema()
    for required in schema.get("required", []):
        if required not in candidate:
            errors.append(f"缺少必填字段：{required}")

    # 关键字段类型检查
    type_checks = {
        "id": str,
        "schema_version": str,
        "created_at_utc": str,
        "updated_at_utc": str,
        "layer": str,
        "verification_status": str,
    }
    for k, t in type_checks.items():
        if k in candidate and not isinstance(candidate[k], t):
            errors.append(f"字段 {k} 类型错误：期望 {t.__name__}，实际 {type(candidate[k]).__name__}")

    # ID 格式
    if "id" in candidate and not ID_PATTERN.match(candidate["id"]):
        errors.append(f"id 不匹配 cp-YYYY-MMDD-NNN：{candidate.get('id')}")

    # schema_version 锁定
    if candidate.get("schema_version") != "candidate-patch.v1":
        errors.append(f"schema_version 必须是 candidate-patch.v1：{candidate.get('schema_version')}")

    # layer 枚举
    if candidate.get("layer") not in {"统计层", "权重层", "结构层"}:
        errors.append(f"layer 必须是 统计层/权重层/结构层 之一：{candidate.get('layer')}")

    # verification_status 枚举
    if candidate.get("verification_status") not in ALLOWED_TRANSITIONS:
        errors.append(f"verification_status 非法：{candidate.get('verification_status')}")

    # allowlist 校验（架构文档 §6.1 #5）
    action = candidate.get("action") or {}
    target = action.get("target") or {}
    target_file = target.get("file")
    if target_file and target_file not in ALLOWED_TARGET_FILES:
        errors.append(f"action.target.file 不在 allowlist：{target_file}")

    # field_paths 必填（架构文档 §6.1 #2）
    cond = candidate.get("condition") or {}
    if not cond.get("field_paths"):
        errors.append("condition.field_paths 必填且非空（round34.1 教训：禁止模糊字段名）")

    # provenance_slugs 必填（架构文档 §6.1 #3）
    src = candidate.get("source") or {}
    if not src.get("provenance_slugs"):
        errors.append("source.provenance_slugs 必填且非空（审计可重定位）")

    # strategy_version + model_version 必填（架构文档 §6.1 #4）
    if not src.get("strategy_version"):
        errors.append("source.strategy_version 必填")
    if not src.get("model_version"):
        errors.append("source.model_version 必填")

    return errors


def validate_against_schema(candidate: dict[str, Any]) -> None:
    """校验候选；通过则静默，失败抛 ValueError。"""
    if _HAVE_JSONSCHEMA:
        try:
            jsonschema.validate(candidate, _load_schema())
            return
        except jsonschema.ValidationError as e:  # type: ignore
            raise ValueError(f"JSON Schema 校验失败：{e.message}") from e

    errors = _validate_required_fields(candidate)
    if errors:
        raise ValueError("候选校验失败：\n  - " + "\n  - ".join(errors))


# ====================================================================
# 文件锁（fcntl.flock，POSIX stdlib）
# ====================================================================

@contextlib.contextmanager
def _file_lock(lock_path: Path, mode: str = "exclusive"):
    """POSIX 文件锁上下文管理器。exclusive 阻塞等待；shared 多读。
    9/2: Windows 无 fcntl，降级为无锁直通（月度单机任务，无并发风险）。"""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX if mode == "exclusive" else fcntl.LOCK_SH)
        yield fd
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# ====================================================================
# 原子写入：write-to-temp + rename
# ====================================================================

def _atomic_write_json(path: Path, obj: Any) -> None:
    """写 JSON 到临时文件 + rename，原子替换。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ====================================================================
# 存储 CRUD
# ====================================================================

def load_candidate_store(path: Path = STORE_FILE) -> dict[str, Any]:
    """读取候选队列；缺失或损坏返回空 schema-v1 store。"""
    if not path.exists():
        return {
            "schema_version": "candidate-patches.v1",
            "updated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "candidates": [],
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        # 损坏文件不抛（架构文档 §12 失败处理）：返回空 + 备份原文件
        backup = path.with_suffix(f".corrupted.{int(datetime.now().timestamp())}.json")
        try:
            path.rename(backup)
            print(f"[WARN] 损坏候选队列已备份至 {backup}", file=sys.stderr)
        except OSError:
            pass
        raise ValueError(f"候选队列损坏（已备份）：{e}") from e

    # 版本兼容：旧 schema_version 自动迁移（目前 v1 是第一个，无需迁移代码；保留扩展点）
    if data.get("schema_version") != "candidate-patches.v1":
        # 未知版本：警告但接受
        print(f"[WARN] 候选队列 schema_version 未知：{data.get('schema_version')}",
              file=sys.stderr)
    return data


def _candidate_fingerprint(candidate: dict[str, Any]) -> str:
    """幂等键：layer + action.target.symbol + condition.expression + provenance_slugs[0]。"""
    cond = candidate.get("condition") or {}
    action = candidate.get("action") or {}
    target = action.get("target") or {}
    src = candidate.get("source") or {}
    parts = [
        candidate.get("layer", ""),
        target.get("symbol", ""),
        cond.get("expression", ""),
        (src.get("provenance_slugs") or [""])[0],
    ]
    raw = "|".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def find_duplicate(store: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any] | None:
    """同 fingerprint + status 非 rejected/kept/reverted 的候选视为重复（不创建新记录）。"""
    fp = _candidate_fingerprint(candidate)
    for c in store.get("candidates", []):
        if _candidate_fingerprint(c) != fp:
            continue
        if c.get("verification_status") in {"rejected", "kept", "reverted"}:
            continue
        return c
    return None


def _generate_id(store: dict[str, Any], now: datetime | None = None) -> str:
    """cp-YYYY-MMDD-NNN，NNN 为当日序号（从 001 起）。"""
    now = now or datetime.now(timezone.utc)
    yyyy = now.strftime("%Y")
    mmdd = now.strftime("%m%d")
    today_prefix = f"cp-{yyyy}-{mmdd}-"
    used = []
    for c in store.get("candidates", []):
        cid = c.get("id", "")
        if cid.startswith(today_prefix):
            m = ID_PATTERN.match(cid)
            if m:
                used.append(int(m.group(3)))
    nnn = (max(used) + 1) if used else 1
    return f"{today_prefix}{nnn:03d}"


def append_candidate(
    candidate: dict[str, Any],
    *,
    path: Path = STORE_FILE,
    lock_path: Path | None = None,
) -> str:
    """原子追加候选；返回 candidate_id。同 fingerprint 已存在则返回已有 ID（幂等）。"""
    if lock_path is None:
        lock_path = path.with_suffix(".lock")
    # 注入时间戳（不需要锁，先做）
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    candidate.setdefault("created_at_utc", now_iso)
    candidate["updated_at_utc"] = now_iso
    candidate.setdefault("verification_status", "pending")

    with _file_lock(lock_path, "exclusive"):
        store = load_candidate_store(path)

        # 幂等：同 fingerprint 已存在则跳过（不需 ID，跳过校验）
        existing = find_duplicate(store, candidate)
        if existing is not None:
            return existing["id"]

        # 分配 ID（必须在 validate 之前，schema 要求 id 必填）
        if "id" not in candidate:
            candidate["id"] = _generate_id(store)

        # 完整校验
        validate_against_schema(candidate)

        candidate.setdefault("history", []).append({
            "at_utc": now_iso,
            "from": None,
            "to": "pending",
            "actor": "candidate_pipeline.append_candidate",
            "reason": "新候选入队",
        })

        store.setdefault("candidates", []).append(candidate)
        store["updated_at_utc"] = now_iso
        _atomic_write_json(path, store)
    return candidate["id"]


def update_candidate(
    candidate_id: str,
    target_status: str,
    *,
    actor: str,
    reason: str,
    path: Path = STORE_FILE,
    lock_path: Path | None = None,
    expected_version: int | None = None,
    extra_patch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """状态机迁移；非法迁移抛 ValueError；并发保护。返回更新后的 candidate。"""
    if lock_path is None:
        lock_path = path.with_suffix(".lock")

    with _file_lock(lock_path, "exclusive"):
        store = load_candidate_store(path)
        for idx, c in enumerate(store.get("candidates", [])):
            if c.get("id") != candidate_id:
                continue
            cur = c.get("verification_status")
            if target_status not in ALLOWED_TRANSITIONS.get(cur, set()):
                raise ValueError(
                    f"非法状态迁移：{cur} → {target_status}（候选 {candidate_id}）。"
                    f"允许的迁移：{sorted(ALLOWED_TRANSITIONS.get(cur, set()))}"
                )
            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            c["verification_status"] = target_status
            c["updated_at_utc"] = now_iso
            c.setdefault("history", []).append({
                "at_utc": now_iso,
                "from": cur,
                "to": target_status,
                "actor": actor,
                "reason": reason,
            })
            if extra_patch:
                # 仅允许更新 verification_*/replay/monitoring/approval/deployment 等非核心字段
                ALLOWED_EXTRA_KEYS = {
                    "verification_evidence", "verification_detail",
                    "replay", "monitoring", "approval", "deployment",
                }
                for k, v in extra_patch.items():
                    if k in ALLOWED_EXTRA_KEYS:
                        c[k] = v
            store["updated_at_utc"] = now_iso
            _atomic_write_json(path, store)
            return c
        raise ValueError(f"候选不存在：{candidate_id}")


def load_candidate(candidate_id: str, path: Path = STORE_FILE) -> dict[str, Any] | None:
    """按 ID 加载候选；不存在返回 None。"""
    store = load_candidate_store(path)
    for c in store.get("candidates", []):
        if c.get("id") == candidate_id:
            return c
    return None


# ====================================================================
# 上游入口：复盘教训 → 候选观察（P1-1 hermes_review.py 调用，本轮未接线）
# ====================================================================

def enqueue_review_lesson(
    *,
    lesson: dict[str, Any],
    signal: dict[str, Any] | None,
    actual_side: str,
    candidate_id: str | None = None,
    path: Path = STORE_FILE,
) -> str | None:
    """
    将一条已结算复盘事实放入候选队列。

    入队策略（P0-1 仅占位，启发式待 P1-1 调试）：
    - 仅 weak_correct=True 或预测错误 或 AI-引擎方向不一致 时入队
    - 必须有 provenance_slugs
    - field_paths 由 caller 提供（lesson 里没有模糊字段名）

    返回 candidate_id；若 caller 已提供且 store 内不存在则返回 None（让 caller 处理）。
    """
    sig = signal or {}
    slug = sig.get("slug") or lesson.get("slug") or ""
    is_correct = lesson.get("is_correct")
    weak_correct = lesson.get("weak_correct", False)
    ai_engine_disagree = lesson.get("ai_engine_disagree", False)

    # 启发式：仅在值得立候选时入队
    if is_correct and not weak_correct and not ai_engine_disagree:
        return None

    strategy_version = sig.get("strategy_version", "unknown")
    model_version = sig.get("model_version", "unknown")

    candidate = {
        "schema_version": "candidate-patch.v1",
        "layer": lesson.get("layer", "统计层"),
        "source_type": "review",
        "source_event_ids": [lesson.get("lesson_id", "")] if lesson.get("lesson_id") else [],
        "condition": {
            "expression": lesson.get("expression", ""),
            "field_paths": lesson.get("field_paths", []),
            "operator": lesson.get("operator", "and"),
            "thresholds": lesson.get("thresholds", {}),
            "regime_filter": lesson.get("regime_filter"),
        },
        "action": {
            "type": lesson.get("action_type", "modify"),
            "target": {
                "file": lesson.get("target_file", "scripts/rule_engine.py"),
                "symbol": lesson.get("target_symbol", ""),
                "table": lesson.get("target_table"),
                "cell": lesson.get("target_cell"),
            },
            "before": lesson.get("before", {}),
            "after": lesson.get("after", {}),
            "expected_behavior": lesson.get("expected_behavior", []),
        },
        "source": {
            "slugs": [slug] if slug else [],
            "provenance_slugs": lesson.get("provenance_slugs") or ([slug] if slug else []),
            "source_lessons": [lesson.get("lesson_id", "")] if lesson.get("lesson_id") else [],
            "source_files": lesson.get("source_files", []),
            "strategy_version": strategy_version,
            "model_version": model_version,
        },
        "verification_status": "pending",
        "verification_evidence": "pending",
    }
    if candidate_id:
        candidate["id"] = candidate_id
    return append_candidate(candidate, path=path)


# ====================================================================
# CLI（调试用）
# ====================================================================

def _cmd_list(path: Path) -> int:
    store = load_candidate_store(path)
    cands = store.get("candidates", [])
    print(f"# 候选队列（{path}）")
    print(f"- schema_version: {store.get('schema_version')}")
    print(f"- updated_at_utc: {store.get('updated_at_utc')}")
    print(f"- candidates: {len(cands)}\n")
    if not cands:
        print("（空）")
        return 0
    counter = Counter(c.get("verification_status", "?") for c in cands)
    print("## 状态分布")
    for status, n in sorted(counter.items(), key=lambda x: -x[1]):
        print(f"  - {status}: {n}")
    print("\n## 候选列表")
    for c in cands:
        fp = _candidate_fingerprint(c)
        slugs = ", ".join((c.get("source", {}).get("provenance_slugs") or [])[:2])
        print(f"  - {c['id']} | {c.get('verification_status'):8s} | {c.get('layer')} | "
              f"{fp} | slugs=[{slugs}]")
    return 0


def _cmd_show(candidate_id: str, path: Path) -> int:
    c = load_candidate(candidate_id, path)
    if c is None:
        print(f"候选不存在：{candidate_id}", file=sys.stderr)
        return 1
    print(json.dumps(c, ensure_ascii=False, indent=2))
    return 0


def _cmd_enqueue_test(path: Path) -> int:
    """注入一个最小合法候选用于端到端验证。"""
    cid = append_candidate({
        "schema_version": "candidate-patch.v1",
        "layer": "统计层",
        "condition": {
            "expression": "abs(judge.net) in [0,2]",
            "field_paths": ["judge.net"],
            "operator": "and",
            "thresholds": {"judge.net_min": 0, "judge.net_max": 2},
            "regime_filter": None,
        },
        "action": {
            "type": "modify",
            "target": {
                "file": "scripts/rule_engine.py",
                "symbol": "_probability",
                "table": "probability_4d",
                "cell": "|net|0-2 × 非贴线 × 顺PM × all",
            },
            "before": {"probability": 0.933, "confidence": "高"},
            "after": {"probability": 0.676, "confidence": "低"},
            "expected_behavior": ["probability", "confidence", "position_scale"],
            "max_cells": 1,
            "max_weights": 0,
        },
        "source": {
            "slugs": ["bitcoin-up-or-down-test-2026-08-21-12pm-et"],
            "provenance_slugs": ["bitcoin-up-or-down-test-2026-08-21-12pm-et"],
            "source_files": ["data/review-log.md"],
            "strategy_version": "round37",
            "model_version": "claude-app-desktop",
        },
        "verification_status": "pending",
        "verification_evidence": "pending",
    }, path=path)
    print(f"已入队：{cid}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes BTC 候选规则管道（v1）")
    parser.add_argument("--list", action="store_true", help="列出所有候选")
    parser.add_argument("--show", metavar="CID", help="显示指定候选详情")
    parser.add_argument("--enqueue-test", action="store_true", help="注入测试候选（调试用）")
    parser.add_argument("--store", metavar="PATH", default=str(STORE_FILE), help="候选队列文件路径")
    parser.add_argument("--schema-check", metavar="PATH", help="校验指定 JSON 文件是否符合 schema")
    args = parser.parse_args(argv)

    path = Path(args.store)

    if args.list:
        return _cmd_list(path)
    if args.show:
        return _cmd_show(args.show, path)
    if args.enqueue_test:
        return _cmd_enqueue_test(path)
    if args.schema_check:
        try:
            data = json.loads(Path(args.schema_check).read_text(encoding="utf-8"))
            validate_against_schema(data)
            print(f"OK：{args.schema_check} 通过校验")
            return 0
        except Exception as e:
            print(f"FAIL：{e}", file=sys.stderr)
            return 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
