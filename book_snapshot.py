#!/usr/bin/env python3
"""Polymarket CLOB 盘口快照采集（盘感旁路的数据底座，不动交易链路）。

每 2 分钟由计划任务 HermesBTC1HBookSnap 触发一次，把当前 ET 小时市场
（bitcoin-up-or-down-...-HHam/pm-et）UP/DOWN 合约的 CLOB 盘口 top3 档位
追加写入 data/book_snapshots.jsonl，供 predict_hourly.py 旁路盘感信号计算。

字段：
    ts_utc          采集时间（UTC ISO）
    et_min          ET "MM-DD HH:MM"（显示用）
    anchor_ms       小时锚点 epoch ms
    minutes_in_hour 决策分钟（0..59）
    slug            市场 slug
    up/down         {"bids": [[price,size],...], "asks": [[price,size],...]} top3 原始档位

幂等：同 slug + 同分钟（minutes_in_hour）已存在则跳过，重复触发不写双份。
失败：静默记录到 data/book_snapshot.log，不阻塞（网络抖动常见）。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from hermes_common import http_get_json  # noqa: E402

DATA_DIR = ROOT / "data"
SNAP = DATA_DIR / "book_snapshots.jsonl"
LOG = DATA_DIR / "book_snapshot.log"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
ET = timezone(timedelta(hours=-4))
HOUR_MS = 3600_000


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).astimezone(ET).strftime('%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def build_slug(anchor_ms: int) -> str:
    """与 bot get_current_window() 同格式的 PM slug。"""
    dt = datetime.fromtimestamp(anchor_ms / 1000, tz=timezone.utc).astimezone(ET)
    hour12 = dt.hour % 12 or 12
    return (f"bitcoin-up-or-down-{dt.strftime('%B').lower()}-{dt.day}-"
            f"{dt.year}-{hour12}{'am' if dt.hour < 12 else 'pm'}-et")


def fetch_book(slug: str) -> dict | None:
    """拉取 UP/DOWN 各 top3 档盘口，失败返回 None。"""
    data = http_get_json(f"{GAMMA_API}/markets?slug={slug}", timeout=10) or []
    if not data:
        return None
    m = data[0]
    tids = json.loads(m.get("clobTokenIds", "[]"))
    if len(tids) < 2:
        return None
    try:
        outcomes = json.loads(m.get("outcomes", "[]"))
    except json.JSONDecodeError:
        outcomes = []
    i_up = outcomes.index("Up") if "Up" in outcomes else 0
    books = [http_get_json(f"{CLOB_API}/book?token_id={t}", timeout=10) for t in tids]

    def top(book: dict | None, key: str, n: int = 3):
        if not book:
            return []
        rows = book.get(key, [])
        rows = sorted(rows, key=lambda x: float(x["price"]), reverse=(key == "bids"))
        return [[float(x["price"]), float(x["size"])] for x in rows[:n]]

    up = {"bids": top(books[i_up], "bids"), "asks": top(books[i_up], "asks")}
    down = {"bids": top(books[1 - i_up], "bids"), "asks": top(books[1 - i_up], "asks")}
    if not up["bids"] and not up["asks"] and not down["bids"] and not down["asks"]:
        return None
    return {"up": up, "down": down}


def _already_exists(slug: str, minutes_in_hour: int) -> bool:
    """幂等：同 slug + 同决策分钟已有快照则不重复写。"""
    if not SNAP.exists():
        return False
    for line in SNAP.read_text(encoding="utf-8-sig").splitlines():
        try:
            old = json.loads(line)
            if old.get("slug") == slug and old.get("minutes_in_hour") == minutes_in_hour:
                return True
        except json.JSONDecodeError:
            continue
    return False


def main() -> int:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    anchor_ms = now_ms // HOUR_MS * HOUR_MS
    slug = build_slug(anchor_ms)
    minutes_in_hour = (now_ms - anchor_ms) // 60_000

    if _already_exists(slug, minutes_in_hour):
        log(f"= {slug} | min={minutes_in_hour} 已存在，跳过")
        return 0

    book = fetch_book(slug)
    if book is None:
        log(f"[X] {slug} | min={minutes_in_hour} 盘口拉取失败")
        return 1

    rec = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "et_min": datetime.fromtimestamp(now_ms / 1000, tz=ET).strftime("%m-%d %H:%M"),
        "anchor_ms": anchor_ms,
        "minutes_in_hour": minutes_in_hour,
        "slug": slug,
        "up": book["up"],
        "down": book["down"],
    }
    with open(SNAP, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    ua = book["up"]["asks"] or [[0, 0]]
    db = book["down"]["bids"] or [[0, 0]]
    log(f"[OK] {slug} | min={minutes_in_hour} | UP ask1={ua[0][0]} DOWN bid1={db[0][0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())