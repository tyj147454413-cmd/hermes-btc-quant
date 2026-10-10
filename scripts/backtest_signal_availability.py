# -*- coding: utf-8 -*-
"""信号可用率回测: 按分析时刻快照里 book_imbalance / smart_money 的齐全度分组,
看各组 胜率 + EV + 累计盈亏。仅用下单时刻可知信号。"""
import json, glob, os

def fval(v, d=None):
    if v is None: return d
    if isinstance(v, dict):
        for k in ("smart_money_pool_direction", "net_up_imbalance"):
            if k in v: return fval(v[k])
        return d
    try: return float(v)
    except: return d

# 1) 每场最后一份快照(=分析时刻)
snap = {}
for p in glob.glob(r"data\snapshots\bitcoin-*.json"):
    slug = os.path.basename(p).split("_")[0]
    try: o = json.load(open(p, encoding="utf-8"))
    except: continue
    t = o.get("collect_time_utc", "")
    if slug not in snap or t > snap[slug][0]:
        snap[slug] = (t, o)

# 2) 结果(带 pnl)
results = {}
for line in open(r"data\hermes_results.jsonl", encoding="utf-8"):
    line = line.strip()
    if not line: continue
    rr = json.loads(line)
    results.setdefault(rr["slug"], {}).update({
        "actual": rr.get("actual"), "side": rr.get("side"),
        "pnl": rr.get("pnl"), "buy_price": rr.get("buy_price")})

rows = []
for slug, (t, o) in snap.items():
    r = results.get(slug)
    if not r or not r["side"] or not r["actual"]: continue
    pm = o.get("polymarket") or {}
    ib = pm.get("book_imbalance")
    smd = fval((pm.get("smart_money_consensus_1h") or {}).get("smart_money_pool_direction"))
    ib_ok = ib is not None
    sm_ok = smd is not None
    rows.append({"slug": slug,
                 "side": r["side"], "actual": r["actual"],
                 "pnl": float(r["pnl"]) if r["pnl"] is not None else 0,
                 "bp": float(r["buy_price"]) if r["buy_price"] is not None else None,
                 "ib_ok": ib_ok, "sm_ok": sm_ok,
                 "ib": ib, "sm": smd})

# 3) 分组: 齐全度
groups = {
    "两组齐全(book+sm)": lambda r: r["ib_ok"] and r["sm_ok"],
    "仅book_imb可用":    lambda r: r["ib_ok"],
    "仅smart_money可用": lambda r: r["sm_ok"],
    "任一组可用(book|sm)": lambda r: r["ib_ok"] or r["sm_ok"],
    "两组都缺":          lambda r: not r["ib_ok"] and not r["sm_ok"],
    "全部样本(基线)":    lambda r: True,
}
print(f"可判样本: {len(rows)}")
print(f"\n== 信号可用率回测 ==")
print(f"{'分组':<26}{'n':>5}{'胜率':>8}{'Σpnl':>9}{'均pnl':>8}{'正EV?':>7}")
for name, cond in groups.items():
    g = [r for r in rows if cond(r)]
    if not g: 
        print(f"{name:<26}{0:>5}"); continue
    win = sum(1 for r in g if r["side"] == r["actual"])
    pnl = sum(r["pnl"] for r in g)
    avg = pnl / len(g)
    print(f"{name:<26}{len(g):>5}{win/len(g)*100:>7.1f}%{pnl:>+9.2f}{avg:>+8.2f}{'是' if avg>0 else '否':>6}")

# 4) 重点: 组齐全 vs 不齐全的错单率
print("\n== 错单率 ==")
for name, cond in groups.items():
    g = [r for r in rows if cond(r)]
    if not g: continue
    w = sum(1 for r in g if r["side"] != r["actual"])
    print(f"  {name:<24}: 错单 {w}/{len(g)} = {w/len(g)*100:.1f}%")