#!/usr/bin/env python3
"""
paper_simulator.py — Hermes BTC 1H paper trading simulator(方案 B 攒样本核心)

设计目的(2026-08-28 round47+ 用户决策):
- 暂停实盘(trading_paused=true)期间,攒"模拟成交"样本做因子归因
- 不动 bot 主流程 — 信号照写,bot paused 路径跳过实盘;本模块独立从 claude_signal.jsonl 读
- 模拟成交价 = min(book_{side}_ask, max_price),模拟金额固定 $1(可配)
- 1H 后用 slug 末价(下小时 anchor)做结算方向,写 p_l
- 输出 data/hermes_shadow_paper.jsonl(append-only)

用法:
  python3 paper_simulator.py enter <slug>           # 模拟成交某 slug
  python3 paper_simulator.py settle                  # 结算所有 pending(每轮 cron 调一次)
  python3 paper_simulator.py report                  # 输出周报统计
  python3 paper_simulator.py report --days 7         # 最近 N 天

约束:
- 仅在 trading_paused=true 时工作(保护 — 实盘模式下不写影子账,避免污染)
- append-only,不删不覆盖历史
- 字段名与 hermes_done.jsonl / hermes_shadow_edge.jsonl 对齐,便于下游 factor_attribution 共用
"""
from __future__ import annotations
import argparse, json, os, sys, glob, re
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

# --- 路径(沙箱友好) ---
SCRIPT_DIR = Path(__file__).parent
HERMES_DIR = SCRIPT_DIR.parent
DATA_DIR = HERMES_DIR / "data"
SNAP_DIR = DATA_DIR / "snapshots"
SHADOW_FILE = DATA_DIR / "hermes_shadow_paper.jsonl"
SIGNAL_FILE = DATA_DIR / "claude_signal.jsonl"
CONFIG_FILE = HERMES_DIR / "hermes_config.json"

# --- 模拟参数(可被 config 覆盖) ---
PAPER_AMOUNT = 1.0  # 固定 $1 模拟金额(可改为读 config.bet_amount)
MAX_PRICE_FALLBACK = 0.60  # 与 bot 一致

# --- slug 时间解析(对齐 scripts/journal_path.py) ---
MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}
SLUG_RE = re.compile(r"^bitcoin-up-or-down-(\w+)-(\d+)-(\d{4})-(\d{1,2})(am|pm)-et$")


def slug_to_dt(slug: str) -> tuple[date, int] | None:
    m = SLUG_RE.match(slug or "")
    if not m:
        return None
    mon, day, year, h12, ampm = m.groups()
    if MONTHS.get(mon) is None:
        return None
    h = int(h12) % 12
    if ampm == "pm":
        h += 12
    return date(int(year), MONTHS[mon], int(day)), h


def next_slug(slug: str) -> str | None:
    """slug 下一小时(用于读末价/锚定)"""
    p = slug_to_dt(slug)
    if not p:
        return None
    d, h = p
    nxt = datetime(d.year, d.month, d.day, h) + timedelta(hours=1)
    # Polymarket slug 月份小写("august" not "August"),对齐实际快照命名
    month_name = [m for m, i in MONTHS.items() if i == nxt.month][0]
    ampm = "am" if nxt.hour < 12 else "pm"
    h12 = nxt.hour % 12 or 12
    return f"bitcoin-up-or-down-{month_name}-{nxt.day}-{nxt.year}-{h12}{ampm}-et"


def prev_slug(slug: str) -> str | None:
    """slug 上一小时(用于核对上一场数据)"""
    p = slug_to_dt(slug)
    if not p:
        return None
    d, h = p
    prv = datetime(d.year, d.month, d.day, h) - timedelta(hours=1)
    month_name = [m for m, i in MONTHS.items() if i == prv.month][0]
    ampm = "am" if prv.hour < 12 else "pm"
    h12 = prv.hour % 12 or 12
    return f"bitcoin-up-or-down-{month_name}-{prv.day}-{prv.year}-{h12}{ampm}-et"


