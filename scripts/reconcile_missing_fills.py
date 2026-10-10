#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性对账: 用链上 trades API 核实缺 fills 终态的订单 (2026-09-01)。

背景: 8/23-8/27 有 4 笔 LIVE 单因 bot 宕机/紧急撤单 except:pass 丢终态（fills 无记录），
review 按 filled=None 伪造 pnl，UI 成交/未成交显示错乱。更早的历史单（7月底-8月中）
是 8/19 fills 功能上线前+日志轮转无法回填的欠账。

逻辑:
  1. SecureClient 连接（复用 bot 的凭据缓存/代理逻辑）
  2. 分页拉取账户全部成交，建 order_id → (size, avg_price) 索引 + 最早成交时间（覆盖下界）
  3. done.jsonl 里无 fills 记录的 LIVE 单:
     - 出现在成交明细 → 补 fills filled=True (RECONCILED_FILLED)
     - 未出现且成交明细覆盖下界早于下单时间 → 补 fills filled=False (RECONCILED_CANCELLED)
     - 覆盖不到 → 保持未知（不动）
  4. 同步修正 results 里对应 slug 的 filled/fill_status/pnl（pnl=0 或按真实均价重算）

用法:
  python scripts/reconcile_missing_fills.py            # 只读，打印报告
  python scripts/reconcile_missing_fills.py --apply    # 写回 fills/results
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(THIS_DIR))

from hermes_common import get_proxy, load_pm_creds  # noqa: E402

DATA = THIS_DIR / "data"
DONE = DATA / "hermes_done.jsonl"
FILLS = DATA / "hermes_fills.jsonl"
RESULTS = DATA / "hermes_results.jsonl"
ENV = THIS_DIR / ".env"


def connect():
    from dotenv import load_dotenv
    load_dotenv(ENV)
    from polymarket import SecureClient
    pk = os.getenv("POLYMARKET_PK")
    if not pk:
        raise RuntimeError("POLYMARKET_PK not set")
    os.environ.pop("http_proxy", None)
    os.environ.pop("https_proxy", None)
    proxy = get_proxy()
    if proxy:
        os.environ["http_proxy"] = proxy
        os.environ["https_proxy"] = proxy
    cached = load_pm_creds()
    if cached:
        from polymarket.models.clob import ApiKeyCreds
        return SecureClient.create(private_key=pk, credentials=ApiKeyCreds(**cached))
    return SecureClient.create(private_key=pk)


def collect_trades(client, own_oids=None):
    """返回 (oid -> {"size":..,"notional":..}, earliest_trade_ts(epoch))。

    own_oids: 本地账本（done/results/fills）里我们自己的 order_id 白名单。
    9/2 关键修复: 账户 trades 里我们是 maker 时，一笔 trade 含多个 maker（含对手方
    其他人的挂单），tr.size 是整笔成交量（如 103.45sh@0.21），我们自己的份额只在
    maker_orders[].matched_amount。必须按白名单精确认领，不能累加整笔量。
    """
    by_oid = {}
    earliest = None
    pages = 0
    paginator = client.list_account_trades()
    for page in paginator:
        pages += 1
        if pages > 100:
            print(f"[!] trades 超过 100 页截断（已覆盖至 {earliest}）")
            break
        for tr in getattr(page, "items", []) or []:
            try:
                _tst = str(getattr(tr, "status", "") or "").upper()
                if "FAIL" in _tst or "RETRY" in _tst:
                    continue
                p = float(tr.price)
                s = float(tr.size)
                if s <= 0 or p <= 0:
                    continue
                ts = getattr(tr, "matched_at", None) or getattr(tr, "updated_at", None)
                if ts is not None:
                    try:
                        if hasattr(ts, "timestamp"):
                            t = ts.timestamp()
                        else:
                            t = float(ts)
                            if t > 1e12:  # ms
                                t /= 1000.0
                        earliest = t if earliest is None else min(earliest, t)
                    except (TypeError, ValueError, OSError):
                        pass
                # 9/2 关键修复: 只认领白名单内我们自己订单的成交份额
                _taker = str(getattr(tr, "taker_order_id", "") or "")
                if own_oids is None or _taker in own_oids:
                    p = float(tr.price)
                    s = float(tr.size)
                    if _taker and s > 0 and p > 0:
                        rec = by_oid.setdefault(_taker, {"size": 0.0, "notional": 0.0})
                        rec["size"] += s
                        rec["notional"] += p * s
                for mo in (getattr(tr, "maker_orders", ()) or ()):
                    _moid = str(getattr(mo, "order_id", "") or "")
                    if _moid and (own_oids is None or _moid in own_oids):
                        p = float(mo.price)
                        s = float(getattr(mo, "matched_amount", 0) or 0)
                        if s > 0 and p > 0:
                            rec = by_oid.setdefault(_moid, {"size": 0.0, "notional": 0.0})
                            rec["size"] += s
                            rec["notional"] += p * s
            except (TypeError, ValueError, ArithmeticError):
                continue
        if not getattr(page, "has_more", False):
            break
    print(f"[OK] trades 共 {pages} 页, 最早成交时间: "
          f"{datetime.fromtimestamp(earliest, tz=timezone.utc).isoformat() if earliest else 'N/A'}")
    return by_oid, earliest


