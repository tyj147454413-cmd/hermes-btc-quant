#!/usr/bin/env python3
"""
Hermes 样本外回测脚本（Out-of-Sample Backtest）。

只处理 2026-08-17 及之后的新快照（未参与 round 1-29 规则迭代的数据），
不改规则，纯观察胜率。用于验证系统是否过拟合。

用法：
    python3 scripts/backtest_oos.py                     # 全量样本外回测
    python3 scripts/backtest_oos.py --since 2026-08-18  # 指定起始日期
    python3 scripts/backtest_oos.py --daily             # 按日汇总
    python3 scripts/backtest_oos.py --save              # 回测 + 持久化到 oos_history.jsonl

输出：
    - 每日胜率、累计胜率
    - 按置信度/Regime/贴线模式分桶
    - 与样本内基线（73.1%）对比
    - 胜率跌破 70% / 60% 自动告警
    - --save 时写入 data/oos_history.jsonl 供 Dashboard 读取
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from signal_validator import extract_factors
from rule_engine import judge

THIS_DIR = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = THIS_DIR / "data" / "snapshots"
RESULTS_FILE = THIS_DIR / "data" / "hermes_results.jsonl"
OOS_HISTORY_FILE = THIS_DIR / "data" / "oos_history.jsonl"
OOS_ALERT_FILE = Path("/tmp/hermes_oos_alert.json")

# 样本内基线（round29 回测结果）
IN_SAMPLE_WIN_RATE = 73.1
IN_SAMPLE_TOTAL = 145

# 告警阈值
WARN_THRESHOLD = 70.0   # 低于此值黄色告警
ALERT_THRESHOLD = 60.0  # 低于此值红色告警
MIN_SAMPLES_FOR_ALERT = 10  # 至少 10 场才触发告警


def load_results() -> dict[str, str]:
    """从 hermes_results.jsonl 加载所有结算结果。"""
    results: dict[str, str] = {}
    if not RESULTS_FILE.exists():
        print("⚠️ 未找到结算结果文件", file=sys.stderr)
        return results
    for line in RESULTS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("actual"):
            results[r["slug"]] = r["actual"]
    return results


def parse_slug_date(slug: str) -> str | None:
    """从 slug 中提取日期，如 'bitcoin-up-or-down-august-17-2026-10am-et' → '2026-08-17'"""
    import re
    m = re.search(r'(january|february|march|april|may|june|july|august|september|october|november|december)-(\d+)-(\d{4})', slug)
    if not m:
        return None
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    }
    month = months.get(m.group(1))
    if month is None:
        return None
    day = int(m.group(2))
    year = int(m.group(3))
    return f"{year}-{month:02d}-{day:02d}"


def get_snapshots_since(since_date: str) -> dict[str, Path]:
    """获取指定日期及之后的快照，每个 slug 取最新。"""
    since = datetime.strptime(since_date, "%Y-%m-%d").date()
    slugs: dict[str, list[tuple[str, Path]]] = defaultdict(list)
    for p in sorted(SNAPSHOT_DIR.glob("*.json")):
        slug = p.name.split("_202")[0]
        date_str = parse_slug_date(slug)
        if date_str is None:
            continue
        file_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        if file_date >= since:
            slugs[slug].append((date_str, p))
    # 每个 slug 取最新快照
    result = {}
    for slug, entries in slugs.items():
        entries.sort(key=lambda x: x[1].stat().st_mtime)
        result[slug] = entries[-1][1]
    return result


def run_oos_backtest(since_date: str, daily: bool = False) -> tuple[list[dict], dict]:
    """运行样本外回测。返回 (逐笔记录, 汇总统计)。"""
    results = load_results()
    slugs = get_snapshots_since(since_date)

    if not slugs:
        print(f"⚠️ 未找到 {since_date} 及之后的快照")
        return [], {}

    records = []
    skipped_no_result = 0
    skipped_data_error = 0

    for slug, path in sorted(slugs.items()):
        actual = results.get(slug)
        if not actual:
            skipped_no_result += 1
            continue

        try:
            md = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            skipped_data_error += 1
            continue

        try:
            factors, inp, notes, mode = extract_factors(md)
        except Exception:
            skipped_data_error += 1
            continue

        # 两遍判定（与 comprehensive_backtest.py 口径一致）
        buy_wall = inp.pop("_buy_wall_ratio")
        sell_wall = inp.pop("_sell_wall_ratio")
        taker_bs_r = inp.pop("taker_bs_ratio", None)

        prelim = judge(dict(inp, extreme_wall=False))
        prelim_dir = prelim.final_direction
        if prelim_dir == "DOWN":
            extreme_wall = bool(sell_wall is not None and sell_wall > 3.0)
        elif prelim_dir == "UP":
            extreme_wall = bool(buy_wall is not None and buy_wall > 3.0)
        else:
            extreme_wall = False

        d = judge(dict(inp, extreme_wall=extreme_wall,
                       _buy_wall_ratio=buy_wall, _sell_wall_ratio=sell_wall,
                       taker_bs_ratio=taker_bs_r))

        if not d.final_direction:
            continue

        win = d.final_direction == actual
        date_str = parse_slug_date(slug) or "unknown"
        is_tie = bool(inp.get("_tie_mode"))
        regime = inp.get("regime", "?")

        records.append({
            "slug": slug,
            "date": date_str,
            "direction": d.final_direction,
            "actual": actual,
            "win": win,
            "net": d.net,
            "s0": d.s0,
            "confidence": d.confidence,
            "scale": d.position_scale,
            "is_tie": is_tie,
            "regime": regime,
            "probability": d.probability,
        })

    # 汇总统计
    total = len(records)
    wins = sum(1 for r in records if r["win"])
    oos_wr = wins / total * 100 if total else 0

    stats = {
        "total": total,
        "wins": wins,
        "losses": total - wins,
        "win_rate": oos_wr,
        "in_sample_baseline": IN_SAMPLE_WIN_RATE,
        "delta": round(oos_wr - IN_SAMPLE_WIN_RATE, 1),
        "skipped_no_result": skipped_no_result,
        "skipped_data_error": skipped_data_error,
    }

    return records, stats


def print_summary(records: list[dict], stats: dict, daily: bool):
    """打印回测汇总报告。"""
    print("=" * 70)
    print("  Hermes 样本外回测报告")
    print("=" * 70)
    print()
    
    if not stats:
        print("  ⚠️ 未找到符合条件的快照或结算结果")
        print()
        print("=" * 70)
        return
    
    print(f"  样本外场次:    {stats['total']}")
    print(f"  胜:            {stats['wins']}")
    print(f"  负:            {stats['losses']}")
    print(f"  样本外胜率:    {stats['win_rate']:.1f}%")
    print(f"  样本内基线:    {stats['in_sample_baseline']:.1f}% ({IN_SAMPLE_TOTAL}场)")
    print(f"  差值:          {stats['delta']:+.1f}pp")
    print(f"  跳过(无结果):  {stats['skipped_no_result']}")
    print(f"  跳过(数据错):  {stats['skipped_data_error']}")
    print()

    # 告警
    if stats["total"] >= MIN_SAMPLES_FOR_ALERT:
        if stats["win_rate"] < ALERT_THRESHOLD:
            print(f"  🚨 严重告警：样本外胜率 {stats['win_rate']:.1f}% < {ALERT_THRESHOLD}%！系统可能过拟合！")
        elif stats["win_rate"] < WARN_THRESHOLD:
            print(f"  ⚠️ 警告：样本外胜率 {stats['win_rate']:.1f}% < {WARN_THRESHOLD}%")
        else:
            print(f"  ✅ 样本外胜率在安全范围内")
    else:
        print(f"  ℹ️ 样本量不足（{stats['total']}<{MIN_SAMPLES_FOR_ALERT}），不触发告警")
    print()

    if not records:
        return

    # 按置信度分桶
    print("─" * 70)
    print("  按置信度分桶")
    print("─" * 70)
    print(f"  {'置信度':<6} {'场次':>5} {'胜':>4} {'负':>4} {'胜率':>7}")
    print(f"  {'-'*6} {'-'*5} {'-'*4} {'-'*4} {'-'*7}")
    for conf in ["高", "中", "低"]:
        subset = [r for r in records if r["confidence"] == conf]
        if not subset:
            continue
        w = sum(1 for r in subset if r["win"])
        wr = w / len(subset) * 100
        print(f"  {conf:<6} {len(subset):>5} {w:>4} {len(subset)-w:>4} {wr:>6.1f}%")
    print()

    # 按 Regime 分桶
    print("─" * 70)
    print("  按 Regime 分桶")
    print("─" * 70)
    print(f"  {'Regime':<8} {'场次':>5} {'胜':>4} {'负':>4} {'胜率':>7}")
    print(f"  {'-'*8} {'-'*5} {'-'*4} {'-'*4} {'-'*7}")
    for reg in ["摆动市", "趋势市"]:
        subset = [r for r in records if r["regime"] == reg]
        if not subset:
            continue
        w = sum(1 for r in subset if r["win"])
        wr = w / len(subset) * 100
        print(f"  {reg:<8} {len(subset):>5} {w:>4} {len(subset)-w:>4} {wr:>6.1f}%")
    print()

    # 按贴线模式分桶
    print("─" * 70)
    print("  按贴线模式分桶")
    print("─" * 70)
    print(f"  {'贴线':<6} {'场次':>5} {'胜':>4} {'负':>4} {'胜率':>7}")
    print(f"  {'-'*6} {'-'*5} {'-'*4} {'-'*4} {'-'*7}")
    for tie_label, tie_val in [("常规", False), ("贴线", True)]:
        subset = [r for r in records if r["is_tie"] == tie_val]
        if not subset:
            continue
        w = sum(1 for r in subset if r["win"])
        wr = w / len(subset) * 100
        print(f"  {tie_label:<6} {len(subset):>5} {w:>4} {len(subset)-w:>4} {wr:>6.1f}%")
    print()

    # 按日汇总
    if daily and records:
        print("─" * 70)
        print("  按日汇总")
        print("─" * 70)
        print(f"  {'日期':<12} {'场次':>5} {'胜':>4} {'负':>4} {'胜率':>7} {'累计胜率':>9}")
        print(f"  {'-'*12} {'-'*5} {'-'*4} {'-'*4} {'-'*7} {'-'*9}")
        by_date: dict[str, list[dict]] = defaultdict(list)
        for r in records:
            by_date[r["date"]].append(r)
        cum_wins = 0
        cum_total = 0
        for date_str in sorted(by_date.keys()):
            day_recs = by_date[date_str]
            dw = sum(1 for r in day_recs if r["win"])
            cum_wins += dw
            cum_total += len(day_recs)
            dwr = dw / len(day_recs) * 100
            cwr = cum_wins / cum_total * 100
            print(f"  {date_str:<12} {len(day_recs):>5} {dw:>4} {len(day_recs)-dw:>4} {dwr:>6.1f}% {cwr:>8.1f}%")
        print()

    # 运行中 10 场胜率（最近 10 场）
    if len(records) >= 10:
        last10 = records[-10:]
        last10_wr = sum(1 for r in last10 if r["win"]) / 10 * 100
        print("─" * 70)
        print(f"  最近 10 场胜率: {last10_wr:.1f}%")
        if last10_wr < 50:
            print(f"  🚨 最近 10 场胜率 {last10_wr:.1f}% < 50%！短期严重偏弱！")
        print()

    print("=" * 70)


def bucket_stats(records: list[dict], key: str) -> dict:
    """按指定维度分桶统计。"""
    buckets: dict[str, dict] = defaultdict(lambda: {"count": 0, "wins": 0})
    for r in records:
        val = r[key]
        label = "贴线" if key == "is_tie" and val else "常规" if key == "is_tie" else str(val)
        buckets[label]["count"] += 1
        if r["win"]:
            buckets[label]["wins"] += 1
    result = {}
    for label, b in sorted(buckets.items()):
        result[label] = {
            "count": b["count"],
            "wins": b["wins"],
            "win_rate": round(b["wins"] / b["count"] * 100, 1) if b["count"] else 0,
        }
    return result


def save_history(records: list[dict], stats: dict, since_date: str):
    """将本次回测结果追加到 oos_history.jsonl。"""
    total = stats["total"]
    if total == 0:
        return

    # 按维度分桶
    by_confidence = bucket_stats(records, "confidence")
    by_regime = bucket_stats(records, "regime")
    by_tie_line = bucket_stats(records, "is_tie")

    # 最近 10 场胜率
    last10 = records[-10:] if len(records) >= 10 else records
    last10_wr = sum(1 for r in last10 if r["win"]) / len(last10) * 100 if last10 else 0

    # 告警等级
    alert = None
    if total >= MIN_SAMPLES_FOR_ALERT:
        if stats["win_rate"] < ALERT_THRESHOLD:
            alert = "critical"
        elif stats["win_rate"] < WARN_THRESHOLD:
            alert = "warn"

    entry = {
        "run_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "since_date": since_date,
        "total": total,
        "wins": stats["wins"],
        "losses": stats["losses"],
        "win_rate": round(stats["win_rate"], 1),
        "last_10_win_rate": round(last10_wr, 1),
        "in_sample_baseline": IN_SAMPLE_WIN_RATE,
        "delta": stats["delta"],
        "by_confidence": by_confidence,
        "by_regime": by_regime,
        "by_tie_line": by_tie_line,
        "alert": alert,
        "skipped_no_result": stats["skipped_no_result"],
        "skipped_data_error": stats["skipped_data_error"],
    }

    OOS_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OOS_HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    print(f"📊 结果已持久化到 {OOS_HISTORY_FILE}")

    # 写入告警文件供 Dashboard 读取
    if alert:
        save_alert(entry)
    elif OOS_ALERT_FILE.exists():
        # 如果之前有告警但现在已恢复，清除告警文件
        OOS_ALERT_FILE.unlink(missing_ok=True)


def save_alert(entry: dict):
    """写入告警文件供 Dashboard 读取。"""
    alert_data = {
        "updated": entry["run_date"],
        "severity": entry["alert"],
        "win_rate": entry["win_rate"],
        "total": entry["total"],
        "delta": entry["delta"],
        "last_10_win_rate": entry["last_10_win_rate"],
        "message": (
            f"严重告警：样本外胜率 {entry['win_rate']}% < {ALERT_THRESHOLD}%！"
            if entry["alert"] == "critical"
            else f"警告：样本外胜率 {entry['win_rate']}% < {WARN_THRESHOLD}%"
        ),
    }
    OOS_ALERT_FILE.write_text(json.dumps(alert_data, ensure_ascii=False, indent=2))
    print(f"🚨 告警已写入 {OOS_ALERT_FILE}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Hermes 样本外回测")
    parser.add_argument("--since", default="2026-08-17",
                        help="起始日期，默认 2026-08-17（round29 训练集之后的日期）")
    parser.add_argument("--daily", action="store_true",
                        help="按日汇总输出")
    parser.add_argument("--save", action="store_true",
                        help="持久化结果到 data/oos_history.jsonl")
    args = parser.parse_args()

    records, stats = run_oos_backtest(args.since, args.daily)
    print_summary(records, stats, args.daily)

    if args.save and stats:
        save_history(records, stats, args.since)

    return 0


if __name__ == "__main__":
    sys.exit(main())