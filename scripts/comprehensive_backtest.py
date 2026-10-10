#!/usr/bin/env python3
"""
Hermes 量化引擎全面回测（阶段 A + C）

对 data/snapshots/ 下所有快照用 rule_engine.judge() 做全量回测，
输出分桶胜率 + 门槛层触发统计。

不改引擎代码，纯审计。

用法：python3 scripts/comprehensive_backtest.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from signal_validator import extract_factors, load_market_data
from rule_engine import judge

SNAPSHOT_DIR = Path("/Users/tangyijun/Desktop/hermes btc/data/snapshots")
RESULTS_FILE = Path("/Users/tangyijun/Desktop/hermes btc/data/hermes_results.jsonl")
OUT_FILE = Path("/Users/tangyijun/Desktop/hermes btc/data/hermes_comprehensive_audit_20260817.md")


def load_results() -> dict[str, dict]:
    """从 hermes_results.jsonl 加载所有结算结果。"""
    results: dict[str, dict] = {}
    if not RESULTS_FILE.exists():
        return results
    for line in RESULTS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        slug = r.get("slug")
        if slug and r.get("actual"):
            results[slug] = {
                "side": r.get("side"),
                "actual": r["actual"],
                "win": r["side"] == r["actual"],
                "buy_price": r.get("buy_price"),
                "pnl": r.get("pnl"),
            }
    return results


def get_latest_snapshots() -> dict[str, Path]:
    """每个 slug 取最新（collect_time_utc 最大）的快照文件。"""
    slugs: dict[str, list[Path]] = defaultdict(list)
    for p in sorted(SNAPSHOT_DIR.glob("*.json")):
        slug = p.name.split("_202")[0]  # 文件名格式: slug_YYYYMMDD_HHMMSS.json
        slugs[slug].append(p)
    return {slug: sorted(paths, key=lambda x: x.stat().st_mtime)[-1] for slug, paths in slugs.items()}


def get_actual_from_snapshot(md: dict) -> str | None:
    """从快照的 prev_hour 字段推断前一小时实际方向（作为结算结果）。"""
    prev = md.get("prev_hour") or {}
    dh = prev.get("direction_history") or []
    if len(dh) >= 2:
        return dh[-1]
    return None


def main() -> int:
    results = load_results()
    slugs = get_latest_snapshots()
    print(f"快照文件: {len(slugs)} 个唯一 slug")
    print(f"结算结果: {len(results)} 个")

    rows: list[dict] = []
    skipped_no_result = 0
    skipped_data_error = 0

    for slug, path in sorted(slugs.items()):
        try:
            md = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            skipped_data_error += 1
            continue

        # 提取因子 + 调 judge
        try:
            factors, inp, notes, mode = extract_factors(md)
        except Exception:
            skipped_data_error += 1
            continue

        # 两遍判定（与 signal_validator 口径一致）
        buy_wall = inp.pop("_buy_wall_ratio")
        sell_wall = inp.pop("_sell_wall_ratio")
        taker_bs = inp.pop("taker_bs_ratio", None)

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
                       taker_bs_ratio=taker_bs))

        # 匹配结算结果
        res = results.get(slug)
        actual = None
        if res:
            actual = res["actual"]
        else:
            # 回退：从快照自身推断
            actual = get_actual_from_snapshot(md)
            if not actual:
                skipped_no_result += 1
                continue

        win = d.final_direction == actual if d.final_direction and actual else None
        if win is None:
            skipped_no_result += 1
            continue

        # 收集数据
        pm_up = inp.get("pm_up") or 0
        pm_side = max(pm_up, 100 - pm_up) if pm_up else 0
        pm_dir = None
        if pm_up and pm_up != 50:
            pm_dir = "UP" if pm_up > 50 else "DOWN"

        dev = float(inp.get("deviation_pct", 0) or 0)
        tie_mode = bool(inp.get("_tie_mode"))
        regime = inp.get("regime") or "?"
        vol = inp.get("volatility_5m") or 0

        # 门槛层触发标记
        trace_str = "|".join(d.trace)
        risk_str = "|".join(d.risks)

        rows.append({
            "slug": slug,
            "s0": d.s0,
            "net": d.net,
            "net_direction": d.net_direction,
            "final_direction": d.final_direction,
            "actual": actual,
            "win": win,
            "confidence": d.confidence,
            "pm_side": pm_side,
            "pm_dir": pm_dir,
            "dev_pct": dev,
            "tie_mode": tie_mode,
            "regime": regime,
            "vol": vol,
            "action": d.action,
            "position_scale": d.position_scale,
            "h12_triggered": d.h12_triggered,
            "g2_triggered": "G2" in trace_str,
            "g3_overrode": "G3 硬覆盖" in trace_str,
            "g4_triggered": "G4/H9" in trace_str,
            "g5_cover": "G5.4 覆盖" in trace_str,
            "f1_triggered": "翻转级" in trace_str,
            "trace": trace_str,
            "risks": risk_str,
            "factors": {k: v for k, v in factors.items()},
            "mode": mode,
        })

    total = len(rows)
    wins = sum(1 for r in rows if r["win"])
    win_rate = wins / total * 100 if total else 0

    print(f"有效样本: {total} | 胜: {wins} | 负: {total - wins} | 胜率: {win_rate:.1f}%")
    print(f"跳过(无结果): {skipped_no_result} | 跳过(数据错误): {skipped_data_error}")

    # ============ 分桶分析 ============

    def bucket_stats(name: str, key_fn, fmt_fn=None) -> list[str]:
        """通用分桶统计。"""
        buckets: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            k = key_fn(r)
            if fmt_fn:
                k = fmt_fn(k)
            buckets[k].append(r)

        lines = []
        lines.append(f"### {name}")
        lines.append("")
        lines.append(f"| 分组 | 场次 | 胜 | 负 | 胜率 | S0均值 | |净分|均值 |")
        lines.append("|------|------|-----|-----|------|--------|----------|")
        for k in sorted(buckets.keys(), key=lambda x: str(x)):
            b = buckets[k]
            n = len(b)
            w = sum(1 for r in b if r["win"])
            rate = w / n * 100 if n else 0
            s0_avg = sum(abs(r["s0"]) for r in b) / n if n else 0
            net_avg = sum(abs(r["net"]) for r in b) / n if n else 0
            lines.append(f"| {k} | {n} | {w} | {n-w} | {rate:.1f}% | {s0_avg:.1f} | {net_avg:.1f} |")
        lines.append("")
        return lines

    def bucket_by_conf(key_fn, fmt_fn=None) -> list[str]:
        """按置信度分桶。"""
        buckets: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            k = key_fn(r)
            if fmt_fn:
                k = fmt_fn(k)
            buckets[k].append(r)

        lines = []
        lines.append(f"| 分组 | 场次 | 胜 | 负 | 胜率 | 高置信 | 中置信 | 低置信 |")
        lines.append("|------|------|-----|-----|------|--------|--------|--------|")
        for k in sorted(buckets.keys(), key=lambda x: str(x)):
            b = buckets[k]
            n = len(b)
            w = sum(1 for r in b if r["win"])
            rate = w / n * 100 if n else 0
            hi = sum(1 for r in b if r["confidence"] == "高")
            mi = sum(1 for r in b if r["confidence"] == "中")
            lo = sum(1 for r in b if r["confidence"] == "低")
            lines.append(f"| {k} | {n} | {w} | {n-w} | {rate:.1f}% | {hi} | {mi} | {lo} |")
        lines.append("")
        return lines

    # ---- 构建报告 ----
    out: list[str] = []
    out.append("# Hermes 量化引擎全面审计报告")
    out.append("")
    out.append(f"> 生成时间：2026-08-17")
    out.append(f"> 样本：{len(slugs)} 个唯一 slug，有效 {total} 场")
    out.append(f"> 引擎版本：`rule_engine.py` RULE_VERSION = `prompt.md@2026-08-16-round25`")
    out.append("")
    out.append(f"## 一、全量基线")
    out.append("")
    out.append(f"- **全量胜率：{win_rate:.1f}%（{wins}/{total}）**")
    out.append(f"- 全量 S0 均值：{sum(abs(r['s0']) for r in rows)/total:.1f}")
    out.append(f"- 全量 |净分| 均值：{sum(abs(r['net']) for r in rows)/total:.1f}")
    out.append("")

    # 实盘对比
    live_wins = sum(1 for r in rows if r["slug"] in results and r["win"])
    live_total = sum(1 for r in rows if r["slug"] in results)
    live_rate = live_wins / live_total * 100 if live_total else 0
    out.append(f"- **实盘匹配场次胜率：{live_rate:.1f}%（{live_wins}/{live_total}）**")
    out.append("")

    # 按模式分桶
    out.append(f"## 二、按模式分桶")
    out.append("")
    out.extend(bucket_stats("模式", lambda r: r["mode"]))
    out.extend(bucket_stats("Regime", lambda r: r["regime"]))
    out.extend(bucket_stats("贴线状态", lambda r: "贴线" if r["tie_mode"] else "常规"))

    # 按评分分桶
    out.append(f"## 三、按评分区间分桶")
    out.append("")
    out.extend(bucket_stats("S0 区间", lambda r: (
        "≥10" if abs(r["s0"]) >= 10 else
        "6-9" if abs(r["s0"]) >= 6 else
        "3-5" if abs(r["s0"]) >= 3 else
        "0-2" if abs(r["s0"]) >= 0 else "0"
    )))
    out.extend(bucket_stats("|净分| 区间", lambda r: (
        "≥10" if abs(r["net"]) >= 10 else
        "6-9" if abs(r["net"]) >= 6 else
        "3-5" if abs(r["net"]) >= 3 else
        "0-2"
    )))

    # 按 PM 分桶
    out.append(f"## 四、按 PM 共识分桶")
    out.append("")
    out.extend(bucket_stats("PM 共识区间", lambda r: (
        "≥80% 深度共识" if r["pm_side"] >= 80 else
        "70-80% 强共识" if r["pm_side"] >= 70 else
        "60-70% 中等共识" if r["pm_side"] >= 60 else
        "50-60% 弱共识" if r["pm_side"] >= 50 else
        "无PM"
    )))

    # 按方向顺逆 PM
    out.extend(bucket_stats("方向 vs PM", lambda r: (
        "顺PM" if r["pm_dir"] and r["final_direction"] == r["pm_dir"] else
        "逆PM" if r["pm_dir"] and r["final_direction"] != r["pm_dir"] else
        "无PM"
    )))

    # 按置信度分桶
    out.append(f"## 五、按置信度分桶")
    out.append("")
    out.extend(bucket_stats("置信度", lambda r: r["confidence"]))

    # 按偏差分桶
    out.append(f"## 六、按偏差区间分桶")
    out.append("")
    out.extend(bucket_stats("|偏差%| 区间", lambda r: (
        "<0.05% 贴线" if abs(r["dev_pct"]) < 0.05 else
        "0.05-0.10%" if abs(r["dev_pct"]) < 0.10 else
        "0.10-0.15%" if abs(r["dev_pct"]) < 0.15 else
        "≥0.15% G4区"
    )))

    # 按 position_scale 分桶
    out.append(f"## 七、按仓位规模分桶")
    out.append("")
    out.extend(bucket_stats("position_scale", lambda r: str(r["position_scale"])))

    # ============ 阶段 C：门槛层分析 ============
    out.append(f"## 八、门槛层触发统计")
    out.append("")

    gates = {
        "G2 PM≥80%反向保护": "g2_triggered",
        "G3 逆PM硬覆盖": "g3_overrode",
        "G4 价格位置回退": "g4_triggered",
        "G5.4 覆盖翻向": "g5_cover",
        "H12 降权": "h12_triggered",
        "F1 翻转级": "f1_triggered",
    }

    out.append("| 门槛层 | 触发次数 | 触发率 | 触发场胜率 | 触发场S0均值 |")
    out.append("|--------|---------|--------|-----------|-------------|")
    for name, key in gates.items():
        triggered = [r for r in rows if r[key]]
        n = len(triggered)
        r = n / total * 100 if total else 0
        w = sum(1 for r in triggered if r["win"]) if n else 0
        wr = w / n * 100 if n else 0
        s0_avg = sum(abs(r["s0"]) for r in triggered) / n if n else 0
        out.append(f"| {name} | {n} | {r:.1f}% | {wr:.1f}% ({w}/{n}) | {s0_avg:.1f} |")
    out.append("")

    # 9.7 各项触发
    out.append("### 9.7 各项触发统计")
    out.append("")
    out.append("| 9.7 项 | 触发次数 | 触发场胜率 |")
    out.append("|--------|---------|-----------|")

    _97_items = {
        "#1 顺共识60-80% 降级": "顺共识区",
        "#2 深度共识≥80% 锁低": "庄家收割区",
        "#3 尾盘操控": "尾盘庄家操控",
        "#4 贴线+顺PM": "贴线+共识",
        "#5 贴线+尾盘+共识": "贴线铁律",
        "#6 反手时点": "反手时点",
        "#8 贴线高不确定": "贴线高不确定",
    }
    for name, keyword in _97_items.items():
        triggered = [r for r in rows if keyword in r["risks"]]
        n = len(triggered)
        w = sum(1 for r in triggered if r["win"]) if n else 0
        wr = w / n * 100 if n else 0
        out.append(f"| {name} | {n} | {wr:.1f}% ({w}/{n}) |")
    out.append("")

    # ============ 时间序列（按 slug 日期排序） ============
    out.append(f"## 九、全部场次明细")
    out.append("")
    out.append("| slug | 方向 | 实际 | 结果 | S0 | 净分 | PM% | 偏差% | 模式 | 置信 | scale |")
    out.append("|------|------|------|------|-----|------|-----|-------|------|------|-------|")
    for r in sorted(rows, key=lambda x: x["slug"]):
        emoji = "✅" if r["win"] else "❌"
        out.append(
            f"| {r['slug']} | {r['final_direction']} | {r['actual']} | {emoji} | "
            f"{r['s0']:+.1f} | {r['net']:+.1f} | {r['pm_side']:.0f}% | "
            f"{r['dev_pct']:+.3f}% | {r['mode']} | {r['confidence']} | {r['position_scale']} |"
        )
    out.append("")

    # 写入文件
    OUT_FILE.write_text("\n".join(out), encoding="utf-8")
    print(f"\n报告已生成：{OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())