def reprice(client, trades):
    """--reprice: 用链上真实成交份额/均价重算已成交单的真实盈亏。

    背景: 历史成交单的 fills 记录大量缺 avg_price/size_matched（成交瞬间 trades API
    延迟、旧版本未带），review 只能按挂单价估算 pnl——如 9/1 5am-et 追价实际成交
    0.70 却按首挂 0.65 算出 +2.69（真实 +2.14）。本函数按 review 同款公式
    （完整成交 amount×(1/avg−1)/−amount；部分成交 size−cost/−cost）重算，回写
    fills（补 avg_price/size_matched）与 results（buy_price/pnl/avg_price）。
    """
    fills_rec = {}
    for line in open(FILLS, encoding="utf-8"):
        line = line.strip()
        if line:
            r = json.loads(line)
            if r.get("order_id"):
                fills_rec[r["order_id"]] = r
    results = {}
    for line in open(RESULTS, encoding="utf-8"):
        line = line.strip()
        if line:
            r = json.loads(line)
            if r.get("slug"):
                results[r["slug"]] = r

    fills_patch = {}   # order_id -> {"avg_price":.., "size_matched":..}
    res_patch = {}     # slug -> {"buy_price":.., "pnl":.., "avg_price":..}
    n_checked = n_fixed = 0
    for slug, rr in results.items():
        if rr.get("filled") is not True or rr.get("reconciled") == "20260901-pnl":
            continue
        oid = rr.get("order_id") or ""
        fr = fills_rec.get(oid) or {}
        if str(fr.get("order_id", "") or "").startswith("PAPER_"):
            continue
        tr = trades.get(oid)
        if not tr:
            # 追价链解析: 首挂在链上无成交（被撤），真实成交在追价子单 oid 下。
            # fills 的 CHASED_ 镜像记录带 repriced_to → 子单 id，用它的 trades 数据定价
            _sub = fr.get("repriced_to") or ""
            if _sub and fills_rec.get(oid, {}).get("filled") is True:
                tr = trades.get(_sub)
        if not tr or tr["size"] <= 0:
            continue
        n_checked += 1
        size = tr["size"]
        avg = tr["notional"] / size
        amount = float(rr.get("amount") or 0)
        side = rr.get("side") or ""
        actual = rr.get("actual") or ""
        win = (side == actual)
        # 部分成交判定: 已成交份额明显小于按记账金额应得的份额 → 用真实成本口径
        est_full = amount / avg if avg > 0 else 0
        partial = amount > 0 and est_full > 0 and size < est_full * 0.98
        if partial:
            cost = size * avg
            pnl = round(size * 1.0 - cost, 2) if win else round(-cost, 2)
        else:
            pnl = round(amount * (1.0 / avg - 1.0), 2) if win else round(-amount, 2)
        old_pnl = rr.get("pnl")
        old_buy = rr.get("buy_price")
        need = (old_pnl != pnl) or (abs((old_buy or 0) - avg) > 1e-6)
        if need:
            res_patch[slug] = {"buy_price": round(avg, 4), "pnl": pnl,
                               "avg_price": round(avg, 4), "reconciled": "20260901-pnl"}
            n_fixed += 1
            print(f"  [重算] {slug.replace('bitcoin-up-or-down-', '')} "
                  f"{size:.2f}sh@{avg:.4f} {'WIN' if win else 'LOSS'} "
                  f"pnl {old_pnl} → {pnl:+.2f} (buy_price {old_buy} → {avg:.4f})"
                  f"{' [部分成交]' if partial else ''}")
        if need or fr.get("avg_price") is None:
            _p = {"avg_price": round(avg, 4), "size_matched": round(size, 4)}
            fills_patch[oid] = _p  # 首挂（CHASED_ 镜像记录）
            if fr.get("repriced_to"):
                fills_patch[fr["repriced_to"]] = _p  # 追价子单真实成交记录
    print(f"\n[reprice] 核对 {n_checked} 笔成交单, 需重算 {n_fixed} 笔, "
          f"补 fills 均价 {len(fills_patch)} 条")
    return fills_patch, res_patch