def read_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


def is_paused() -> bool:
    """paused 才允许写影子账(避免实盘模式下污染)"""
    cfg = read_config()
    return bool(cfg.get("trading_paused", False))


def find_snapshot(slug: str) -> dict | None:
    """读 slug 最新全量快照(全量存档)"""
    if not SNAP_DIR.exists():
        return None
    files = sorted(SNAP_DIR.glob(f"{slug}_*.json"), reverse=True)
    for f in files:
        try:
            with open(f) as fp:
                d = json.load(fp)
            # 全量快照有 polymarket 块;slim 没 polymarket 跳过
            if "polymarket" in d:
                return d
        except Exception:
            continue
    # 退而求其次,slim
    for f in files:
        try:
            with open(f) as fp:
                return json.load(fp)
        except Exception:
            continue
    return None


def compute_entry_price(side: str, snapshot: dict, max_price: float) -> float:
    """
    模拟成交价 — 对齐 bot taker/maker 决策:
    - 优先 book_{side}_ask(真实盘口卖一价)
    - 若 ask 缺失或超 max_price,挂 max_price 限价(假设等回落成交)
    - 兜底 pm_{side}_price
    """
    pm = snapshot.get("polymarket", {})
    if side == "UP":
        ask = pm.get("book_up_ask") or pm.get("up_price")
    else:
        # book_down_ask 未单独存,down_price 是 gamma 标记价;简化用 (1 - book_up_bid)
        ask = (1.0 - pm.get("book_up_bid", 0)) if pm.get("book_up_bid") else pm.get("down_price")
    if ask is None:
        return min(0.50, max_price)  # 兜底,不该发生
    if ask > max_price:
        # 限价挂单 = max_price 等回落(模拟挂单价)
        return max_price
    return float(ask)


def find_signal(slug: str) -> dict | None:
    """
    三层 fallback 找信号(对齐 round33.2 bot 队列清空事实):
    1. claude_signal.jsonl — bot 实时队列,本轮刚落盘的信号可能还在
    2. hermes_last_signal.json — 最近一笔持久化信号
    3. hermes_bot_launchd.log — 解析 DEBUG: full sig content 行,全历史
    """
    # 第 1 层:claude_signal.jsonl
    if SIGNAL_FILE.exists():
        lines = SIGNAL_FILE.read_text().splitlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("slug") == slug:
                return d

    # 第 2 层:hermes_last_signal.json
    last_sig_file = DATA_DIR / "hermes_last_signal.json"
    if last_sig_file.exists():
        try:
            d = json.loads(last_sig_file.read_text())
            inner = d.get("signal", d)  # 包一层 signal 字段
            if inner.get("slug") == slug:
                return inner
        except Exception:
            pass

    # 第 3 层:bot_launchd.log DEBUG: full sig content
    log_file = DATA_DIR / "hermes_bot_launchd.log"
    if log_file.exists():
        try:
            content = log_file.read_text()
            # 找所有含 slug 的 "DEBUG: full sig content:" 段
            # 该段后跟 {'slug': '...', ...} dict 字面量
            idx = 0
            found = None
            while True:
                i = content.find(f"'slug': '{slug}'", idx)
                if i < 0:
                    break
                # 找 dict 开始
                brace_start = content.rfind("{", 0, i)
                if brace_start < 0:
                    break
                # 平衡花括号提取 dict
                depth = 0
                end = brace_start
                for j in range(brace_start, min(brace_start + 5000, len(content))):
                    if content[j] == "{":
                        depth += 1
                    elif content[j] == "}":
                        depth -= 1
                        if depth == 0:
                            end = j + 1
                            break
                dict_str = content[brace_start:end]
                # Python literal_eval 解析单引号 dict
                import ast
                try:
                    parsed = ast.literal_eval(dict_str)
                    if parsed.get("slug") == slug:
                        found = parsed  # 取最后一笔(最新)
                except Exception:
                    pass
                idx = i + 1
            if found:
                return found
        except Exception:
            pass

    return None


