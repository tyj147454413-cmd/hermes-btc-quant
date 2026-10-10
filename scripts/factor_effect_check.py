# -*- coding: utf-8 -*-
"""round54 因子有效性全量校验。
对每个已结算快照调用生产引擎 extract_factors, 对齐 actual, 统计每个因子:
  1) 触发(非0)时 指向方向 与 actual 的方向命中率  —— 单因子独立预测力
  2) 触发率(n触发/总样本)
生产用同一函数, 避免重算误差。方向语义: 因子>0 指 UP, <0 指 DOWN。
"""
import sys, os, glob, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import signal_validator as sv

# ---- 收集结算记录 ----
results = {}
for line in open(r"data\hermes_results.jsonl", encoding="utf-8"):
    line = line.strip()
    if not line: continue
    rr = json.loads(line)
    results[rr["slug"]] = {"actual": rr.get("actual"), "side": rr.get("side"),
                           "pnl": rr.get("pnl"), "prob": rr.get("prob")}

# ---- 收集快照(每 slug 取最新) ----
snap = {}
for p in glob.glob(r"data\snapshots\bitcoin-*.json"):
    slug = os.path.basename(p).split("_")[0]
    try: o = json.load(open(p, encoding="utf-8"))
    except: continue
    t = o.get("collect_time_utc", "")
    if slug not in snap or t > snap[slug][0]:
        snap[slug] = (t, o)

# ---- 每场跑引擎因子 ----
rows = []
err = 0
for slug,(t,o) in snap.items():
    r = results.get(slug)
    if not r or not r["actual"]: continue
    try:
        factors,_judge,notes,_mode = sv.extract_factors(o)
    except Exception as e:
        err += 1
        continue
    rows.append({"slug":slug,"actual":r["actual"],"side":r["side"],
                 "pnl":float(r["pnl"]) if r["pnl"] is not None else None,
                 "factors":factors})

print(f"已结算样本 {len(rows)} 场, extract_factors 异常跳过 {err}")

# ---- 单因子方向命中率 ----
print("\n=== 单因子独立方向命中率 (因子>0->UP, <0->DOWN, 只统计触发样本) ===")
# 先排除纯备选/不参与 side 的? 全部列出
summary=[]
for k in sorted(rows[0]["factors"].keys()):
    trig=[r for r in rows if r["factors"].get(k,0)!=0]
    if not trig: continue
    win=sum(1 for r in trig if (r["factors"][k]>0 and r["actual"]=="UP") or (r["factors"][k]<0 and r["actual"]=="DOWN"))
    # 平均触发值(看是否畸形:同符号占比)
    pos=[r for r in trig if r["factors"][k]>0]
    neg=[r for r in trig if r["factors"][k]<0]
    frac=win/len(trig)*100
    summary.append((frac,k,len(trig),len(pos),len(neg)))
summary.sort(reverse=True)
print(f"{'命中率':>7} {'因子名':<24} {'n触发':>6} {'数UP':>4} {'数DOWN':>5}")
for frac,k,n,np_,nn_ in summary:
    bar="█"*int(frac/10)
    print(f"{frac:5.1f}%  {k:<24} {n:>6} {np_:>4} {nn_:>5}  {bar}")

# ---- 因子触发时的 side(引擎最终方向) 是否正确: 佐证该因子出现在净分里时是否把引擎带对 ----
print("\n=== 因子触发的场次里, 引擎最终 side 的命中率 (整个净分含该因子时) ===")
d=[]
for k in sorted(rows[0]["factors"].keys()):
    trig=[r for r in rows if r["factors"].get(k,0)!=0]
    if not trig: continue
    win=sum(1 for r in trig if r["side"]==r["actual"])
    d.append((win/len(trig)*100,k,len(trig)))
d.sort(reverse=True)
print(f"{'side命中率':>9} {'因子名':<24} {'n':>5}")
for frac,k,n in d:
    print(f"{frac:8.1f}%  {k:<24} {n:>5}")

# ---- 当前引擎净分方向命中率基线 ----
# 净分 S0 = Σfactors(主判模式), 方向= sign(S0)。对齐 actual。
from collections import Counter
net_hit=net_n=0
for r in rows:
    s0 = sum(r["factors"].values())
    if s0==0: continue
    net_n+=1
    pred = "UP" if s0>0 else "DOWN"
    if pred==r["actual"]: net_hit+=1
print(f"\n=== 当前引擎净分方向基线: n={net_n} 命中={net_hit} {net_hit/net_n*100:.1f}% ===")

# ---- 强因子同向共振找高胜率组合 ----
# 用方向命中率>70% 的前5强因子做"同向共振": ≥k个强因子指向同一方向时,该方向命中率
strong = ["pm_momentum","price_position","pm_consensus","data_completeness","recent_dir_15m"]
def dir_of(r,k):  # 因子方向: >0->UP, <0->DOWN, 0->None
    v=r["factors"].get(k,0)
    return "UP" if v>0 else ("DOWN" if v<0 else None)
print("\n=== 强因子同向共振命中率 (指向同一方向的强因子个数) ===")
from collections import defaultdict
agg=defaultdict(lambda:[0,0])
for r in rows:
    vote=Counter(dir_of(r,k) for k in strong)
    for dname in ("UP","DOWN"):
        cnt=vote.get(dname,0)
        if cnt>=0:
            hit = 1 if r["actual"]==dname else 0
            agg[min(cnt,5)][0]+=hit; agg[min(cnt,5)][1]+=1
for k in sorted(agg):
    tot,nn=agg[k]
    print(f"  >= {k} 个强因子同向: n={nn:>4} 该方向命中={tot/nn*100 if nn else 0:.1f}%")

# ---- 相邻双因子组合 top 命中率 ----
print("\n=== 双因子组合 同向命中率 Top (两因子同指方向时,该方向命中) ===")
keys=[k for k in strong]
combos=[]
for i in range(len(keys)):
    for j in range(i+1,len(keys)):
        a,b=keys[i],keys[j]
        hit=nn=0
        for r in rows:
            da,db=dir_of(r,a),dir_of(r,b)
            if da and da==db:
                nn+=1
                if r["actual"]==da: hit+=1
        if nn>=15:
            combos.append((hit/nn*100,(a,b),nn,hit))
combos.sort(reverse=True)
print(f"{'命中率':>7} {'组合':<38} {'n':>4}")
for frac,(a,b),nn,hit in combos[:12]:
    print(f"{frac:5.1f}%  {a}+{b:<30} {nn:>4} ({hit})")