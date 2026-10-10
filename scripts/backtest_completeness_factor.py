# -*- coding: utf-8 -*-
"""验证「方向一致+2/矛盾-2」因子语义是否提升引擎决策质量。

判据: 引擎已定 side。比较
  - 同向子集(ib 与 sm 方向一致) 的 side 命中率  vs 矛盾子集
  - 该因子加入净分后, 用 (score + factor) vs (score) 的判别力
仅使用下单时刻可知信号。"""
import json, glob, os

def fval(v, d=None):
    if v is None: return d
    if isinstance(v, dict):
        for k in ("smart_money_pool_direction", "net_up_imbalance"):
            if k in v: return fval(v[k])
        return d
    try: return float(v)
    except: return d

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
    results.setdefault(rr["slug"], {}).update({
        "actual": rr.get("actual"), "side": rr.get("side"),
        "pnl": rr.get("pnl"), "score": rr.get("net_score"),
        "prob": rr.get("probability_raw"), "buy_price": rr.get("buy_price")})

rows = []
for slug, (t, o) in snap.items():
    r = results.get(slug)
    if not r or not r["side"] or not r["actual"]: continue
    pm = o.get("polymarket") or {}
    ib = pm.get("book_imbalance")
    smd = fval((pm.get("smart_money_consensus_1h") or {}).get("smart_money_pool_direction"))
    ib_sign = (ib > 0) - (ib < 0) if ib is not None else None
    sm_sign = (smd > 0) - (smd < 0) if smd is not None else None
    # 因子: 同向+2 / 矛盾-2 / 缺任0
    if ib_sign is not None and sm_sign is not None:
        factor = 2.0 if ib_sign == sm_sign else -2.0
        bucket = "同向+2" if ib_sign == sm_sign else "矛盾-2"
    else:
        factor = 0.0
        bucket = "缺信号0"
    rows.append({"slug": slug, "side": r["side"], "actual": r["actual"],
                 "pnl": float(r["pnl"]) if r["pnl"] is not None else 0,
                 "bp": float(r["buy_price"]) if r.get("buy_price") is not None else None,
                 "ib": ib_sign, "sm": sm_sign, "factor": factor, "bucket": bucket,
                 "score": r["score"]})

def show(name, g):
    if not g:
        print(f"  {name:<22}: 空"); return
    win = sum(1 for r in g if r["side"] == r["actual"])
    pnl = sum(r["pnl"] for r in g)
    bps = [r["bp"] for r in g if r["bp"] is not None]
    bpm = (sum(bps)/len(bps)) if bps else 0
    print(f"  {name:<22}: n={len(g):>4} 命中={win:>3} {win/len(g)*100:5.1f}%  Σpnl={pnl:+9.2f} 均pnl={pnl/len(g):+7.2f} 均价B={bpm:.2f}")

print(f"可判样本 {len(rows)}")
print("\n== 因子分档 × 引擎side命中率 ==")
for b in ["同向+2", "矛盾-2", "缺信号0"]:
    show(b, [r for r in rows if r["bucket"] == b])
show("全部(基线)", rows)

# 判别力: 因子符号 vs 单子对错的互信息式比较
print("\n== 因子方向 vs 单子对错 ==")
for b in ["同向+2", "矛盾-2"]:
    g = [r for r in rows if r["bucket"] == b]
    win = sum(1 for r in g if r["side"] == r["actual"])
    print(f"  {b:<22}: 变更因子上到+2/-2后的命中={win/len(g)*100:.1f}% (n={len(g)})")

# 关键: 同向+2 vs 矛盾-2 命中率差是否显著(V成对), 以及因子是否真的与net方向一致
print("\n== 与引擎 net_score 方向一致性 (因子是否有效放大共识) ==")
agree = disagree = both_known = 0
for r in rows:
    sc = r["score"]
    if sc is None or r["factor"] == 0: continue
    both_known += 1
    if sc * r["factor"] > 0: agree += 1
    else: disagree += 1
print(f"  因子与net_score同号: {agree}/{both_known} = {agree/both_known*100 if both_known else 0:.0f}% | 异号: {disagree}")