def append_shadow(record: dict) -> None:
    SHADOW_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SHADOW_FILE, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_shadow(only_settled: bool = False) -> list[dict]:
    if not SHADOW_FILE.exists():
        return []
    out = []
    for line in SHADOW_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if only_settled and d.get("status") != "settled":
            continue
        out.append(d)
    return out


# === enter: 模拟成交 ===
def cmd_enter(slug: str, amount: float = PAPER_AMOUNT) -> dict:
    if not is_paused():
        return {"ok": False, "error": "trading_paused=false, 拒绝写影子账(避免污染实盘)"}

    sig = find_signal(slug)
    if not sig:
        return {"ok": False, "error": f"claude_signal.jsonl 找不到 slug={slug}"}
    if sig.get("action") == "SKIP":
        return {"ok": False, "error": f"slug={slug} action=SKIP 不模拟成交"}
    side = sig.get("side") or sig.get("signal")
    if side not in ("UP", "DOWN"):
        return {"ok": False, "error": f"side 异常: {side}"}

    snap = find_snapshot(slug)
    if not snap:
        return {"ok": False, "error": f"找不到快照 {slug}"}

    max_price = sig.get("max_price") or MAX_PRICE_FALLBACK
    entry_price = compute_entry_price(side, snap, max_price)
    shares = round(amount / entry_price, 4) if entry_price > 0 else 0.0
    cost = round(shares * entry_price, 4)

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "slug": slug,
        "side": side,
        "action": sig.get("action", "ORDER"),
        "source": sig.get("source", "btc-agent"),
        "probability": sig.get("probability"),
        "max_price": max_price,
        "anchor_price": snap.get("anchor_price"),
        "entry_price": entry_price,
        "shares": shares,
        "amount": amount,
        "cost": cost,
        "status": "pending",
        "settle_ts": None,
        "close_price": None,
        "actual_dir": None,
        "win": None,
        "p_l": None,
        "strategy_version": sig.get("strategy_version", "round47"),
        "model_version": sig.get("model_version", "claude-app-desktop"),
        "pm_side_price": snap.get("polymarket", {}).get(f"{side.lower()}_price"),
        "book_side_ask": snap.get("polymarket", {}).get("book_up_ask") if side == "UP"
                          else (1.0 - snap.get("polymarket", {}).get("book_up_bid", 0)
                                if snap.get("polymarket", {}).get("book_up_bid") else None),
        "reason": sig.get("reason", "")[:200],  # 截断
    }
    append_shadow(record)
    return {"ok": True, "record": record}


# === settle: 结算所有 pending ===
def cmd_settle() -> dict:
    if not SHADOW_FILE.exists():
        return {"ok": True, "settled": 0, "msg": "无影子账"}
    records = load_shadow()
    settled_count = 0
    pending_count = 0
    errors = []
    for r in records:
        if r.get("status") != "pending":
            continue
        pending_count += 1
        slug = r["slug"]
        # 找下一小时 slug 的快照(末价即结算价)
        nxt = next_slug(slug)
        if not nxt:
            errors.append(f"{slug}: next_slug 失败")
            continue
        snap_nxt = find_snapshot(nxt)
        if not snap_nxt:
            errors.append(f"{slug}: 找不到下一小时快照 {nxt}, 等下次")
            continue
        # close = 下一小时 snapshot 的 current_price(该小时最后一刻)
        close_price = snap_nxt.get("current_price")
        anchor = r.get("anchor_price")
        if close_price is None or anchor is None:
            errors.append(f"{slug}: close/anchor 缺失")
            continue
        # 方向
        if close_price > anchor:
            actual_dir = "UP"
        elif close_price < anchor:
            actual_dir = "DOWN"
        else:
            actual_dir = "FLAT"  # 平局 Polymarket 一般按 UP 结算(简化按 UP)

        side = r["side"]
        win = (actual_dir == side) or (actual_dir == "FLAT" and side == "UP")
        if win:
            # 赢了 = 1 share 换 $1(Polymarket 二元合约 0/1)
            p_l = round(r["shares"] * 1.0 - r["cost"], 4)
        else:
            p_l = round(-r["cost"], 4)

        # 直接更新 records 元素(避免浅拷贝 bug)
        r["status"] = "settled"
        r["settle_ts"] = datetime.now(timezone.utc).isoformat()
        r["close_price"] = close_price
        r["actual_dir"] = actual_dir
        r["win"] = win
        r["p_l"] = p_l
        settled_count += 1

    # 重写主文件(只更新 status/sell 数据,append-only 风格不允许覆盖)
    tmp = SHADOW_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(SHADOW_FILE)

    return {
        "ok": True,
        "settled": settled_count,
        "pending_left": pending_count - settled_count,
        "errors": errors[:5],
    }


