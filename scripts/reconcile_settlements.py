#!/usr/bin/env python3
"""结算校正脚本 — 复核全部已复盘记录，识别被"outcomePrices>0.9 实时价"误判的场次。

背景（2026-08-14 修复）:
  原 hermes_review.py::get_actual_result 仅凭 outcomePrices>0.9 判"已归1=已赢"，
  盘中一侧领先至 90%+ 会被误当结算价（如 8/14 3am 场 UP 实时价 0.925 被误判已赢，
  实际 DOWN 赢）。本脚本用 GAMMA endDate+最终结算价 / 币安回溯重新判定真实结果，
  识别 recorded_actual != true_actual 的误判场。

用法:
  python3 scripts/reconcile_settlements.py            # 只扫描，输出误判清单（dry-run）
  python3 scripts/reconcile_settlements.py --apply    # 按真实结果修正 results/stats/lessons/review-log

说明:
  - dry-run 模式不修改任何数据文件。
  - --apply 会对误判场逐条修正，并重建 stats（从修正后 results 重算）。
  - GAMMA 只保留最近 slug，历史 slug 用币安回溯（anchor vs 时段末收盘价）兜底。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import hermes_common as hc  # noqa: E402
from hermes_review import (  # noqa: E402
    _binance_settle,
    _parse_end_utc,
    DATA_DIR,
    RESULTS,
    REVIEW_LOG,
    LESSONS_FILE,
    STATS_FILE,
    REVIEWED_FILE,
    get_actual_result,
    update_stats,
    update_lessons,
    analyze_indicators,
    load_stats,
    save_stats,
    load_lessons,
    save_lessons,
    write_review_log,
)

# 输出报告
REPORT = DATA_DIR / "hermes_settlement_reconcile_20260814.md"


def load_done_signal_map() -> dict[str, dict]:
    """done.jsonl → {order_id: signal} 与 {slug: signal} 映射。"""
    by_oid: dict[str, dict] = {}
    by_slug: dict[str, dict] = {}
    done_file = DATA_DIR / "hermes_done.jsonl"
    if not done_file.exists():
        return by_oid, by_slug
    for line in done_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        sig = e.get("signal", {})
        res = e.get("result", {})
        oid = res.get("order_id")
        slug = sig.get("slug", "")
        if oid:
            by_oid[oid] = sig
        if slug:
            by_slug[slug] = sig
    return by_oid, by_slug


def load_results() -> list[dict]:
    rows = []
    if not RESULTS.exists():
        return rows
    for line in RESULTS.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def recompute_pnl(rec: dict, signal: dict, true_actual: str) -> float:
    """按真实结果重算 pnl（与 hermes_review.main 同口径）。"""
    side = signal.get("side", rec.get("side", ""))
    amount = float(signal.get("amount", rec.get("amount", 0)) or 0)
    buy_price = float(rec.get("buy_price") or signal.get("market_price", 0) or signal.get("max_price", 0) or 0)
    if buy_price <= 0:
        buy_price = 0.5
    if side == true_actual:
        return round(amount * (1.0 / buy_price - 1.0), 2)
    return round(-amount, 2)


def scan(apply: bool = False) -> int:
    rows = load_results()
    by_oid, by_slug = load_done_signal_map()
    print(f"加载 {len(rows)} 条已复盘记录")

    mismatches = []      # recorded_actual != true_actual
    verified = []        # 一致
    unsettled = []       # 无法判定（GAMMA 无数据 + 币安无锚点等）

    # GAMMA 查询缓存，避免重复请求
    gamma_cache: dict[str, dict | None] = {}

    for rec in rows:
        slug = rec.get("slug", "")
        oid = rec.get("order_id", "")
        sig = by_oid.get(oid) or by_slug.get(slug) or {}
        anchor = sig.get("anchor_price")
        sig_ts = sig.get("timestamp", "")
        recorded = rec.get("actual", "")

        # 复用修复后的 get_actual_result（含 endDate 时间守卫 + GAMMA 结算价 + 币安兜底）
        true_actual, desc = get_actual_result(slug, anchor, sig_ts)
        if true_actual not in ("UP", "DOWN"):
            unsettled.append((slug, recorded, desc))
            continue

        if true_actual != recorded:
            new_pnl = recompute_pnl(rec, sig, true_actual)
            mismatches.append({
                "slug": slug, "oid": oid, "side": sig.get("side") or rec.get("side"),
                "recorded": recorded, "true": true_actual, "desc": desc,
                "amount": sig.get("amount"), "buy_price": rec.get("buy_price"),
                "old_pnl": rec.get("pnl"), "new_pnl": new_pnl,
            })
        else:
            verified.append((slug, recorded, desc))

    print(f"一致: {len(verified)} | 误判: {len(mismatches)} | 无法判定: {len(unsettled)}")
    for u in unsettled:
        print(f"  ? {u[0].split('bitcoin-up-or-down-')[-1]} recorded={u[1]} desc={u[2]}")

    if mismatches:
        print("\n=== 误判清单 ===")
        for m in mismatches:
            print(f"  ❌ {m['slug'].split('bitcoin-up-or-down-')[-1]:28s} "
                  f"记录={m['recorded']} 真实={m['true']} | pnl {m['old_pnl']}→{m['new_pnl']} | {m['desc']}")

    # 写报告
    _write_report(rows, verified, mismatches, unsettled)

    if apply:
        # 无论有无误判都全量重建 stats（8/14: session 改为信号时刻推导，
        # 第一次重建用的 fallback 复盘时刻逻辑已废弃，必须重算）
        apply_fixes(rows, by_oid, mismatches)
        if mismatches:
            print(f"\n✅ 已修正 {len(mismatches)} 条误判记录并重建统计")
        else:
            print("\n无误判；已按信号时刻推导重建 by_session 统计")
    return 0


def _write_report(rows, verified, mismatches, unsettled):
    lines = []
    lines.append("# Hermes 结算校正报告（2026-08-14）\n")
    lines.append(f"- 总记录: {len(rows)}")
    lines.append(f"- 一致: {len(verified)}")
    lines.append(f"- **误判: {len(mismatches)}**")
    lines.append(f"- 无法判定: {len(unsettled)}\n")
    lines.append("## 误判清单\n")
    if mismatches:
        lines.append("| slug | side | 记录actual | 真实actual | 旧pnl | 新pnl | 依据 |")
        lines.append("|---|---|---|---|---|---|---|")
        for m in mismatches:
            lines.append(f"| {m['slug']} | {m['side']} | {m['recorded']} | {m['true']} | "
                         f"{m['old_pnl']} | {m['new_pnl']} | {m['desc']} |")
    else:
        lines.append("无。\n")
    lines.append("\n## 无法判定\n")
    for u in unsettled:
        lines.append(f"- {u[0]} recorded={u[1]} desc={u[2]}")
    lines.append("")
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告已写入: {REPORT}")


def apply_fixes(rows, by_oid, mismatches):
    """修正 results/stats/lessons/review-log。stats 从修正后的 results 全量重算。

    8/14: review-log 修正改为由 results 的 _reconciled 标记驱动（而非 mismatches），
    保证二次运行（mismatches 已空）也能把 review-log 修正到位。
    """
    mmap = {m["slug"]: m for m in mismatches}

    # 1) 修正 results.jsonl：逐行更新 actual/pnl（幂等：已 reconciled 的不重复标记）
    new_lines = []
    for line in RESULTS.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            new_lines.append(line)
            continue
        m = mmap.get(rec.get("slug"))
        if m:
            rec["actual"] = m["true"]
            rec["pnl"] = m["new_pnl"]
            rec["_reconciled"] = True
            rec["_reconciled_ts"] = datetime.now(timezone.utc).isoformat()
        new_lines.append(json.dumps(rec, ensure_ascii=False))
    RESULTS.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    print(f"  已修正 results.jsonl（{len(mmap)} 条）")

    # 2) 从修正后的 results 全量重算 stats
    fixed_rows = [json.loads(l) for l in new_lines]
    _rebuild_stats(fixed_rows, by_oid)
    print("  已重建 review-stats.json")

    # 3) 全量重建 lessons（清除重复/残留，patterns 与 results 一致）
    _fix_lessons(mmap, by_oid)
    print("  已全量重建 review-lessons.json")

    # 4) 修正 review-log.md（由 _reconciled 标记驱动，幂等）
    _fix_review_log(fixed_rows, by_oid)
    print("  已修正 review-log.md")


def _sig_session(sig: dict) -> str:
    """从信号 timestamp 推导时段（历史 signal 无 session 字段，
    fallback 用复盘时刻会失真——8/14 修复: 用信号本身 ET 时刻）。"""
    from hermes_review import get_session_name
    ts = (sig.get("timestamp") or "").strip()
    if ts:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return get_session_name(dt.hour)
        except Exception:
            pass
    return ""


def _rebuild_stats(fixed_rows, by_oid):
    """从修正后的 results 全量重算 stats（保持与 update_stats 同口径）。

    8/14 修复: session 用信号 timestamp 推导，避免 fallback 复盘时刻
    导致 by_session 全归当前时段（历史 bug）。
    """
    stats = load_stats()
    # 清空重新统计
    stats["total"] = 0
    stats["wins"] = 0
    stats["losses"] = 0
    stats["profit_loss"] = 0.0
    stats["by_side"] = {}
    stats["by_session"] = {}
    stats["by_confidence"] = {}
    stats["streak"] = {"current": 0, "max_win": 0, "max_loss": 0}

    # 按 ts 排序（保持时间顺序，streak 才正确）
    def _key(r):
        try:
            return datetime.fromisoformat(r.get("ts", "").replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0
    for rec in sorted(fixed_rows, key=_key):
        slug = rec.get("slug", "")
        sig = by_oid.get(rec.get("order_id", "")) or {}
        if not sig:
            # 从 results 自身字段构造最小 signal
            sig = {"slug": slug, "side": rec.get("side"), "amount": rec.get("amount"),
                   "probability": 50, "session": "", "timestamp": rec.get("ts", "")}
        signal = dict(sig)
        signal["slug"] = slug
        signal.setdefault("side", rec.get("side"))
        # 8/14 修复: 用信号时刻推导 session，避免 fallback 复盘时刻失真
        if not signal.get("session"):
            session = _sig_session(signal)
            if session:
                signal["session"] = session
        actual = rec.get("actual", "")
        prediction_correct = (signal.get("side") == actual)
        update_stats(stats, signal, actual, prediction_correct, rec.get("pnl", 0))
    save_stats(stats)


def _fix_lessons(mmap, by_oid):
    """全量重建 lesson 规则库（8/14 修复）。

    旧实现"手动 pop _processed_slugs + 过滤列表"破坏了 update_lessons 内部
    基于 _processed_slugs 的回滚机制，导致旧条目残留 + 新条目重复（18 个 slug
    出现 2 条 lesson）。本版本直接从空结构按时间顺序重放全部已修正 results，
    patterns/regime_patterns/pair_patterns/lessons/_processed_slugs 全部一致。
    """
    lessons = {"lessons": [], "patterns": {}, "regime_patterns": {},
               "pair_patterns": {}, "_processed_slugs": {}}

    def _key(r):
        try:
            return datetime.fromisoformat(r.get("ts", "").replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0

    rows = sorted(load_results(), key=_key)
    for rec in rows:
        sig = by_oid.get(rec.get("order_id", "")) or {}
        if not sig:
            continue
        actual = rec.get("actual", "")
        if actual not in ("UP", "DOWN"):
            continue
        # 8/14: 历史 signal 的 reason 可能是 list（不同版本写入），analyze_indicators 需 str
        if isinstance(sig.get("reason"), list):
            sig = dict(sig)
            sig["reason"] = " ".join(str(x) for x in sig["reason"])
        correct, wrong, analysis = analyze_indicators(sig, actual)
        update_lessons(lessons, sig, actual, correct, wrong)
    save_lessons(lessons)


def _fix_review_log(fixed_rows, by_oid):
    """修正 review-log.md 中被误判 slug 的"实际/结果"行（由 _reconciled 标记驱动，幂等）。

    格式: 每个条目以 "##  | <slug>" 开头，含 "**实际**: ..." 与 "**结果**: ..." 两行。
    修正规则:
      **实际**: 改为 "<真实方向> | 结算校正 (8/14)"，保留原描述前缀一致性。
      **结果**: 按 prediction_correct 重写 ✅/❌。
    对非 reconciled 条目原样保留（避免破坏无关历史）。
    """
    if not REVIEW_LOG.exists():
        return
    text = REVIEW_LOG.read_text(encoding="utf-8")

    # 需要修正的 slug: results 中带 _reconciled 标记
    fix_map = {}
    for rec in fixed_rows:
        if not rec.get("_reconciled"):
            continue
        slug = rec.get("slug", "")
        true_actual = rec.get("actual", "")
        if true_actual not in ("UP", "DOWN"):
            continue
        sig = by_oid.get(rec.get("order_id", "")) or {}
        side = sig.get("side") or rec.get("side", "")
        fix_map[slug] = {"true": true_actual, "side": side}

    if not fix_map:
        return

    lines = text.split("\n")
    out = []
    in_block = None
    for line in lines:
        # 兼容两种标题格式: "##  | <slug>" 与 "## <timestamp> | <slug>"
        if line.startswith("## "):
            after = line[3:]
            if after.startswith("| "):
                in_block = after[2:].strip()
            elif " | " in after:
                in_block = after.split(" | ", 1)[1].strip()
            else:
                in_block = None
        if in_block in fix_map:
            info = fix_map[in_block]
            if line.startswith("**实际**:"):
                out.append(f"**实际**: {info['true']} | 结算校正 (8/14 reconcile)")
                continue
            if line.startswith("**结果**:"):
                correct = (info["side"] == info["true"])
                mark = "✅ 预测正确" if correct else "❌ 预测错误"
                out.append(f"**结果**: {mark} (8/14 校正)")
                continue
        out.append(line)
    REVIEW_LOG.write_text("\n".join(out), encoding="utf-8")
    print(f"  已修正 review-log.md（{len(fix_map)} 个 slug）")


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    sys.exit(scan(apply=apply))
