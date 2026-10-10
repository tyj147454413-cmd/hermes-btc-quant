# -*- coding: utf-8 -*-
"""round54 pm_pair 配对因子 验证 + 定权。
用户指定三个双因子配对: pm_momentum+price_position / pm_momentum+pm_consensus / pm_momentum+recent_dir_15m。
本脚本:
  1) 算样本外(时序最晚25%)这些配对与 pm_momentum 同向时的命中率;
  2) 试"同时 ≥k 个配对同向"的命中率, 决定 pm_pair 因子加权方案。
"""
import sys, os, glob, json
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import signal_validator as sv

results = {}
for line in open(r"data\hermes_results.jsonl", encoding="utf-8"):
    line = line.strip()
    if not line: continue
    rr = json.loads(line)
    results[rr["slug"]] = rr

snap = {}
for p in glob.glob(r"data\snapshots\bitcoin-*.json"):
    slug = os.path.basename(p).split("_")[0]
    try: o = json.load(open(p, encoding="utf-8"))
    except: continue
    t = o.get("collect_time_utc", "")
    if slug not in snap or t > snap[slug][0]:
        snap[slug] = (t, o)

rows = []
for slug, (t, o) in snap.items():
    r = results.get(slug)
    if not r or not r["actual"]: continue
    try:
        factors, _j, _n, _ = sv.extract_factors(o)
    except Exception:
        continue
    rows.append({"t": t, "actual": r["actual"], "F": factors})
rows.sort(key=lambda r: r["t"])

def fdir(r, k):
    v = r["F"].get(k, 0)
    return 1 if v > 0 else (-1 if v < 0 else 0)

n = len(rows)
test = rows[int(n*0.75):]
train = rows[:int(n*0.75)]
print(f"总{n}: 训练{len(train)} 样本外{len(test)}")

PAIRS = [("pm_momentum","price_position"),("pm_momentum","pm_consensus"),("pm_momentum","recent_dir_15m")]

def pair_stats(seg, name):
    print(f"\n=== [{name}] 单配对: pm_momentum 与该因子同向 ===")
    for a, b in PAIRS:
        hit = nn = 0
        for r in seg:
            da, db = fdir(r,a), fdir(r,b)
            if da and da == db:
                nn += 1
                if (da>0 and r["actual"]=="UP") or (da<0 and r["actual"]=="DOWN"): hit += 1
        print(f"  {a}+{b}: n={nn:>3} 命中={hit/nn*100 if nn else 0:.1f}%")
    print(f"=== [{name}] >=k 个配对同向(与 pm 同向) ===")
    for k in (1,2,3):
        hit=nn=0
        for r in seg:
            same = sum(1 for a,b in PAIRS if fdir(r,a) and fdir(r,a)==fdir(r,b))
            if same >= k:
                nn+=1
                d = fdir(r,"pm_momentum")
                if d and ((d>0 and r["actual"]=="UP") or (d<0 and r["actual"]=="DOWN")): hit+=1
        print(f"  ≥{k} 个同向: n={nn:>3} 命中={hit/nn*100 if nn else 0:.1f}%")

pair_stats(train, "训练段")
pair_stats(test, "样本外测试段")