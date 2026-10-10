#!/usr/bin/env python3
"""
obsidian_sync.py — Hermes 交易分析记录自动同步到 Obsidian

把 Hermes (btc-agent) 的复盘/分析记录自动整理成 Obsidian 笔记。全自动，可挂 cron/launchd。

数据源（项目侧落盘）：
  data/hermes_results.jsonl   每笔已复盘交易（权威：order_id/slug/side/actual/amount/pnl/buy_price）
  data/hermes_analysis.json   当前最新一份完整分析（每整点 :29 更新，仅保留最新）
  data/hermes_done.jsonl      成交信号（signal+result）
  data/review-log.md          复盘详情（reason/指标贡献/教训，供复盘篇补充）

输出（Obsidian 库，注意：库在工作区外，需用户授权的绝对路径）：
  <VAULT>/Polymarket/BTC-Hourly-Analysis/<slug>.md    当前分析篇（覆盖更新）
  <VAULT>/Polymarket/YYYY-MM-DD/复盘-<slug>.md        每笔复盘篇（幂等，已存在跳过）
  <VAULT>/Polymarket/YYYY-MM-DD/YYYY-MM-DD-今日.md    每日汇总（追加/重建）
  <VAULT>/Polymarket/BTC-Hourly-Analysis/BTC-Hourly-Analysis-Index.md  目录索引（重建）

用法：
  python3 scripts/obsidian_sync.py            # 全量同步（写文件）
  python3 scripts/obsidian_sync.py --dry-run  # 只打印将执行的动作，不写文件
  python3 scripts/obsidian_sync.py --analysis-only   # 只同步当前分析篇
  python3 scripts/obsidian_sync.py --since 2026-08-14  # 只同步该日期之后

注意：hermes_analysis.json 只保留最新一份，因此分析篇无法为历史小时重建（数据源客观缺失）。
      复盘篇有全历史（results.jsonl），今日.md 汇总当日结果。

⚠️ 生成的笔记是"结构化数据驱动"，不包含 AI 手写式"自检"段（那需要分析判断）。铁律遵循 Karpathy：
     自指/bullet/双链/给自己看/不修饰。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, date, timedelta
from pathlib import Path

# ============ 路径配置 ============
HERMES_DIR = Path("/Users/tangyijun/Desktop/hermes btc")
DATA_DIR = HERMES_DIR / "data"
# Obsidian 库根（在工作区外，用户已确认授权）
VAULT = Path("/Users/tangyijun/Documents/Obsidian Vault")
HOURLY_DIR = VAULT / "Polymarket" / "BTC-Hourly-Analysis"
INDEX_FILE = HOURLY_DIR / "BTC-Hourly-Analysis-Index.md"

RESULTS_FILE = DATA_DIR / "hermes_results.jsonl"
ANALYSIS_FILE = DATA_DIR / "hermes_analysis.json"
DONE_FILE = DATA_DIR / "hermes_done.jsonl"
REVIEW_LOG_FILE = DATA_DIR / "review-log.md"

SLUG_RE = re.compile(
    r"^bitcoin-up-or-down-(\w+)-(\d+)-(\d{4})-(\d{1,2})(am|pm)-et$"
)
MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}


# ============ 工具函数 ============
def parse_slug(slug: str):
    """把 slug 解析成 (date, hour24)。失败返回 None。"""
    m = SLUG_RE.match(slug or "")
    if not m:
        return None
    mon_name, day, year, h12, ampm = m.groups()
    mon = MONTHS.get(mon_name)
    if mon is None:
        return None
    hour = int(h12) % 12
    if ampm == "pm":
        hour += 12
    return date(int(year), mon, int(day)), hour


def slug_date_str(slug: str) -> str:
    """slug → 'YYYY-MM-DD'"""
    p = parse_slug(slug)
    return p[0].isoformat() if p else ""


def fmt_currency(x) -> str:
    if x is None:
        return ""
    try:
        return f"${float(x):,.2f}"
    except (TypeError, ValueError):
        return str(x)


def _fmt_prob(p) -> str:
    """概率显示归一：0-1 的小数转百分比整数/小数，非数值原样返回。"""
    if p is None or p == "":
        return ""
    try:
        f = float(p)
    except (TypeError, ValueError):
        return str(p)
    if 0 < f < 1:
        return f"{f * 100:g}"
    return f"{f:g}"


def _split_reasons(reasons):
    """reasons 可能含超长单元素（内嵌 ';' 分隔），拆成独立要点列表。"""
    out = []
    for r in (reasons or []):
        if not isinstance(r, str):
            out.append(str(r))
            continue
        parts = [p.strip() for p in re.split(r"[;；]", r)]
        if len(parts) > 1:
            out.extend(p for p in parts if p)
        else:
            out.append(r.strip())
    return out


def _slug_sort_key(slug: str):
    """按 (日期, 小时) 排序 key，保证同日内按时间序而非字母序。"""
    p = parse_slug(slug)
    return (p[0], p[1]) if p else (date.min, 0)


# 今日.md 手工补充区 marker：marker 之间内容脚本保留不覆盖
MANUAL_MARKER_HEAD = "## ✍️ 手工补充（今日主线/自检/教训）"
MANUAL_MARKER_FOOT = "## 关联"
MANUAL_PLACEHOLDER = "（可在此补充：今日主线 / 自检 / 教训深化 / 关键观察。脚本只保留此区内容，其余自动重建。）"


def extract_manual_section(text: str) -> str:
    """从旧今日.md 提取手工补充区内容（HEAD 到 FOOT 之间），无手工内容返回 ''。"""
    m = re.search(
        rf"{re.escape(MANUAL_MARKER_HEAD)}(.*?)(?={re.escape(MANUAL_MARKER_FOOT)}|\Z)",
        text, re.S)
    if not m:
        return ""
    body = m.group(1).strip()
    if not body or MANUAL_PLACEHOLDER in body:
        return ""
    return body


def read_jsonl(path: Path):
    """读取 jsonl，容错损坏行。"""
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def load_review_log_details():
    """解析 review-log.md，返回 {slug: {...}}。
    按 '## <ts> | <slug>' 分块，提取预测/实际/结果/信号原因/指标贡献/教训。
    """
    if not REVIEW_LOG_FILE.exists():
        return {}
    text = REVIEW_LOG_FILE.read_text()
    blocks = re.split(r"\n## ", text)
    details = {}
    for blk in blocks:
        m = re.match(r"[^|]*\| ([^\n]+)", blk)
        if not m:
            continue
        slug = m.group(1).strip()
        # 提取关键字段
        pred = re.search(r"\*\*预测\*\*: ([^\n]*)", blk)
        actual = re.search(r"\*\*实际\*\*: ([^\n]*)", blk)
        result = re.search(r"\*\*结果\*\*: ([^\n]*)", blk)
        reason = re.search(r"### 信号原因\n([^\n]*)", blk)
        correct = re.search(r"- 正确贡献: ([^\n]*)", blk)
        wrong = re.search(r"- 错误贡献: ([^\n]*)", blk)
        # 教训块：### ⚠️ 教训 之后的 bullet
        lesson_m = re.search(r"### ⚠️ 教训\n(.*?)(?:---|\Z)", blk, re.S)
        lessons = []
        if lesson_m:
            lessons = [l.strip().lstrip("- ") for l in lesson_m.group(1).splitlines()
                       if l.strip().startswith("-")]
        details[slug] = {
            "pred": pred.group(1).strip() if pred else "",
            "actual": actual.group(1).strip() if actual else "",
            "result": result.group(1).strip() if result else "",
            "reason": reason.group(1).strip() if reason else "",
            "correct": correct.group(1).strip() if correct else "",
            "wrong": wrong.group(1).strip() if wrong else "",
            "lessons": lessons,
        }
    return details


# ============ 生成器 ============
def gen_analysis_note(analysis: dict) -> str:
    """从 hermes_analysis.json 生成分析篇 markdown。"""
    slug = analysis.get("slug", "")
    direction = analysis.get("direction", analysis.get("predicted_side", ""))
    conf = analysis.get("confidence", "")
    prob = _fmt_prob(analysis.get("probability", ""))
    anchor = analysis.get("anchor_price", "")
    cur = analysis.get("current_price", "")
    dev = analysis.get("deviation", "")
    action = analysis.get("action", "")
    pos_scale = analysis.get("position_scale", "")
    engine = analysis.get("engine_check") or {}
    reasons = _split_reasons(analysis.get("reasons") or [])
    risks = analysis.get("risks") or []
    scores = analysis.get("scores") or {}
    kls = analysis.get("key_levels") or {}
    ts = analysis.get("timestamp", "")

    dev_pct = ""
    if anchor:
        try:
            dev_pct = f"{float(dev) / float(anchor) * 100:.4f}%" if dev is not None else ""
        except (TypeError, ValueError):
            dev_pct = ""

    # 引擎信息
    eng_result = engine.get("engine_action", "")
    eng_s0 = engine.get("engine_s0", "")
    eng_net = engine.get("engine_net", "")
    eng_corr = engine.get("corrected", False)
    eng_prob = engine.get("engine_probability", "")

    lines = []
    lines.append("---")
    lines.append(f'title: "BTC 1H 分析 | {slug}"')
    lines.append("type: synthesis")
    lines.append(f"date: {slug_date_str(slug)}")
    lines.append(f"slug: {slug}")
    lines.append(f"direction: {direction}")
    lines.append(f"confidence: {conf}")
    lines.append(f"probability: {prob}")
    lines.append(f"anchor_price: {anchor}")
    lines.append(f"current_price: {cur}")
    lines.append(f"deviation_pct: {dev_pct}")
    lines.append(f"action: {action}")
    lines.append(f"position_scale: {pos_scale}")
    lines.append("source: btc-agent")
    lines.append("tags:")
    lines.append("  - polymarket")
    lines.append("  - btc")
    lines.append("  - hourly")
    lines.append("  - hermes")
    lines.append("  - 8-step-analysis")
    lines.append(f"last_updated: {date.today().isoformat()}")
    lines.append("---")
    lines.append("")
    lines.append(f"# BTC 1H 分析 | {slug}")
    lines.append("")
    lines.append(f"> 分析时间:{ts} | 方向:**{direction}** | 置信度:**{conf}** | 概率:{prob}%")
    lines.append(f"> 锚定价:{fmt_currency(anchor)} | 当前价:{fmt_currency(cur)} | 偏差:**{dev_pct}**")
    lines.append(f"> action:{action} | position_scale:{pos_scale} | 来源:btc-agent")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 决策摘要")
    lines.append(f"- 方向:{direction}（概率 {prob}%）")
    lines.append(f"- 置信度:{conf}")
    lines.append(f"- 引擎复算:action={eng_result} | s0={eng_s0} | net={eng_net} | 概率={eng_prob}")
    lines.append(f"- 是否修正:{'是（引擎向保守方向修正）' if eng_corr else '否'}")
    lines.append("")
    lines.append("## 关键理由（bullet 提炼）")
    for r in reasons[:8]:
        lines.append(f"- {r}")
    if not reasons:
        lines.append("- （无）")
    lines.append("")
    lines.append("## 关键风险")
    for r in risks[:6]:
        lines.append(f"- {r}")
    if not risks:
        lines.append("- （无）")
    lines.append("")
    lines.append("## 评分（模型侧原始分，不做定价依据）")
    lines.append(f"- UP:{scores.get('UP', '')} | DOWN:{scores.get('DOWN', '')}")
    lines.append("")
    lines.append("## 关键位（key_levels）")
    lines.append(f"- 强支撑:{kls.get('strong_support', '')} | 弱支撑:{kls.get('weak_support', '')} | 枢轴:{kls.get('pivot', '')}")
    lines.append(f"- 弱阻力:{kls.get('weak_resistance', '')} | 强阻力:{kls.get('strong_resistance', '')}")
    lines.append("")
    lines.append("## 关联")
    lines.append(f"- 复盘待结算后补链:[[{slug_date_str(slug)}-今日]]")
    lines.append("- 方法论:[[2026-08-13-怎样分析胜率高]]")
    lines.append("")
    lines.append("---")
    lines.append("*本分析由 btc-agent (hermes) 自主执行 · 数据源 hermes_analysis.json · 由 obsidian_sync.py 自动整理*")
    lines.append("*⚠️ 非投资建议 · 决策权归 BOSS*")
    lines.append("")
    return "\n".join(lines)


def gen_review_note(res: dict, detail: dict) -> str:
    """从 hermes_results.jsonl + review-log 详情生成复盘篇。"""
    slug = res.get("slug", "")
    side = res.get("side", "")
    actual = res.get("actual", "")
    amount = res.get("amount")
    pnl = res.get("pnl")
    buy_price = res.get("buy_price")
    ts = res.get("ts", "")

    result = detail.get("result", "")
    if not result:
        if actual and actual == side:
            result = "✅ 预测正确"
        elif actual:
            result = "❌ 预测错误"
        else:
            result = "⏳ 待结算"
    pred_line = detail.get("pred", "")  # 形如 "UP | 置信度=0.518% | EV=$0 | 金额=$0 | 限价=0"
    reason = detail.get("reason", "")
    correct = detail.get("correct", "无")
    wrong = detail.get("wrong", "无")
    lessons = detail.get("lessons", [])
    actual_desc = detail.get("actual", "")

    lines = []
    lines.append("---")
    lines.append(f'title: "复盘 | {slug}"')
    lines.append("type: reflection")
    lines.append("level: L2")
    lines.append(f"date: {slug_date_str(slug)}")
    lines.append(f"slug: {slug}")
    lines.append(f"direction: {side}")
    lines.append(f"actual_side: {actual}")
    lines.append(f"result: {result}")
    lines.append(f"pnl: {pnl}")
    lines.append("tags:")
    lines.append("  - polymarket")
    lines.append("  - btc")
    lines.append("  - hourly")
    lines.append("  - 复盘")
    lines.append("  - hermes")
    lines.append(f"last_updated: {date.today().isoformat()}")
    lines.append("---")
    lines.append("")
    lines.append(f"# 复盘 | {slug}")
    lines.append("")
    lines.append("## 预测 vs 实际")
    # pred_line 已含完整预测详情（side/置信度/EV/金额/限价），直接用；否则 fallback
    if pred_line:
        lines.append(f"- 预测:**{pred_line}**")
    else:
        lines.append(f"- 预测:**{side}** | 金额:{fmt_currency(amount)} | 买入价:{buy_price}")
    if actual_desc:
        lines.append(f"- 实际:**{actual_desc}**")
    else:
        lines.append(f"- 实际:**{actual}** | Binance 结算")
    lines.append(f"- 结果:**{result}** | PnL:{fmt_currency(pnl)}")
    lines.append("")
    lines.append("## 信号原因（当时判断依据）")
    lines.append(f"- {reason if reason else '（无记录）'}")
    lines.append("")
    lines.append("## 指标贡献分析")
    lines.append(f"- 正确贡献:{correct}")
    lines.append(f"- 错误贡献:{wrong}")
    lines.append("")
    lines.append("## ⚠️ 教训（可复用的）")
    for l in lessons:
        lines.append(f"- {l}")
    if not lessons:
        lines.append("- （无记录）")
    lines.append("")
    lines.append("## 关联")
    lines.append(f"- 对应分析:[[{slug}]]（BTC-Hourly-Analysis/）")
    lines.append(f"- 当日汇总:[[{slug_date_str(slug)}-今日]]")
    lines.append("- 方法论:[[2026-08-13-怎样分析胜率高]]")
    lines.append("")
    lines.append("---")
    lines.append("*复盘数据源 hermes_results.jsonl + review-log.md · 由 obsidian_sync.py 自动整理*")
    lines.append("")
    return "\n".join(lines)


def gen_daily_note(d: date, rows, analyses, manual_section: str = "") -> str:
    """生成每日今日.md 汇总。rows=当日已复盘交易，analyses=当日可用分析。
    manual_section=旧文件里保留的手工补充区内容（脚本不覆盖）。"""
    label = d.isoformat()
    lines = []
    lines.append("---")
    lines.append(f'title: "{label} 今日"')
    lines.append("type: synthesis")
    lines.append(f"created: {label}")
    lines.append("tags: [polymarket, btc, daily, hermes]")
    lines.append(f"last_updated: {date.today().isoformat()}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {label} 今日")
    lines.append("")
    lines.append("> Hermes (btc-agent) 逐小时分析 + 复盘汇总页。由 obsidian_sync.py 自动整理。")
    lines.append("")
    lines.append("## 交易信号汇总（已复盘成交）")
    lines.append("| 时段(slug) | 方向 | 实际 | 结果 | PnL | 买入价 | 复盘篇 |")
    lines.append("|---|---|---|---|---|---|---|")
    if rows:
        wins = 0
        total_pnl = 0.0
        for r in sorted(rows, key=lambda x: _slug_sort_key(x.get("slug", ""))):
            slug = r.get("slug", "")
            side = r.get("side", "")
            actual = r.get("actual", "")
            pnl = r.get("pnl")
            if actual and actual == side:
                mark = "✅"
                wins += 1
            elif actual:
                mark = "❌"
            else:
                mark = "待结算"
            try:
                total_pnl += float(pnl or 0)
            except (TypeError, ValueError):
                pass
            lines.append(f"| {slug} | {side} | {actual or '—'} | {mark} | {fmt_currency(pnl)} | {r.get('buy_price', '')} | [[复盘-{slug}]] |")
        lines.append("")
        settled = sum(1 for r in rows if r.get("actual"))
        lines.append(f"- 今日方向统计:命中 **{wins}/{settled}**（已结算 {settled} 笔），胜率 **{wins/settled*100:.1f}%**" if settled else "- 今日暂无已结算成交")
        lines.append(f"- 累计成交:{len(rows)} 笔 | 净利润:**{fmt_currency(total_pnl)}**")
    else:
        lines.append("| （当日暂无已复盘成交） | | | | | | |")
        lines.append("")
        lines.append("- 今日暂无已复盘成交")
    lines.append("")
    lines.append("## 当日可用分析（hermes_analysis.json 最新）")
    for a in analyses:
        slug = a.get("slug", "")
        lines.append(f"- [[{slug}]] — {a.get('direction', '')} {_fmt_prob(a.get('probability', ''))}% 置信度{a.get('confidence', '')}")
    if not analyses:
        lines.append("- （无）")
    lines.append("")
    lines.append(MANUAL_MARKER_HEAD)
    lines.append("")
    if manual_section:
        lines.append(manual_section)
    else:
        lines.append(f"- {MANUAL_PLACEHOLDER}")
    lines.append("")
    lines.append(MANUAL_MARKER_FOOT)
    lines.append(f"- 昨日:[[{(d - timedelta(days=1)).isoformat()}-今日]]")
    lines.append("- 方法论:[[2026-08-13-怎样分析胜率高]]")
    lines.append("- 完整分析目录:[[BTC-Hourly-Analysis-Index]]")
    lines.append("- 归档规范:[[Hermes-Obsidian-归档规范]]")
    lines.append("")
    lines.append("---")
    lines.append("*本页由 obsidian_sync.py 自动整理 · 数据源 hermes_results.jsonl + hermes_analysis.json*")
    lines.append("")
    return "\n".join(lines)


def gen_index() -> str:
    """生成 BTC-Hourly-Analysis-Index.md。扫描目录实际文件，不受 --since 影响。"""
    slugs_by_date = {}
    if HOURLY_DIR.exists():
        for f in HOURLY_DIR.glob("*.md"):
            if f.name == "BTC-Hourly-Analysis-Index.md":
                continue
            stem = f.stem
            p = parse_slug(stem)
            if not p:
                # 兼容带日期前缀的旧文件名：YYYY-MM-DD-<slug>
                m = re.match(r"^\d{4}-\d{2}-\d{2}-(.+)$", stem)
                if m:
                    p = parse_slug(m.group(1))
            if not p:
                continue
            d, h = p
            slugs_by_date.setdefault(d.isoformat(), []).append((h, f.stem))
    for ds in slugs_by_date:
        slugs_by_date[ds] = [s for _, s in sorted(slugs_by_date[ds])]

    lines = []
    lines.append("---")
    lines.append('title: "BTC Hourly Analysis 目录索引"')
    lines.append("type: MOC")
    lines.append("tags: [polymarket, btc, hourly, index, hermes]")
    lines.append(f"last_updated: {date.today().isoformat()}")
    lines.append("---")
    lines.append("")
    lines.append("# BTC Hourly Analysis 目录索引")
    lines.append("")
    lines.append("> Hermes (btc-agent) 每小时 1H up/down 分析归档目录。由 obsidian_sync.py 自动维护。")
    lines.append("")
    lines.append("## 归档规则（必读）")
    lines.append("- **每篇位置**:本目录 `Polymarket/BTC-Hourly-Analysis/<slug>.md`，命名即 slug")
    lines.append("- **数据源**:`hermes_analysis.json`（当前分析）+ `hermes_results.jsonl`（复盘）")
    lines.append("- **写入时机**:obsidian_sync.py 定时运行自动生成")
    lines.append("- **铁律**:Karpathy 五原则 + 复盘不覆盖原分析（双链互连）")
    lines.append("- **限制**:hermes_analysis.json 仅保留最新一份，历史小时分析篇无法自动重建（数据源客观缺失）")
    lines.append("")
    for date_str in sorted(slugs_by_date, reverse=True):
        lines.append(f"## {date_str}")
        for slug in slugs_by_date[date_str]:
            lines.append(f"- [[{slug}]]")
        lines.append("")
    lines.append("## 关联")
    lines.append("- 归档规范:[[Hermes-Obsidian-归档规范]]")
    lines.append("- 方法论:[[2026-08-13-怎样分析胜率高]]")
    lines.append("")
    lines.append("---")
    lines.append("*索引由 obsidian_sync.py 自动维护*")
    lines.append("")
    return "\n".join(lines)


# ============ 主流程 ============
def main():
    ap = argparse.ArgumentParser(description="Hermes 交易分析记录同步到 Obsidian")
    ap.add_argument("--dry-run", action="store_true", help="只打印动作不写文件")
    ap.add_argument("--analysis-only", action="store_true", help="只同步当前分析篇")
    ap.add_argument("--since", help="只同步该日期(YYYY-MM-DD)之后的复盘")
    args = ap.parse_args()

    if not VAULT.exists():
        print(f"❌ Obsidian 库不存在: {VAULT}", file=sys.stderr)
        sys.exit(1)

    done_actions = []
    skip_actions = []

    # ---- 1. 当前分析篇 ----
    analysis = None
    if ANALYSIS_FILE.exists():
        try:
            analysis = json.loads(ANALYSIS_FILE.read_text())
        except json.JSONDecodeError:
            analysis = None
    if analysis and analysis.get("slug"):
        slug = analysis["slug"]
        target = HOURLY_DIR / f"{slug}.md"
        content = gen_analysis_note(analysis)
        if args.dry_run:
            done_actions.append(f"[analysis] 将写入 {target}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            done_actions.append(f"[analysis] 已写入 {target}")
    elif args.dry_run:
        print("ℹ️ 无当前分析（hermes_analysis.json 为空）")

    if args.analysis_only:
        _report(done_actions, skip_actions)
        return

    # ---- 2. 复盘篇（全历史，幂等） ----
    results = read_jsonl(RESULTS_FILE)
    log_details = load_review_log_details()
    since_d = None
    if args.since:
        try:
            since_d = date.fromisoformat(args.since)
        except ValueError:
            print(f"❌ 无效 --since 日期: {args.since}", file=sys.stderr)
            sys.exit(1)

    by_date = {}
    for res in results:
        slug = res.get("slug", "")
        d, _ = parse_slug(slug)
        if not d:
            continue
        if since_d and d < since_d:
            continue
        by_date.setdefault(d, []).append(res)

    for d in sorted(by_date):
        day_dir = VAULT / "Polymarket" / d.isoformat()
        for res in by_date[d]:
            slug = res["slug"]
            target = day_dir / f"复盘-{slug}.md"
            if target.exists():
                skip_actions.append(f"[review] 已存在跳过 {target}")
                continue
            content = gen_review_note(res, log_details.get(slug, {}))
            if args.dry_run:
                done_actions.append(f"[review] 将写入 {target}")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
                done_actions.append(f"[review] 已写入 {target}")

    # ---- 3. 每日今日.md ----
    # 遍历"有复盘 ∪ 当前分析"的日期并集，保证每天一篇今日.md
    days_union = set(by_date.keys())
    cur_analysis_d = None
    if analysis and analysis.get("slug"):
        p = parse_slug(analysis["slug"])
        if p:
            cur_analysis_d = p[0]
            days_union.add(p[0])
    for d in sorted(days_union):
        day_dir = VAULT / "Polymarket" / d.isoformat()
        today_target = day_dir / f"{d.isoformat()}-今日.md"
        rows = by_date.get(d, [])
        cur_analysis = analysis if (cur_analysis_d == d) else None
        # 保留旧文件手工补充区（今日主线/自检/教训），避免脚本重建覆盖
        manual = ""
        if today_target.exists():
            manual = extract_manual_section(today_target.read_text())
        content = gen_daily_note(d, rows, [cur_analysis] if cur_analysis else [], manual)
        if args.dry_run:
            done_actions.append(f"[daily] 将写入 {today_target}")
        else:
            day_dir.mkdir(parents=True, exist_ok=True)
            today_target.write_text(content)
            done_actions.append(f"[daily] 已写入 {today_target}")

    # ---- 4. Index（扫描目录实际文件重建，不受 --since 影响）----
    if not args.dry_run:
        INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
        INDEX_FILE.write_text(gen_index())
        done_actions.append(f"[index] 已写入 {INDEX_FILE}")
    else:
        done_actions.append(f"[index] 将写入 {INDEX_FILE}")

    _report(done_actions, skip_actions)


def _report(done, skip):
    print(f"=== obsidian_sync 完成 ===")
    for a in done:
        print(f"  ✓ {a}")
    if skip:
        print(f"  ⏭️ 跳过 {len(skip)} 个已存在:")
        for s in skip[:20]:
            print(f"    {s}")
    print(f"共执行 {len(done)} 个动作，跳过 {len(skip)} 个。")


if __name__ == "__main__":
    main()
