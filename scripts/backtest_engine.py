#!/usr/bin/env python3
"""
Hermes 事件驱动回测器（升级计划书 任务 1.1，2026-08-22）。

定位：
    把"人工反事实统计"升级为可重复的事件驱动回测：
    快照事件流 → 策略产生信号 → 按 PM 盘口撮合 → 用真实结算标签算 P&L
    → 权益曲线 + 标准绩效指标（胜率/Wilson95/EV/最大回撤/Sharpe/Sortino/成交率）。

安全声明：只读历史数据；输出仅写 data/ 下新报告文件，不改任何现有文件。

撮合模型 v1（诚实简化，参数可调）：
    - 入场价 = 快照内 Polymarket 该方向价格（信号时刻 ask）
    - 挂单上限 max_price（默认 0.60）：ask ≤ 上限才成交，成交价 = min(ask, 上限)
      （保守口径：按 ask 成交，不假设排队优化）
    - 手续费 fee 默认 0（Polymarket 1H 市场现行无交易费；留参数位）
    - 无部分成交/排队模拟；结算 = 实际标签 actual（results.jsonl）

策略接口：
    class Strategy:
        name = "..."
        def on_event(self, ev) -> dict | None:
            # ev = {"slug","md"(原始快照dict),"hour_et"}
            # 返回 {"side":"UP"/"DOWN","limit":可选,"prob":可选,"note":可选} 或 None

内置策略：
    follow_pm / fade_pm / always_up / always_down / random
    hermes_engine = signal_validator.extract_factors + rule_engine.judge 全引擎重放
                    （与实盘同链路，两遍 extreme_wall 复算，复用 replay_common.replay_judge_full）

用法：
    python3 scripts/backtest_engine.py                          # 全部基准策略
    python3 scripts/backtest_engine.py --strategies hermes_engine
    python3 scripts/backtest_engine.py --pick last --start 2026-08-15
    python3 scripts/backtest_engine.py --validate               # 引擎重放 vs 实盘信号一致性
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERMES_DIR = os.environ.get("HERMES_DIR")
ROOT = Path(_HERMES_DIR) if _HERMES_DIR else Path("/Users/tangyijun/Desktop/hermes btc")
sys.path.insert(0, str(ROOT / "scripts"))

from replay_common import SNAPSHOT_DIR, load_results_full, wilson, replay_judge_full  # noqa: E402


# ============================================================
# 快照加载（first=信号窗口前瞻最小 / last=与旧回放口径一致）
# ============================================================

def get_snapshots(pick: str = "first") -> dict[str, Path]:
    slugs: dict[str, list[tuple[str, Path]]] = defaultdict(list)
    for p in sorted(SNAPSHOT_DIR.glob("*.json")):
        stem = p.stem
        if "_202" not in stem:
            continue
        slug, _, ts = stem.partition("_202")
        slugs[slug].append(("202" + ts, p))
    out = {}
    for slug, items in slugs.items():
        items.sort(key=lambda x: x[0])
        out[slug] = items[0][1] if pick == "first" else items[-1][1]
    return out


_MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}


def slug_date(slug: str):
    import re
    from datetime import date as _date
    m = re.search(r"-([a-z]+)-(\d+)-(\d{4})-", slug)
    if not m:
        return None
    try:
        return _date(int(m.group(3)), _MONTHS.get(m.group(1), 0), int(m.group(2)))
    except ValueError:
        return None


# ============================================================
# 策略
# ============================================================

class Strategy:
    name = "base"

    def on_event(self, ev: dict) -> dict | None:
        raise NotImplementedError


def _pm(md: dict) -> dict:
    return md.get("polymarket") or {}


class FollowPM(Strategy):
    name = "follow_pm"

    def on_event(self, ev):
        pm = _pm(ev["md"])
        up, down = pm.get("up_price"), pm.get("down_price")
        if not isinstance(up, (int, float)) or not isinstance(down, (int, float)):
            return None
        return {"side": "UP" if up >= down else "DOWN", "note": f"pm {up:.2f}/{down:.2f}"}


class FadePM(FollowPM):
    name = "fade_pm"

    def on_event(self, ev):
        sig = super().on_event(ev)
        if not sig:
            return None
        sig["side"] = "DOWN" if sig["side"] == "UP" else "UP"
        sig["note"] = "fade " + sig["note"]
        return sig


class AlwaysSide(Strategy):
    def __init__(self, side: str):
        self.name = f"always_{side.lower()}"
        self._side = side

    def on_event(self, ev):
        return {"side": self._side, "note": "static"}


class RandomSide(Strategy):
    def __init__(self, seed: int = 42):
        self.name = "random"
        self._seed = seed
        self._rng = random.Random(seed)

    def on_event(self, ev):
        return {"side": self._rng.choice(["UP", "DOWN"]), "note": f"seed{self._seed}"}


class HermesRuleEngine(Strategy):
    """完整规则引擎重放：extract_factors → 两遍 judge（实盘同链路）。"""

    name = "hermes_engine"

    def __init__(self, max_price: float = 0.60):
        self._cap = max_price

    def on_event(self, ev):
        md = ev["md"]
        if (md.get("polymarket") or {}).get("active") is False:
            return None
        res = replay_judge_full(md)
        if res is None:
            return None
        d, factors, inp = res
        if getattr(d, "action", "") == "SKIP":
            return None
        side = getattr(d, "final_direction", None)
        if side not in ("UP", "DOWN"):
            return None
        prob = getattr(d, "probability", None)
        try:
            prob = round(float(prob) / 100, 4) if float(prob) > 1 else round(float(prob), 4)
        except (TypeError, ValueError):
            prob = None
        return {"side": side, "limit": self._cap, "prob": prob,
                "note": f"net={getattr(d, 'net', '?')} conf={getattr(d, 'confidence', '?')}"}


def build_strategies(names: list[str], max_price: float) -> list[Strategy]:
    catalog = {
        "follow_pm": FollowPM,
        "fade_pm": FadePM,
        "always_up": lambda: AlwaysSide("UP"),
        "always_down": lambda: AlwaysSide("DOWN"),
        "random": RandomSide,
        "hermes_engine": lambda: HermesRuleEngine(max_price),
    }
    out = []
    for n in names:
        if n not in catalog:
            raise SystemExit(f"未知策略: {n}（可选: {', '.join(catalog)}）")
        out.append(catalog[n]())
    return out


# ============================================================
# 回测器
# ============================================================

def run_backtest(strategy, events, labels, max_price=0.60, fee=0.0) -> dict:
    """单策略回放。返回 trades + metrics。"""
    trades = []
    for ev in events:
        sig = strategy.on_event(ev)
        if not sig or sig.get("side") not in ("UP", "DOWN"):
            continue
        slug = ev["slug"]
        pm = ev["md"].get("polymarket") or {}
        ask = pm.get("up_price") if sig["side"] == "UP" else pm.get("down_price")
        if not isinstance(ask, (int, float)) or ask <= 0 or ask >= 1:
            continue
        limit = sig.get("limit") or max_price
        pay = min(ask, limit)
        lab = labels.get(slug)
        actual = lab.get("actual") if lab else None
        filled = ask <= limit
        win = None
        pnl = 0.0
        if filled and actual in ("UP", "DOWN"):
            win = 1 if actual == sig["side"] else 0
            pnl = (1 - pay) * (1 - fee) if win else -pay * (1 + fee)
        trades.append({"slug": slug, "side": sig["side"], "ask": round(ask, 4),
                       "pay": round(pay, 4), "filled": filled, "win": win,
                       "pnl": round(pnl, 4), "prob": sig.get("prob"),
                       "note": sig.get("note", "")})

    settled = [t for t in trades if t["filled"] and t["win"] is not None]
    n_sig, n_fill, n_set = len(trades), sum(1 for t in trades if t["filled"]), len(settled)
    wins = sum(t["win"] for t in settled)
    total_pnl = sum(t["pnl"] for t in settled)

    # 权益曲线（每单 1 share 固定注码）
    eq, peak, maxdd = 0.0, 0.0, 0.0
    curve = [0.0]
    rets = []
    for t in settled:
        eq += t["pnl"]
        rets.append(t["pnl"])
        curve.append(round(eq, 4))
        peak = max(peak, eq)
        maxdd = max(maxdd, peak - eq)

    wr = wins / n_set if n_set else 0.0
    lo, hi = wilson(wins, n_set)
    mean_pnl = total_pnl / n_set if n_set else 0.0
    # 每单 Sharpe/Sortino（年化按 24×365 单）
    if len(rets) > 1:
        mu = sum(rets) / len(rets)
        var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
        sd = math.sqrt(var)
        downside = [r for r in rets if r < 0]
        dsd = math.sqrt(sum(r ** 2 for r in downside) / len(rets)) if downside else 0.0
        sharpe = mu / sd * math.sqrt(24 * 365) if sd > 0 else 0.0
        sortino = mu / dsd * math.sqrt(24 * 365) if dsd > 0 else 0.0
    else:
        sharpe = sortino = 0.0

    # 概率校准（若策略输出 prob）
    probs = [(t["prob"], t["win"]) for t in settled if isinstance(t.get("prob"), (int, float))]
    brier = (sum((p - w) ** 2 for p, w in probs) / len(probs)) if probs else None

    metrics = {
        "strategy": strategy.name,
        "n_signals": n_sig,
        "n_filled": n_fill,
        "fill_rate": round(n_fill / n_sig, 3) if n_sig else 0.0,
        "n_settled": n_set,
        "wins": wins,
        "win_rate": round(wr, 4),
        "wilson95": [round(lo, 1), round(hi, 1)],
        "avg_pay": round(sum(t["pay"] for t in settled) / n_set, 4) if n_set else 0,
        "ev_per_trade": round(mean_pnl, 4),
        "total_pnl_per_share": round(total_pnl, 2),
        "max_drawdown_per_share": round(maxdd, 2),
        "sharpe_ann": round(sharpe, 2),
        "sortino_ann": round(sortino, 2),
        "brier": round(brier, 4) if brier is not None else None,
    }
    return {"metrics": metrics, "trades": trades, "equity_curve": curve}


# ============================================================
# 引擎重放 vs 实盘信号一致性校验（计划书验收 1.1）
# ============================================================

def validate_engine(events, labels) -> dict:
    agree = diff = skip = 0
    diffs = []
    for ev in events:
        lab = labels.get(ev["slug"])
        if not lab or lab.get("side") not in ("UP", "DOWN"):
            continue
        res = replay_judge_full(ev["md"])
        if res is None:
            skip += 1
            continue
        d, _, _ = res
        rep = getattr(d, "final_direction", None)
        if rep not in ("UP", "DOWN"):
            skip += 1
            continue
        if rep == lab["side"]:
            agree += 1
        else:
            diff += 1
            diffs.append({"slug": ev["slug"], "live": lab["side"], "replay": rep})
    total = agree + diff
    return {"n_compared": total, "agree": agree, "diff": diff, "skipped": skip,
            "agreement_pct": round(agree / total * 100, 1) if total else 0.0,
            "diffs": diffs}


# ============================================================
# 报告
# ============================================================

def fmt_report(results: list[dict], meta: dict) -> str:
    lines = [
        "# Hermes 事件驱动回测报告",
        "",
        f"- 生成(UTC)：{meta['generated_at_utc']}",
        f"- 快照口径：pick={meta['pick']}（first=信号窗口，前瞻偏差最小）",
        f"- 事件数：{meta['n_events']}（有结算标签：{meta['n_labelled']}）",
        f"- 撮合：max_price={meta['max_price']}，fee={meta['fee']}，按 ask 成交的保守口径",
        "",
        "| 策略 | 信号 | 成交率 | 结算 | 胜率 | Wilson95 | 均价 | EV/单 | 总P&L/share | 最大回撤 | Sharpe年化 | Brier |",
        "|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        m = r["metrics"]
        w = f"{m['wilson95'][0]}~{m['wilson95'][1]}%"
        b = f"{m['brier']:.3f}" if m["brier"] is not None else "—"
        lines.append(
            f"| {m['strategy']} | {m['n_signals']} | {m['fill_rate']*100:.0f}% | {m['n_settled']} "
            f"| {m['win_rate']*100:.1f}% | {w} | {m['avg_pay']:.3f} | {m['ev_per_trade']*100:+.1f}% "
            f"| {m['total_pnl_per_share']:+.2f} | {m['max_drawdown_per_share']:.2f} "
            f"| {m['sharpe_ann']} | {b} |")
    lines += ["", "> EV/单 = 每单固定 1 share 注码的期望盈亏；Sharpe/Sortino 按每小时一单年化。",
              "> 本表为历史统计，非未来收益保证；小样本 Wilson 区间宽属正常。", ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Hermes 事件驱动回测器（任务1.1）")
    ap.add_argument("--strategies", default="follow_pm,fade_pm,always_up,always_down,random,hermes_engine")
    ap.add_argument("--pick", choices=["first", "last"], default="first")
    ap.add_argument("--max-price", type=float, default=0.60)
    ap.add_argument("--fee", type=float, default=0.0)
    ap.add_argument("--start", default=None, help="YYYY-MM-DD 起始（含）")
    ap.add_argument("--end", default=None, help="YYYY-MM-DD 结束（含）")
    ap.add_argument("--validate", action="store_true", help="引擎重放 vs 实盘信号一致性")
    ap.add_argument("--report", default="data/backtest_report.md")
    ap.add_argument("--json", default=None, help="可选：明细 JSON 输出路径")
    args = ap.parse_args()

    from datetime import date as _date
    labels = load_results_full()
    snaps = get_snapshots(args.pick)

    events = []
    for slug, path in sorted(snaps.items()):
        d0 = slug_date(slug)
        if args.start and d0 and d0 < _date.fromisoformat(args.start):
            continue
        if args.end and d0 and d0 > _date.fromisoformat(args.end):
            continue
        try:
            md = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        events.append({"slug": slug, "md": md})

    n_labelled = sum(1 for e in events if labels.get(e["slug"], {}).get("actual") in ("UP", "DOWN"))
    meta = {"generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "pick": args.pick, "max_price": args.max_price, "fee": args.fee,
            "n_events": len(events), "n_labelled": n_labelled}

    print(f"═══ Hermes 事件驱动回测（{len(events)} 事件，{n_labelled} 已结算，pick={args.pick}）═══")

    results = []
    strat_names = [s.strip() for s in args.strategies.split(",") if s.strip()]
    for st in build_strategies(strat_names, args.max_price):
        t0 = datetime.now()
        r = run_backtest(st, events, labels, args.max_price, args.fee)
        results.append(r)
        m = r["metrics"]
        print(f"· {m['strategy']:<14} 信号{m['n_signals']:>4} 成交率{m['fill_rate']*100:>3.0f}% "
              f"胜率{m['win_rate']*100:>5.1f}% (n={m['n_settled']}) "
              f"EV/单{m['ev_per_trade']*100:>+5.1f}% 总P&L{m['total_pnl_per_share']:>+7.2f} "
              f"回撤{m['max_drawdown_per_share']:>5.2f} [{(datetime.now()-t0).total_seconds():.1f}s]")

    val = None
    if args.validate:
        val = validate_engine(events, labels)
        print(f"· 引擎重放一致性: {val['agree']}/{val['n_compared']} = {val['agreement_pct']}%"
              f"（分歧 {val['diff']}，跳过 {val['skipped']}）")

    report = fmt_report(results, meta)
    if val:
        report += ("## 引擎重放 vs 实盘信号一致性\n\n"
                   f"- 比对场次：{val['n_compared']}｜一致：{val['agree']}｜分歧：{val['diff']}"
                   f"｜跳过：{val['skipped']}\n- 一致率：**{val['agreement_pct']}%**\n"
                   "- 分歧清单（live=实盘方向, replay=引擎重放）：\n")
        for x in val["diffs"][:20]:
            report += f"  - {x['slug']}: {x['live']} → {x['replay']}\n"

    rp = ROOT / args.report
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(report, encoding="utf-8")
    if args.json:
        jp = ROOT / args.json
        jp.parent.mkdir(parents=True, exist_ok=True)
        jp.write_text(json.dumps({"meta": meta, "results": results, "validate": val},
                                 ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"报告: {rp}" + (f"｜JSON: {ROOT / args.json}" if args.json else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
