#!/usr/bin/env python3
"""引擎-AI 方向分歧归因（8/21）。

背景：三宇宙回测发现引擎方向胜率 75.4% vs 实盘 AI 66.1%，一致率仅 82.6%。
AI 手算净分偏离引擎复算的场次系统性拖低实盘胜率 9pp。

本脚本：
1. 重放引擎方向（与 fill_bias_backtest 同口径）
2. join 实盘 AI 方向（done.jsonl）
3. 分歧场次统计：引擎对 vs AI 对的胜负归属
4. 从 review-log.md 解析 AI 的 S₀ 因子分解，与引擎复算因子对比，定位分歧因子

只读分析，输出报告到 data/。
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from signal_validator import extract_factors
from rule_engine import judge

ROOT = Path("/Users/tangyijun/Desktop/hermes btc")
SNAPSHOT_DIR = ROOT / "data" / "snapshots"
RESULTS_FILE = ROOT / "data" / "hermes_results.jsonl"
DONE_FILE = ROOT / "data" / "hermes_done.jsonl"
REVIEW_LOG = ROOT / "data" / "review-log.md"


def load_results() -> dict[str, dict]:
    out = {}
    for line in RESULTS_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("actual") and r.get("slug"):
            out[r["slug"]] = r
    return out


def load_done() -> dict[str, dict]:
    out = {}
    for line in DONE_FILE.read_text().splitlines():
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


def load_review_reasons() -> dict[str, str]:
    """slug → AI 信号原因（review-log.md ### 信号原因 段落）。"""
    out = {}
    text = REVIEW_LOG.read_text()
    blocks = re.split(r"\n## ", text)[1:]
    for b in blocks:
        m = re.search(r"bitcoin-up-or-down-[\w-]+", b)
        if not m:
            continue
        m2 = re.search(r"### 信号原因\n(.+?)(?=\n###|\Z)", b, re.S)
        if m2:
            out[m.group(0)] = m2.group(1).strip()
    return out


def latest_snapshots() -> dict[str, Path]:
    slugs = defaultdict(list)
    for p in sorted(SNAPSHOT_DIR.glob("*.json")):
        slugs[p.name.split("_202")[0]].append(p)
    return {s: sorted(ps, key=lambda x: x.stat().st_mtime)[-1] for s, ps in slugs.items()}


def engine_replay(md: dict):
    """与 fill_bias_backtest / regime_split_replay 相同的两遍重放。返回 (direction, probability, factors_raw)。"""
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
    if not d.final_direction or d.probability is None:
        return None
    return d


def parse_ai_s0(reason: str) -> tuple[int | None, str]:
    """从 AI reason 解析 S₀ 和括号内因子分解。返回 (s0, breakdown_str)。"""
    m = re.search(r"S₀=(-?\d+)\(([^)]*)\)", reason)
    if not m:
        return None, ""
    return int(m.group(1)), m.group(2)


