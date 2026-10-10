#!/usr/bin/env python3
"""
paper_report.py — Hermes BTC 1H paper trading 周报

读 data/hermes_shadow_paper.jsonl 累计样本,输出:
1. 累计统计(N / 胜率 / 总 P&L / 平均 P&L)
2. 按 side 分(UP / DOWN 胜率 / P&L)
3. 按 strategy_version 分(round41 vs round42 vs round47 vs …新规则 vs 旧规则对比)
4. 按 entry_price 段分(<0.40 低赔率 / 0.40-0.55 中赔率 / >0.55 高赔率)
5. 按结算方向分(UP/DOWN 占比)
6. 5 条自动教训(基于样本分布提示)

用法:
  python3 paper_report.py                 # 全量报告
  python3 paper_report.py --days 7        # 最近 N 天
  python3 paper_report.py --md            # 输出 Markdown 报告落 data/reports/

设计:不依赖 factor_attribution.py,只基于 shadow_paper.jsonl 已有字段直接统计,
后续攒够 200 笔可结合 factor_attribution 跑深度归因。
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import Counter, defaultdict

SCRIPT_DIR = Path(__file__).parent
HERMES_DIR = SCRIPT_DIR.parent
DATA_DIR = HERMES_DIR / "data"
REPORTS_DIR = DATA_DIR / "reports"
SHADOW_FILE = DATA_DIR / "hermes_shadow_paper.jsonl"


def load_shadow() -> list[dict]:
    if not SHADOW_FILE.exists():
        return []
    out = []
    for line in SHADOW_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def in_window(r: dict, days: int) -> bool:
    try:
        ts = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
    except Exception:
        return True  # 时间解析失败视作在窗内
    return ts >= datetime.now(timezone.utc) - timedelta(days=days)


def stats_for(records: list[dict]) -> dict:
    """通用统计"""
    n = len(records)
    if n == 0:
        return {"n": 0}
    wins = sum(1 for r in records if r.get("win"))
    total_p_l = sum(r.get("p_l", 0) for r in records)
    return {
        "n": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate": round(wins / n, 4),
        "total_p_l": round(total_p_l, 4),
        "avg_p_l": round(total_p_l / n, 4),
    }


def bucket_entry_price(r: dict) -> str:
    p = r.get("entry_price", 0)
    if p < 0.40:
        return "<0.40 低赔率"
    if p < 0.55:
        return "0.40-0.55 中赔率"
    return ">=0.55 高赔率"


def bucket_dev_pct(r: dict) -> str:
    anchor = r.get("anchor_price")
    close = r.get("close_price")
    if anchor is None or close is None:
        return "未知"
    dev = abs(close - anchor) / anchor * 100
    if dev < 0.05:
        return "<0.05% 贴线"
    if dev < 0.15:
        return "0.05-0.15% 中"
    return ">=0.15% 偏离"


def generate_lessons(records: list[dict], stats: dict) -> list[str]:
    """基于样本自动生成 5 条教训"""
    lessons = []
    n = stats["n"]
    if n < 10:
        return ["样本不足(<10 笔),暂不生成统计教训"]

    # 1. 整体胜率
    wr = stats["win_rate"]
    if wr >= 0.60:
        lessons.append(f"✅ 整体胜率 {wr*100:.1f}% ≥60%,模型在新样本下仍有效")
    elif wr >= 0.50:
        lessons.append(f"⚠️ 整体胜率 {wr*100:.1f}% 50-60%,边缘状态,关注样本扩样后是否衰减")
    else:
        lessons.append(f"❌ 整体胜率 {wr*100:.1f}% <50%,模型失效,需重审规则")

    # 2. UP vs DOWN 对比
    up = [r for r in records if r["side"] == "UP"]
    dn = [r for r in records if r["side"] == "DOWN"]
    if up and dn:
        up_wr = sum(1 for r in up if r.get("win")) / len(up)
        dn_wr = sum(1 for r in dn if r.get("win")) / len(dn)
        if abs(up_wr - dn_wr) > 0.15:
            better = "UP" if up_wr > dn_wr else "DOWN"
            worse = "DOWN" if better == "UP" else "UP"
            lessons.append(f"⚠️ {better} 胜率 {max(up_wr, dn_wr)*100:.1f}% vs {worse} {min(up_wr, dn_wr)*100:.1f}%,方向不平衡,优先关注 {worse} 信号质量")

    # 3. 高赔率 vs 低赔率
    high = [r for r in records if r.get("entry_price", 0) >= 0.55]
    low = [r for r in records if r.get("entry_price", 0) < 0.40]
    if high and len(high) >= 5:
        high_wr = sum(1 for r in high if r.get("win")) / len(high)
        if high_wr < 0.50:
            lessons.append(f"⚠️ 高赔率(entry>=0.55)胜率 {high_wr*100:.1f}% <50%,bot max_price=0.60 拦截是否过松?考虑降至 0.55")
    if low and len(low) >= 5:
        low_wr = sum(1 for r in low if r.get("win")) / len(low)
        if low_wr >= 0.70:
            lessons.append(f"✅ 低赔率(<0.40)胜率 {low_wr*100:.1f}%,可考虑放松 max_price 上限到 0.70 捕获更多机会")

    # 4. 贴线样本
    tie = [r for r in records if bucket_dev_pct(r) == "<0.05% 贴线"]
    if tie and len(tie) >= 3:
        tie_wr = sum(1 for r in tie if r.get("win")) / len(tie)
        lessons.append(f"📌 贴线(|dev|<0.05%)样本 {len(tie)} 笔胜率 {tie_wr*100:.1f}%,贴线策略在新规则下 {'稳定' if tie_wr>=0.55 else '需重审'}")

    # 5. 策略版本对比
    versions = defaultdict(list)
    for r in records:
        v = r.get("strategy_version", "unknown")
        versions[v].append(r)
    if len(versions) > 1:
        version_stats = []
        for v, recs in versions.items():
            wr = sum(1 for r in recs if r.get("win")) / len(recs)
            version_stats.append((v, len(recs), wr))
        version_stats.sort(key=lambda x: -x[2])
        best = version_stats[0]
        worst = version_stats[-1]
        if best[2] - worst[2] > 0.10:
            lessons.append(f"📊 策略版本对比:{best[0]}({best[1]} 笔 胜率{best[2]*100:.1f}%) vs {worst[0]}({worst[1]} 笔 {worst[2]*100:.1f}%),版本迭代 {'有效' if best[2]>worst[2] else '需复核'}")

    # 默认填补
    while len(lessons) < 5:
        lessons.append("样本特征未触发特定教训,持续攒样本中")

    return lessons[:5]


def generate_report(days: int = None) -> dict:
    records = load_shadow()
    if days:
        records = [r for r in records if in_window(r, days)]

    settled = [r for r in records if r.get("status") == "settled"]
    pending = [r for r in records if r.get("status") == "pending"]

    overall = stats_for(settled)

    # 按 side
    up_recs = [r for r in settled if r["side"] == "UP"]
    dn_recs = [r for r in settled if r["side"] == "DOWN"]
    by_side = {"UP": stats_for(up_recs), "DOWN": stats_for(dn_recs)}

    # 按 strategy_version
    by_version = {}
    versions = defaultdict(list)
    for r in settled:
        v = r.get("strategy_version", "unknown")
        versions[v].append(r)
    for v, recs in sorted(versions.items()):
        by_version[v] = stats_for(recs)

    # 按 entry_price
    by_price = {}
    prices = defaultdict(list)
    for r in settled:
        b = bucket_entry_price(r)
        prices[b].append(r)
    for b in ["<0.40 低赔率", "0.40-0.55 中赔率", ">=0.55 高赔率"]:
        if prices[b]:
            by_price[b] = stats_for(prices[b])

    # 按 贴线/偏离
    by_dev = {}
    devs = defaultdict(list)
    for r in settled:
        b = bucket_dev_pct(r)
        devs[b].append(r)
    for b in devs:
        by_dev[b] = stats_for(devs[b])

    lessons = generate_lessons(settled, overall)

    return {
        "ok": True,
        "window_days": days,
        "total_samples": len(records),
        "settled": len(settled),
        "pending": len(pending),
        "overall": overall,
        "by_side": by_side,
        "by_strategy_version": by_version,
        "by_entry_price_bucket": by_price,
        "by_dev_bucket": by_dev,
        "lessons": lessons,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def render_text(r: dict) -> str:
    lines = []
    lines.append("=" * 60)
    lines.append(f"Hermes BTC 1H Paper Trading 周报")
    lines.append("=" * 60)
    if r.get("window_days"):
        lines.append(f"窗口:最近 {r['window_days']} 天")
    lines.append(f"生成时间(UTC): {r['generated_at']}")
    lines.append("")
    lines.append(f"📊 累计统计")
    lines.append(f"  总样本:{r['total_samples']} | 已结算:{r['settled']} | 待结算:{r['pending']}")
    o = r["overall"]
    lines.append(f"  胜率:{o.get('win_rate', 0)*100:.1f}% ({o.get('wins',0)}/{o.get('n',0)})")
    lines.append(f"  总 P&L:${o.get('total_p_l', 0):.2f} | 平均 P&L:${o.get('avg_p_l', 0):.4f}/笔")
    lines.append("")

    lines.append("📈 按方向")
    for side in ["UP", "DOWN"]:
        s = r["by_side"].get(side, {})
        if s.get("n", 0) > 0:
            lines.append(f"  {side}:{s['n']} 笔,胜率 {s['win_rate']*100:.1f}%,P&L ${s['total_p_l']:.2f}")
    lines.append("")

    if r["by_strategy_version"]:
        lines.append("🔖 按策略版本")
        for v, s in sorted(r["by_strategy_version"].items()):
            lines.append(f"  {v}:{s['n']} 笔,胜率 {s['win_rate']*100:.1f}%,P&L ${s['total_p_l']:.2f}")
        lines.append("")

    if r["by_entry_price_bucket"]:
        lines.append("💰 按 entry_price 段")
        for b in ["<0.40 低赔率", "0.40-0.55 中赔率", ">=0.55 高赔率"]:
            s = r["by_entry_price_bucket"].get(b)
            if s:
                lines.append(f"  {b}:{s['n']} 笔,胜率 {s['win_rate']*100:.1f}%,P&L ${s['total_p_l']:.2f}")
        lines.append("")

    if r["by_dev_bucket"]:
        lines.append("📏 按 |deviation| 段")
        for b, s in r["by_dev_bucket"].items():
            lines.append(f"  {b}:{s['n']} 笔,胜率 {s['win_rate']*100:.1f}%,P&L ${s['total_p_l']:.2f}")
        lines.append("")

    lines.append("📚 自动教训(5 条)")
    for i, lesson in enumerate(r["lessons"], 1):
        lines.append(f"  {i}. {lesson}")
    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)


def render_markdown(r: dict) -> str:
    lines = [f"# Hermes BTC 1H Paper Trading 周报\n"]
    if r.get("window_days"):
        lines.append(f"**窗口**: 最近 {r['window_days']} 天\n")
    lines.append(f"**生成时间(UTC)**: {r['generated_at']}\n")
    lines.append("")
    lines.append("## 📊 累计统计\n")
    o = r["overall"]
    lines.append(f"- 总样本: **{r['total_samples']}** | 已结算: **{r['settled']}** | 待结算: **{r['pending']}**")
    lines.append(f"- 胜率: **{o.get('win_rate', 0)*100:.1f}%** ({o.get('wins',0)}/{o.get('n',0)})")
    lines.append(f"- 总 P&L: **${o.get('total_p_l', 0):.2f}** | 平均 P&L: **${o.get('avg_p_l', 0):.4f}/笔**")
    lines.append("")

    lines.append("## 📈 按方向\n")
    lines.append("| 方向 | N | 胜率 | 总 P&L |")
    lines.append("|---|---:|---:|---:|")
    for side in ["UP", "DOWN"]:
        s = r["by_side"].get(side, {})
        if s.get("n", 0) > 0:
            lines.append(f"| {side} | {s['n']} | {s['win_rate']*100:.1f}% | ${s['total_p_l']:.2f} |")
    lines.append("")

    if r["by_strategy_version"]:
        lines.append("## 🔖 按策略版本\n")
        lines.append("| 版本 | N | 胜率 | 总 P&L |")
        lines.append("|---|---:|---:|---:|")
        for v, s in sorted(r["by_strategy_version"].items()):
            lines.append(f"| {v} | {s['n']} | {s['win_rate']*100:.1f}% | ${s['total_p_l']:.2f} |")
        lines.append("")

    if r["by_entry_price_bucket"]:
        lines.append("## 💰 按 entry_price 段\n")
        lines.append("| 段 | N | 胜率 | 总 P&L |")
        lines.append("|---|---:|---:|---:|")
        for b in ["<0.40 低赔率", "0.40-0.55 中赔率", ">=0.55 高赔率"]:
            s = r["by_entry_price_bucket"].get(b)
            if s:
                lines.append(f"| {b} | {s['n']} | {s['win_rate']*100:.1f}% | ${s['total_p_l']:.2f} |")
        lines.append("")

    if r["by_dev_bucket"]:
        lines.append("## 📏 按 |deviation| 段\n")
        lines.append("| 段 | N | 胜率 | 总 P&L |")
        lines.append("|---|---:|---:|---:|")
        for b, s in r["by_dev_bucket"].items():
            lines.append(f"| {b} | {s['n']} | {s['win_rate']*100:.1f}% | ${s['total_p_l']:.2f} |")
        lines.append("")

    lines.append("## 📚 自动教训\n")
    for i, lesson in enumerate(r["lessons"], 1):
        lines.append(f"{i}. {lesson}")
    lines.append("")
    lines.append("\n> ⚠️ 本报告基于 paper_simulator 影子账,不构成投资建议。")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Hermes paper trading 周报")
    parser.add_argument("--days", type=int, default=None, help="最近 N 天(默认全量)")
    parser.add_argument("--md", action="store_true", help="输出 Markdown 文件到 data/reports/")
    args = parser.parse_args()

    r = generate_report(days=args.days)
    print(render_text(r))

    if args.md:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        if args.days:
            md_path = REPORTS_DIR / f"paper_report_{ts}_last{args.days}d.md"
        else:
            md_path = REPORTS_DIR / f"paper_report_{ts}_all.md"
        md_path.write_text(render_markdown(r), encoding="utf-8")
        print(f"\n📝 Markdown 报告已落盘:{md_path}")


if __name__ == "__main__":
    main()