def apply_patches(fills_patch, res_patch):
    """原位重写 fills/results（tmp+replace 原子替换）。fills 现有记录上补字段。"""
    if fills_patch:
        tmp = FILLS.with_suffix(".jsonl.tmp")
        with open(FILLS, encoding="utf-8") as fin, open(tmp, "w", encoding="utf-8") as fout:
            for line in fin:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                patch = fills_patch.get(r.get("order_id"))
                if patch:
                    r.update(patch)
                fout.write(json.dumps(r, ensure_ascii=False) + "\n")
        tmp.replace(FILLS)
    if res_patch:
        tmp = RESULTS.with_suffix(".jsonl.tmp")
        with open(RESULTS, encoding="utf-8") as fin, open(tmp, "w", encoding="utf-8") as fout:
            for line in fin:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                patch = res_patch.get(r.get("slug"))
                if patch:
                    r.update(patch)
                fout.write(json.dumps(r, ensure_ascii=False) + "\n")
        tmp.replace(RESULTS)
    print(f"[OK] 已写回: fills 修 {len(fills_patch)}, results 修 {len(res_patch)}")


def load_local_ledger():
    """读 done/fills/results，返回 (done, fills, results, own_oids 白名单)"""
    done = [json.loads(l) for l in open(DONE, encoding="utf-8") if l.strip()]
    fills = {}
    for line in open(FILLS, encoding="utf-8"):
        line = line.strip()
        if line:
            r = json.loads(line)
            if r.get("order_id"):
                fills[r["order_id"]] = r
    results = {}
    for line in open(RESULTS, encoding="utf-8"):
        line = line.strip()
        if line:
            r = json.loads(line)
            if r.get("slug"):
                results[r["slug"]] = r
    own = set(fills.keys())
    for d in done:
        oid = (d.get("result") or {}).get("order_id") or ""
        if oid:
            own.add(oid)
    for r in results.values():
        if r.get("order_id"):
            own.add(r["order_id"])
    return done, fills, results, own


