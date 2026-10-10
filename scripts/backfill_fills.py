#!/usr/bin/env python3
"""一次性回填 hermes_fills.jsonl（8/19）。

数据源:
1. hermes_bot_launchd.log 的两种终态事件（order_id 只有前 20 字符，需前缀匹配 done.jsonl 完整 id）:
   - "⏰ 订单超时取消 | {slug} {side} | {oid20} | {age}min" → filled=False, TIMEOUT_CANCELLED
   - "📋 订单结算 | {slug} {side} | {oid20} → FILLED"      → filled=True,  FILLED
2. done.jsonl 里 PAPER_ 前缀单 → filled=True, PAPER

规则:
- 同一 order_id 多条日志事件 → 以时间序最后一条为准（如先超时取消后查到 FILLED）
- 幂等：fills.jsonl 已有 order_id 跳过
- 当前 PENDING 文件里的活单不处理（bot 恢复追踪后自动结算）
"""

import json
import re
from pathlib import Path
from collections import OrderedDict

THIS_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = THIS_DIR / "data"
DONE = DATA_DIR / "hermes_done.jsonl"
LOG = DATA_DIR / "hermes_bot_launchd.log"
FILLS = DATA_DIR / "hermes_fills.jsonl"
PENDING = DATA_DIR / "hermes_pending.jsonl"

RE_TIMEOUT = re.compile(r"⏰ 订单超时取消 \| (\S+) (UP|DOWN) \| (\S+) \| \d+min")
RE_SETTLE = re.compile(r"📋 订单结算 \| (\S+) (UP|DOWN) \| (\S+) → (FILLED|CANCELLED)")


def main():
    # 1. done.jsonl: order_id → slug/side（前缀索引）
    done_orders = {}  # full_oid -> {"slug":..., "side":...}
    prefix_index = {}  # oid20 -> full_oid
    pending_oids = set()
    if PENDING.exists():
        for line in PENDING.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                pending_oids.add(json.loads(line).get("order_id", ""))
            except json.JSONDecodeError:
                pass
    if DONE.exists():
        for line in DONE.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            oid = e.get("result", {}).get("order_id", "")
            slug = e.get("signal", {}).get("slug", "")
            side = e.get("signal", {}).get("side") or e.get("signal", {}).get("signal", "")
            if oid:
                done_orders[oid] = {"slug": slug, "side": side}
                prefix_index[oid[:20]] = oid

    # 2. 已有 fills（幂等）
    existing = set()
    if FILLS.exists():
        for line in FILLS.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                existing.add(json.loads(line).get("order_id", ""))
            except json.JSONDecodeError:
                pass

    # 3. 时间序解析日志事件（同 order 取最后一条）
    events = OrderedDict()  # full_oid -> {"filled":..., "status":...}
    unmatched = 0
    if LOG.exists():
        for line in LOG.read_text(errors="replace").splitlines():
            m = RE_TIMEOUT.search(line)
            if m:
                slug, side, oid20 = m.group(1), m.group(2), m.group(3)
                full = prefix_index.get(oid20)
                if full:
                    events[full] = {"filled": False, "status": "TIMEOUT_CANCELLED",
                                    "slug": slug, "side": side}
                else:
                    unmatched += 1
                continue
            m = RE_SETTLE.search(line)
            if m:
                slug, side, oid20, st = m.groups()
                full = prefix_index.get(oid20)
                if full:
                    events[full] = {"filled": st == "FILLED", "status": st,
                                    "slug": slug, "side": side}
                else:
                    unmatched += 1

    # 4. PAPER 单直接 filled=True
    paper_events = {}
    for oid, info in done_orders.items():
        if oid.startswith("PAPER_"):
            paper_events[oid] = {"filled": True, "status": "PAPER",
                                 "slug": info["slug"], "side": info["side"]}

    # 5. 合并写入（日志事件优先于 PAPER 推断——PAPER 单不会有日志事件，互斥）
    to_write = []
    for oid, ev in events.items():
        if oid in existing or oid in pending_oids:
            continue
        to_write.append((oid, ev))
    for oid, ev in paper_events.items():
        if oid in existing or oid in pending_oids:
            continue
        to_write.append((oid, ev))

    n_filled = sum(1 for _, ev in to_write if ev["filled"] is True)
    n_unfilled = sum(1 for _, ev in to_write if ev["filled"] is False)
    n_paper = sum(1 for _, ev in to_write if ev["status"] == "PAPER")

    if to_write:
        with open(FILLS, "a") as f:
            for oid, ev in to_write:
                f.write(json.dumps({"order_id": oid, "slug": ev["slug"], "side": ev["side"],
                                    "filled": ev["filled"], "status": ev["status"],
                                    "ts": "backfill"}) + "\n")

    total_done = len(done_orders)
    known = len(existing) + len(to_write)
    print(f"done.jsonl 总订单: {total_done}")
    print(f"本次回填: {len(to_write)} 条（成交 {n_filled} / 未成交 {n_unfilled} / PAPER {n_paper}）")
    print(f"日志事件未匹配 order_id: {unmatched} 条")
    print(f"成交状态已知: {known}/{total_done}，未知(显示❔): {total_done - known}")
    # 抽查 8/18 7pm
    for oid, info in done_orders.items():
        if "august-18-2026-7pm" in info["slug"] and not oid.startswith("PAPER_"):
            ev = events.get(oid)
            print(f"抽查 7pm-et: {oid[:20]} → {ev['status'] if ev else '无日志事件'}")


if __name__ == "__main__":
    main()