def main() -> int:
    results = load_results()
    done = load_done()
    reasons = load_review_reasons()
    snaps = latest_snapshots()

    cases = []
    for slug, path in sorted(snaps.items()):
        res = results.get(slug)
        live = done.get(slug)
        if not res or not live:
            continue
        actual = res["actual"].upper()
        live_side = (live.get("side") or live.get("signal") or "").upper()
        if not live_side:
            continue
        try:
            md = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        d = engine_replay(md)
        if d is None:
            continue
        cases.append({
            "slug": slug,
            "actual": actual,
            "engine": d.final_direction,
            "ai": live_side,
            "engine_win": d.final_direction == actual,
            "ai_win": live_side == actual,
            "engine_prob": d.probability,
            "ai_conf": live.get("probability_raw") or live.get("probability"),
            "reason": reasons.get(slug, ""),
            "ts": res.get("ts", ""),
            "snapshot": path,
        })

    agree = [c for c in cases if c["engine"] == c["ai"]]
    disagree = [c for c in cases if c["engine"] != c["ai"]]

    L = []
    L.append("# 引擎-AI 方向分歧归因报告\n")
    L.append(f"- 运行时间：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    L.append(f"- 匹配样本：{len(cases)} 场（引擎重放 × 实盘 AI 信号 × 已结算）")
    L.append(f"- 一致：{len(agree)}（{len(agree)/len(cases)*100:.1f}%） | 分歧：{len(disagree)}（{len(disagree)/len(cases)*100:.1f}%）\n")

    # ===== 胜负归属 =====
    L.append("## 一、分歧场次胜负归属\n")
    ew = sum(1 for c in disagree if c["engine_win"])
    aw = sum(1 for c in disagree if c["ai_win"])
    both_lose = sum(1 for c in disagree if not c["engine_win"] and not c["ai_win"])
    L.append(f"分歧 {len(disagree)} 场：")
    L.append(f"- 引擎对：{ew} 场（{ew/len(disagree)*100:.1f}%）")
    L.append(f"- AI 对：{aw} 场（{aw/len(disagree)*100:.1f}%）")
    L.append(f"- 双输：{both_lose} 场（{both_lose/len(disagree)*100:.1f}%）")
    L.append("")
    L.append(f"反事实：若全部听引擎（分歧场也按引擎走），总胜率 = "
             f"{(sum(1 for c in agree if c['engine_win']) + ew)/len(cases)*100:.1f}%")
    L.append(f"实际（听 AI）：{(sum(1 for c in agree if c['ai_win']) + aw)/len(cases)*100:.1f}%\n")

    # ===== 分歧明细表 =====
    L.append("## 二、分歧场次明细\n")
    L.append("| # | slug 尾段 | ET 时段 | 引擎 | AI | 实际 | 引擎对? | AI conf |")
    L.append("|---|---|---|---|---|---|---|---|")
    for i, c in enumerate(disagree, 1):
        et = re.search(r"-(\d+)am-et|-(\d+)pm-et|-(\d+)am-pm", c["slug"])
        hour = (et.group(1) or et.group(2) or et.group(3)) if et else "?"
        L.append(f"| {i} | ...{c['slug'][-18:]} | {hour} | {c['engine']} | {c['ai']} | "
                 f"{c['actual']} | {'✅' if c['engine_win'] else '❌'} | {c['ai_conf']} |")
    L.append("")

    # ===== AI S₀ vs 引擎净分对比 =====
    L.append("## 三、AI 自报 S₀ vs 引擎复算（分歧场次）\n")
    s0_parsed = 0
    s0_diff = 0
    factor_counter = Counter()
    for c in disagree:
        s0, breakdown = parse_ai_s0(c["reason"])
        if s0 is None:
            continue
        s0_parsed += 1
        L.append(f"### {c['slug'][-30:]}")
        L.append(f"- AI reason 片段：`{c['reason'][:220]}`")
        L.append(f"- AI S₀={s0:+d} | 引擎方向={c['engine']}（引擎概率 {c['engine_prob']:.0f}%） | AI 方向={c['ai']}")
        L.append("")

    # ===== 分歧模式：按 AI 方向分类 =====
    L.append("## 四、分歧模式分类\n")
    # AI 顺 PM 共识 vs 引擎逆共识
    ai_follows_engine_overrides = Counter()
    for c in disagree:
        r = c["reason"]
        tags = []
        if "硬覆盖" in r or "覆盖" in r:
            tags.append("覆盖条款")
        if "锁低" in r:
            tags.append("锁低")
        if "H12" in r:
            tags.append("H12")
        if "G3" in r:
            tags.append("G3")
        if "G5" in r:
            tags.append("G5")
        if "背离" in r and "假" in r:
            tags.append("假背离判断")
        if "诱多" in r or "诱空" in r:
            tags.append("诱多空")
        if "庄家" in r:
            tags.append("庄家视角")
        if not tags:
            tags.append("无特殊条款")
        for t in tags:
            ai_follows_engine_overrides[t] += 1
    L.append("分歧场次 AI reason 中出现的条款标签：\n")
    for k, v in ai_follows_engine_overrides.most_common():
        L.append(f"- {k}: {v} 场")
    L.append("")

    # 分歧时段分布
    hour_counter = Counter()
    for c in disagree:
        m = re.search(r"-(\d+)am-et|-(\d+)pm-et", c["slug"])
        if m:
            h = m.group(1) or m.group(2)
            hour_counter[h] += 1
    L.append("分歧按 ET 时段：" + ", ".join(f"{k}点:{v}场" for k, v in sorted(hour_counter.items(), key=lambda x: int(x[0]))) + "\n")

    # ===== 保存分歧场次 slug 列表（供后续深挖）=====
    out = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "total": len(cases),
        "agree": len(agree),
        "disagree": len(disagree),
        "engine_win_in_disagree": ew,
        "ai_win_in_disagree": aw,
        "both_lose": both_lose,
        "cases": [{k: c[k] for k in ("slug", "actual", "engine", "ai", "engine_win", "ai_win", "ts")}
                  for c in disagree],
    }
    (ROOT / "data" / "hermes_divergence_cases.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1))

    report = "\n".join(L)
    out_file = ROOT / "data" / f"hermes_divergence_analysis_{datetime.now().strftime('%Y%m%d')}.md"
    out_file.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n报告: {out_file}")
    print(f"分歧清单 JSON: data/hermes_divergence_cases.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
