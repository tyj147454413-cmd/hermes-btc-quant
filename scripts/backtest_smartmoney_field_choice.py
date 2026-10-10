# -*- coding: utf-8 -*-
"""round54 决策：data_completeness(聪明订单)因子，smart_money 字段选型对比。

对比两套 smart_money 方向字段：
  A) smart_money_pool_direction  (池内钱包持仓, 滞后/可能矛盾)
  B) net_up_imbalance            (近15分钟聪明钱净买UP, 实时)

分别与 book_imbalance 组合成「同向+2/矛盾-2」, 比较每个字段本身对 actual 的
方向命中率, 以及组合因子加入净分后对 side 判别的影响。决定生产用哪个。

字段方向口径: >0 偏 UP, <0 偏 DOWN (net_up_imbalance 即 UP 净买占比, 与 book 同口径)。
命中 = 210 结算记录 (hermes_results.jsonl) 与下单时刻快照对齐。
"""
import json, glob, os

def fval(v, d=None):
    if v is None: return d
    if isinstance(v, dict):
        for k in ("smart_money_pool_direction", "net_up_imbalance"):
            if k in v: return fval(v[k])
        return d
    try: return float(v)
    except: return d

def snap_dir(pool_direction, net_up_imbalance):
    """取 smart_money _dir 两种口径的方向。"""
    for k in ("smart_money_pool_direction", "net_up_imbalance"):
        pass
    return pool_direction, net_up_imbalance

snap = {}
for p in glob.glob(r"data\snapshots\bitcoin-*.json"):
    slug = os.path.basename(p).split("_")[0]
    try: o = json.load(open(p, encoding="utf-8"))
    except: continue
    t = o.get("collect_time_utc", "")
    if slug not in snap or t > snap[slug][0]:
        snap[slug] = (t, o)

results = {}
for line in open(r"data\hermes_results.jsonl", encoding="utf-8"):
    line = line.strip()
    if not line: continue
    rr = json.loads(line)
    results.setdefault(rr["slug"], {}).update({"actual": rr.get("actual"), "side": rr.get("side"), "pnl": rr.get("pnl")})

rows = []
for slug, (t, o) in snap.items():
    r = results.get(slug)
    if not r or not r["side"] or not r["actual"]: continue
    pm = o.get("polymarket") or {}
    ib = pm.get("book_imbalance")
    sm = pm.get("smart_money_consensus_1h") or {}
    pool_v = fval(sm.get("smart_money_pool_direction"))
    net_v = fval(sm.get("net_up_imbalance"))
    ib_sign = (ib > 0) - (ib < 0) if ib is not None else None
    pool_sign = (pool_v > 0) - (pool_v < 0) if pool_v is not None else None
    net_sign = (net_v > 0) - (net_v < 0) if net_v is not None else None
    rows.append({"slug": slug, "side": r["side"], "actual": r["actual"],
                 "pnl": float(r["pnl"]) if r["pnl"] is not None else 0,
                 "ib": ib_sign, "pool": pool_sign, "net": net_sign})

def hitrate(name, g):
    if not g: print(f"  {name:<34}: 空"); return
    win = sum(1 for r in g if r["side"] == r["actual"])
    print(f"  {name:<34}: n={len(g):>4} 命中={win:>3} {win/len(g)*100:5.1f}%")

print(f"可对齐已结算样本 {len(rows)}")
print("\n== 单字段方向对 actual 的命中(只统计该字段存在时) ==")
g_pool = [r for r in rows if r["pool"] is not None]
g_net = [r for r in rows if r["net"] is not None]
g_ib = [r for r in rows if r["ib"] is not None]
print("[book_imbalance]           ", "只做参照:")
hitrate("book_imbalance 存在时 side命中", [r for r in rows if r["ib"] is not None])
print("[smart_money_pool_direction]")
hitrate("pool_dir 存在时 side命中", g_pool)
# pool_dir 指向的方向是否与 actual 一致
up_p = [r for r in g_pool if r["pool"] > 0]; dn_p = [r for r in g_pool if r["pool"] < 0]
print("  其中 pool_dir>0(偏UP):", len(up_p), "场, 其中实际UP:", sum(1 for r in up_p if r["actual"]=="UP"), f"({sum(1 for r in up_p if r['actual']=='UP')/len(up_p)*100 if up_p else 0:.0f}%)")
print("  其中 pool_dir<0(偏DOWN):", len(dn_p), "场, 其中实际DOWN:", sum(1 for r in dn_p if r["actual"]=="DOWN"), f"({sum(1 for r in dn_p if r['actual']=='DOWN')/len(dn_p)*100 if dn_p else 0:.0f}%)")
print("[smart_money_consensus_1h.net_up_imbalance]")
hitrate("net_up 存在时 side命中", g_net)
up_n = [r for r in g_net if r["net"] > 0]; dn_n = [r for r in g_net if r["net"] < 0]
print("  其中 net_up>0(偏UP):", len(up_n), "场, 其中实际UP:", sum(1 for r in up_n if r["actual"]=="UP"), f"({sum(1 for r in up_n if r['actual']=='UP')/len(up_n)*100 if up_n else 0:.0f}%)")
print("  其中 net_up<0(偏DOWN):", len(dn_n), "场, 其中实际DOWN:", sum(1 for r in dn_n if r["actual"]=="DOWN"), f"({sum(1 for r in dn_n if r['actual']=='DOWN')/len(dn_n)*100 if dn_n else 0:.0f}%)")

print("\n== 组合因子(同向+2)选型: 换 smart_money 字段 ==")
def factor_for(r, sm_sign):
    if r["ib"] is not None and sm_sign is not None:
        return 2.0 if r["ib"] == sm_sign else -2.0
    return 0.0
# 方案A: book × pool_dir ; 方案B: book × net_up
print("[方案A] data_completeness 用 smart_money_pool_direction")
for b in ["同向+2","矛盾-2","缺信号0"]:
    g=[r for r in rows if (lambda fv: b==("同向+2" if fv==2 else "矛盾-2" if fv==-2 else "缺信号0"))(factor_for(r,r["pool"]))]
    hitrate(b, g)
print("[方案B] data_completeness 用 net_up_imbalance  (推荐候选)")
for b in ["同向+2","矛盾-2","缺信号0"]:
    g=[r for r in rows if (lambda fv: b==("同向+2" if fv==2 else "矛盾-2" if fv==-2 else "缺信号0"))(factor_for(r,r["net"]))]
    hitrate(b, g)