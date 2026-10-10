#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round50 v3 聪明钱池子滚动筛选 (2026-08-28 用户决策「滚动盈利钱包池」第 3 步)

职责: 读账本 (data/hermes_smart_money_ledger.jsonl), 按钱包聚合参与次数/胜率/累计盈亏,
筛出滚动盈利池写入 data/hermes_smart_money_pool.json。
采集器 hermes_collect v3 每小时读该文件标记池内钱包持仓方向 (smart_money_pool_direction)。

筛选条件 (默认, 可参数覆盖):
- min_rounds   = 10  参与 ≥10 个已结算小时 (防一次性豪赌幸存者)
- min_pnl      = 100 累计 pnl_shares > 100 (实测盈利, 不靠持仓大)
- min_winrate  = 0.55 胜率 ≥55% (过滤重仓单场侥幸)
- window_days  = 7   只统计最近 N 天账本 (滚动淘汰状态下滑的钱包)

样本不足 (< min_rounds 最高参与次数或 < 72h) 仍输出文件但 note 标注, 采集器照常读。

用法:
  python3 scripts/smart_money_pool.py                     # 默认参数
  python3 scripts/smart_money_pool.py --min-rounds 5      # 早期样本少时放宽
  python3 scripts/smart_money_pool.py --dry-run           # 只打印不写文件
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = THIS_DIR / "data"
LEDGER_JL = DATA_DIR / "hermes_smart_money_ledger.jsonl"
POOL_JSON = DATA_DIR / "hermes_smart_money_pool.json"


def load_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def aggregate_wallets(ledger: list, window_days: int = 7) -> dict:
    """纯函数: 聚合每钱包 {rounds, wins, pnl_shares, pnl_usd_est, name, last_slug}。

    rounds = 参与的不同 slug 数 (钱包可能两侧都持, 按 slug 去重计一次)。
    pnl 按行累加 (同 slug 同钱包多行时账本端已去重, 此处直接求和)。
    """
    cutoff = None
    if window_days and ledger:
        # 用账本最新 settled_at 作窗口锚点, 避免「现在时间」导致测试不可复现
        latest = max(str(r.get("settled_at", "")) for r in ledger)
        try:
            cutoff_dt = datetime.fromisoformat(latest) - timedelta(days=window_days)
            cutoff = cutoff_dt.isoformat()
        except ValueError:
            cutoff = None
    agg = {}
    for r in ledger:
        if cutoff is not None and str(r.get("settled_at", "")) < cutoff:
            continue
        w = r.get("wallet")
        if not w:
            continue
        a = agg.setdefault(w, {
            "wallet": w, "name": r.get("name") or "", "slugs": set(),
            "wins": 0, "pnl_shares": 0.0, "pnl_usd_est": 0.0, "last_slug": "",
        })
        a["slugs"].add(r.get("slug"))
        if r.get("win"):
            a["wins"] += 1
        a["pnl_shares"] += float(r.get("pnl_shares", 0) or 0)
        a["pnl_usd_est"] += float(r.get("pnl_usd_est", 0) or 0)
        if str(r.get("settled_at", "")) > str(a.get("last_settled", "")):
            a["last_settled"] = r.get("settled_at", "")
            a["last_slug"] = r.get("slug", "")
    for a in agg.values():
        a["rounds"] = len(a["slugs"])
    return agg


def select_pool(agg: dict, min_rounds: int = 10, min_pnl: float = 100.0,
                min_winrate: float = 0.55) -> list:
    """纯函数: 按条件筛池子, 返回按 pnl_shares 降序的钱包列表。"""
    pool = []
    for a in agg.values():
        rounds = a["rounds"]
        winrate = a["wins"] / rounds if rounds > 0 else 0.0
        if rounds < min_rounds:
            continue
        if a["pnl_shares"] <= min_pnl:
            continue
        if winrate < min_winrate:
            continue
        pool.append({
            "wallet": a["wallet"],
            "name": a["name"],
            "rounds": rounds,
            "wins": a["wins"],
            "win_rate": round(winrate, 4),
            "pnl_shares": round(a["pnl_shares"], 2),
            "pnl_usd_est": round(a["pnl_usd_est"], 2),
            "last_slug": a["last_slug"],
        })
    pool.sort(key=lambda x: x["pnl_shares"], reverse=True)
    return pool


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-rounds", type=int, default=10)
    ap.add_argument("--min-pnl", type=float, default=100.0)
    ap.add_argument("--min-winrate", type=float, default=0.55)
    ap.add_argument("--window-days", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ledger = load_jsonl(LEDGER_JL)
    if not ledger:
        print("账本为空 (hermes_smart_money_ledger.jsonl 不存在或无记录), 先跑 smart_money_settle.py")
        return 0
    agg = aggregate_wallets(ledger, window_days=args.window_days)
    pool = select_pool(agg, args.min_rounds, args.min_pnl, args.min_winrate)

    max_rounds = max((a["rounds"] for a in agg.values()), default=0)
    n_slugs = len({r.get("slug") for r in ledger if r.get("slug")})
    note = None
    if n_slugs < 72:
        note = f"样本不足: 已结算 {n_slugs} 小时 (<72h 影子期), 池子置信度有限, 仅影子观察"
    elif not pool:
        note = "无钱包满足筛选条件"
    if pool:
        print(f"池子 {len(pool)} 个钱包 (条件: rounds>={args.min_rounds}, pnl>{args.min_pnl}, "
              f"winrate>={args.min_winrate}):")
        for p in pool[:10]:
            print(f"  {p['wallet'][:10]}... rounds={p['rounds']} 胜率={p['win_rate']:.0%} "
                  f"pnl={p['pnl_shares']:.0f}shares ${p['pnl_usd_est']:.0f} {p['name'][:20]}")
    else:
        print(f"池子为空 (统计钱包 {len(agg)} 个, 最高参与 {max_rounds} 小时, 已结算 {n_slugs} 小时)")
    if note:
        print(f"note: {note}")

    if args.dry_run:
        return 0
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window_days": args.window_days,
        "criteria": {"min_rounds": args.min_rounds, "min_pnl": args.min_pnl,
                     "min_winrate": args.min_winrate},
        "stats": {"settled_slugs": n_slugs, "wallets_seen": len(agg),
                  "max_rounds": max_rounds},
        "pool_size": len(pool),
        "pool": pool,
    }
    if note:
        out["note"] = note
    POOL_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已写入 {POOL_JSON} (pool_size={len(pool)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