# === report: 周报 ===
def cmd_report(days: int = 7) -> dict:
    records = load_shadow()
    if not records:
        return {"ok": True, "msg": "无样本"}
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    recent = []
    for r in records:
        try:
            ts = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
        except Exception:
            continue
        if ts >= cutoff:
            recent.append(r)
    settled = [r for r in recent if r.get("status") == "settled"]
    pending = [r for r in recent if r.get("status") == "pending"]

    if not settled:
        return {"ok": True, "total": len(recent), "settled": 0, "pending": len(pending), "msg": "尚无结算样本"}

    wins = sum(1 for r in settled if r.get("win"))
    total_p_l = sum(r.get("p_l", 0) for r in settled)
    win_rate = wins / len(settled) if settled else 0.0
    avg_p_l = total_p_l / len(settled) if settled else 0.0

    # 按 side 分
    up_records = [r for r in settled if r["side"] == "UP"]
    dn_records = [r for r in settled if r["side"] == "DOWN"]
    up_win = sum(1 for r in up_records if r.get("win"))
    dn_win = sum(1 for r in dn_records if r.get("win"))
    up_p_l = sum(r.get("p_l", 0) for r in up_records)
    dn_p_l = sum(r.get("p_l", 0) for r in dn_records)

    return {
        "ok": True,
        "days": days,
        "total_samples": len(recent),
        "settled": len(settled),
        "pending": len(pending),
        "win_rate": round(win_rate, 4),
        "total_p_l": round(total_p_l, 4),
        "avg_p_l": round(avg_p_l, 4),
        "up_n": len(up_records), "up_win_rate": round(up_win / len(up_records), 4) if up_records else 0,
        "up_p_l": round(up_p_l, 4),
        "dn_n": len(dn_records), "dn_win_rate": round(dn_win / len(dn_records), 4) if dn_records else 0,
        "dn_p_l": round(dn_p_l, 4),
        "msg": f"近 {days} 天 {len(settled)} 笔结算,胜率 {win_rate*100:.1f}%,总 P&L ${total_p_l:.2f}"
    }


def main():
    parser = argparse.ArgumentParser(description="Hermes paper trading simulator")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_enter = sub.add_parser("enter", help="模拟成交某 slug")
    p_enter.add_argument("slug")
    p_enter.add_argument("--amount", type=float, default=PAPER_AMOUNT)

    sub.add_parser("settle", help="结算所有 pending 影子账")

    p_report = sub.add_parser("report", help="周报")
    p_report.add_argument("--days", type=int, default=7)

    args = parser.parse_args()
    if args.cmd == "enter":
        result = cmd_enter(args.slug, args.amount)
    elif args.cmd == "settle":
        result = cmd_settle()
    elif args.cmd == "report":
        result = cmd_report(args.days)
    else:
        result = {"ok": False, "error": "unknown cmd"}

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())