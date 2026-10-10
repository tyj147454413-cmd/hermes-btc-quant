#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round50 v3 聪明钱账本结算工具 (2026-08-28 用户决策「滚动盈利钱包池」第 2 步)

职责: 读采集器落盘的钱包持仓明细 (data/hermes_smart_money_wallets.jsonl),
对每个已结算 slug 按结算结果给每个钱包算盈亏, 追加写账本
(data/hermes_smart_money_ledger.jsonl), 供 smart_money_pool.py 筛池子。

口径 (诚实披露):
- pnl_shares: 持赢侧 +amount / 持输侧 -amount (shares 简单口径, 不含成本)
- pnl_usd_est: 按采集时刻快照价估算成本 -- 赢侧 amount×(1-cost), 输侧 -amount×cost
  cost = 该钱包持仓侧的快照标记价(up_price/down_price)。钱包实际买入价未知, 此为近似。
- 结算判定: gamma API outcomePrices 结算后 ["1","0"]=UP 赢 / ["0","1"]=DOWN 赢;
  closed=False 的 slug 跳过 (未结算)。
- 幂等: 账本已有的 slug 不重复结算; 采集器同 slug 重复落盘按 (slug, wallet) 去重取最新。

用法:
  python3 scripts/smart_money_settle.py            # 结算所有未处理 slug
  python3 scripts/smart_money_settle.py --dry-run  # 只看会结算哪些, 不写账本
"""
import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = THIS_DIR / "data"
WALLETS_JL = DATA_DIR / "hermes_smart_money_wallets.jsonl"
LEDGER_JL = DATA_DIR / "hermes_smart_money_ledger.jsonl"

GAMMA = "https://gamma-api.polymarket.com/markets?slug={slug}"
GAMMA_CLOSED = "https://gamma-api.polymarket.com/markets?slug={slug}&closed=true"


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


def dedupe_wallet_records(records: list) -> list:
    """(slug, wallet) 去重, 保留 ts 最新的一条 (采集器重复落盘容错)。"""
    best = {}
    for r in records:
        key = (r.get("slug"), r.get("wallet"))
        if key[0] is None or key[1] is None:
            continue
        prev = best.get(key)
        if prev is None or str(r.get("ts", "")) >= str(prev.get("ts", "")):
            best[key] = r
    return list(best.values())


def fetch_outcome(slug: str, fetch=None) -> str | None:
    """查 gamma 结算结果: 返回 'UP' / 'DOWN' / None(未结算或查询失败)。

    fetch 参数供测试注入 (默认真实 urllib)。
    注意: gamma 对已关闭市场必须带 closed=true 才返回(实测 8/28: 已结算 slug
    不带参数查询返回空列表), 故两查: 先 closed=true 再默认(活跃市场)。
    """
    if fetch is None:
        def fetch(url):
            req = urllib.request.Request(url, headers={"User-Agent": "hermes-settle/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())
    try:
        markets = None
        for url in (GAMMA_CLOSED.format(slug=slug), GAMMA.format(slug=slug)):
            markets = fetch(url)
            if isinstance(markets, list) and markets:
                break
        if not isinstance(markets, list) or not markets:
            return None
        m = markets[0]
        if not m.get("closed"):
            return None
        prices_raw = m.get("outcomePrices", "[]")
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        if len(prices) < 2:
            return None
        up_p = float(prices[0])
        down_p = float(prices[1])
        if up_p > 0.99 and down_p < 0.01:
            return "UP"
        if down_p > 0.99 and up_p < 0.01:
            return "DOWN"
        return None
    except Exception:
        return None


def settle_records(deduped: list, outcome_map: dict) -> list:
    """纯函数: 给定去重后的持仓明细与结算结果, 产出账本行。

    outcome_map: {slug: 'UP'|'DOWN'}  (只含已结算 slug)
    返回: [{slug, et_hour, wallet, name, side, amount, outcome,
            pnl_shares, pnl_usd_est, settled_at}]
    """
    rows = []
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for r in deduped:
        slug = r.get("slug")
        outcome = outcome_map.get(slug)
        if outcome is None:
            continue
        side = r.get("side")
        amount = float(r.get("amount", 0) or 0)
        if side not in ("UP", "DOWN") or amount <= 0:
            continue
        won = (side == outcome)
        # 成本近似: 该侧快照标记价
        cost = float(r.get("up_price" if side == "UP" else "down_price", 0) or 0)
        pnl_shares = amount if won else -amount
        pnl_usd_est = amount * (1 - cost) if won else -amount * cost
        rows.append({
            "slug": slug,
            "et_hour": r.get("et_hour"),
            "wallet": r.get("wallet"),
            "name": r.get("name") or "",
            "side": side,
            "amount": round(amount, 2),
            "outcome": outcome,
            "win": won,
            "pnl_shares": round(pnl_shares, 2),
            "pnl_usd_est": round(pnl_usd_est, 2),
            "cost_price_est": cost,
            "settled_at": now_iso,
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只列出将结算的 slug, 不写账本")
    args = ap.parse_args()

    records = load_jsonl(WALLETS_JL)
    if not records:
        print("无钱包持仓明细 (hermes_smart_money_wallets.jsonl 为空或不存在)")
        return 0
    deduped = dedupe_wallet_records(records)
    ledger = load_jsonl(LEDGER_JL)
    done_slugs = {r.get("slug") for r in ledger if r.get("slug")}

    # 待结算 slug = 明细里出现且账本未处理的
    all_slugs = sorted({r.get("slug") for r in deduped if r.get("slug")})
    pending = [s for s in all_slugs if s not in done_slugs]
    print(f"明细记录 {len(records)} 行 -> 去重 {len(deduped)} 行, slug {len(all_slugs)} 个, "
          f"账本已结算 {len(done_slugs)} 个, 待查 {len(pending)} 个")

    outcome_map = {}
    unresolved = []
    for s in pending:
        oc = fetch_outcome(s)
        if oc is None:
            unresolved.append(s)
        else:
            outcome_map[s] = oc
        print(f"  {s}: {oc if oc else '未结算/查询失败'}")

    rows = settle_records(deduped, outcome_map)
    print(f"可结算账本行: {len(rows)} (未结算跳过 {len(unresolved)} 个 slug)")
    if args.dry_run or not rows:
        return 0

    with LEDGER_JL.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"已追加 {len(rows)} 行 -> {LEDGER_JL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