def main():
    apply = "--apply" in sys.argv
    if "--reprice" in sys.argv:
        _done, _fills, _results, own = load_local_ledger()
        client = connect()
        trades, _earliest = collect_trades(client, own_oids=own)
        fills_patch, res_patch = reprice(client, trades)
        if apply and (fills_patch or res_patch):
            apply_patches(fills_patch, res_patch)
        elif not apply:
            print("（dry-run，加 --apply 写回）")
        return

    done, fills, results, own = load_local_ledger()

    missing = []  # (slug, oid, done_entry)
    for d in done:
        res = d.get("result") or {}
        oid = res.get("order_id") or ""
        sig = d.get("signal") or {}
        slug = d.get("slug") or sig.get("slug") or ""
        if not oid or oid.startswith("PAPER_"):
            continue
        if oid not in fills:
            missing.append((slug, oid, d))
    print(f"缺 fills 终态的 LIVE 单: {len(missing)}")

    client = connect()
    trades, earliest = collect_trades(client, own_oids=own)

    to_write = []   # fills 新记录
    res_patch = {}  # slug -> {"filled":.., "fill_status":.., "pnl":.., "buy_price":..}
    for slug, oid, d in missing:
        if oid in trades:
            rec = trades[oid]
            avg = rec["notional"] / rec["size"]
            to_write.append({"order_id": oid, "slug": slug,
                             "side": (d.get("signal") or {}).get("side") or "",
                             "filled": True, "status": "RECONCILED_FILLED",
                             "size_matched": rec["size"], "avg_price": round(avg, 6),
                             "ts": "reconcile-20260901"})
            # pnl 保留 review 原值（trades 的 size/avg 口径未与 stake 完全核验，仅修成交状态）
            res_patch[slug] = {"filled": True, "fill_status": "RECONCILED_FILLED"}
            print(f"  [FILLED] {slug.replace('bitcoin-up-or-down-', '')} "
                  f"{rec['size']:.2f}sh @ {avg:.4f}（pnl 保留 review 原值）")
        elif earliest is not None:
            # 覆盖判断: earliest（账户最早成交时间）早于该 slug 当日时段才认为明细覆盖到
            import re
            m = re.search(r"(\w+)-(\d+)-(\d+)(?:-|$)", slug or "")
            covered = False
            if m:
                months = {n: i for i, n in enumerate(
                    ["january", "february", "march", "april", "may", "june", "july",
                     "august", "september", "october", "november", "december"], 1)}
                mon = months.get(m.group(1))
                if mon:
                    try:
                        from datetime import datetime as _dt
                        # slug 当日 UTC 16:00（ET 正午）作为该单时段估计
                        t_est = _dt(int(m.group(3)), mon, int(m.group(2)), 16, 0,
                                    tzinfo=timezone.utc).timestamp()
                        covered = earliest <= t_est
                    except Exception:
                        covered = False
            if covered:
                # 终态守卫: 未到该市场结算时间+1h 缓冲的单可能仍在 TTL 窗口内，不能标未成交
                # （如 sept-1-7am 刚下单仍在窗口。slug 小时 ET → 结算 = (hour+1):00 ET = UTC-4）
                import re as _re
                _hm = _re.search(r"-(\d{1,2})(am|pm)-et", slug or "")
                settled = True
                if _hm:
                    from datetime import datetime as _dt2, timedelta as _td2, timezone as _tz2
                    _h = int(_hm.group(1)) % 12 + (12 if _hm.group(2) == "pm" else 0)
                    # 结算 = slug 当日 (hour+1):00 ET；EDT=UTC-4 → UTC = ET+4，用 timedelta 自动进位
                    _base = _dt(int(m.group(3)), mon, int(m.group(2)), _h, 0, tzinfo=_tz2.utc)
                    _settle = _base + _td2(hours=5)  # +1h 结算 +4h 时区
                    settled = time.time() >= _settle.timestamp() + 3600
                if not settled:
                    print(f"  [跳过] {slug.replace('bitcoin-up-or-down-', '')} 未到结算时间+1h，可能仍在 TTL 窗口")
                else:
                    to_write.append({"order_id": oid, "slug": slug,
                                     "side": (d.get("signal") or {}).get("side") or "",
                                     "filled": False, "status": "RECONCILED_CANCELLED",
                                     "ts": "reconcile-20260901"})
                    res_patch[slug] = {"filled": False, "fill_status": "RECONCILED_CANCELLED",
                                       "pnl": 0.0, "buy_price": results.get(slug, {}).get("buy_price")}
                    print(f"  [未成交] {slug.replace('bitcoin-up-or-down-', '')} (trades 明细已覆盖该时段)")
            else:
                print(f"  [跳过] {slug.replace('bitcoin-up-or-down-', '')} trades 明细覆盖不到该时段，保持未知")
        else:
            print(f"  [跳过] {slug.replace('bitcoin-up-or-down-', '')} 无 trades 数据")

    print(f"\n计划: 补 fills {len(to_write)} 条, 修 results {len(res_patch)} 条")
    if not apply:
        print("（dry-run，加 --apply 写回）")
        return

    # 写 fills（append）
    with open(FILLS, "a", encoding="utf-8") as f:
        for rec in to_write:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    # 修 results（原位更新对应 slug 的字段，重写文件）
    tmp = RESULTS.with_suffix(".jsonl.tmp")
    with open(RESULTS, encoding="utf-8") as fin, open(tmp, "w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            patch = res_patch.get(r.get("slug"))
            if patch:
                r.update(patch)
                r["reconciled"] = "20260901"
            fout.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(RESULTS)
    print(f"[OK] 已写回: fills +{len(to_write)}, results 修正 {len(res_patch)}")


if __name__ == "__main__":
    main()
