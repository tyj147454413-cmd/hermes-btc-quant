# -*- coding: utf-8 -*-
"""round54 组合因子 样本外验证 (时序切分)。
判断强因子共振(>=3 个同向) 与 top 双因子组合 是否有过拟合:
把 460 场按 collect 时间由旧到新排序, 取最晚 25% 作为"样本外/未来"段,
检验这些组合在样本外段的命中率是否仍显著 > 基线。
同期 PPM 盘口价作为对照(市场一致则样本外天然高, 需扣除)。
"""
import sys, os, glob, json
from collections import Counter, defaultdict
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
    pm = (o.get("polymarket") or {})
    up_price = pm.get("up_price")
    rows.append({"t": t, "slug": slug, "actual": r["actual"],
                 "factors": factors, "up_price": up_price})

rows.sort(key=lambda r: r["t"])
n = len(rows)
test = rows[int(n*0.75):]  # 最晚25% 作样本外
train = rows[:int(n*0.75)]
print(f"总 {n} 场: 训练段 {len(train)}, 样本外测试段 {len(test)} (最晚25%)")

def dir_of(r, k):
    v = r["factors"].get(k, 0)
    return "UP" if v > 0 else ("DOWN" if v < 0 else None)

strong = ["pm_momentum", "price_position", "pm_consensus", "data_completeness", "recent_dir_15m"]

def resonance_stats(seg, name):
    print(f"\n--- [{name}] 强因子同向共振 n={len(seg)} ---")
    for thr in (3, 4):
        hit = nn = 0
        for r in seg:
            vote = Counter(dir_of(r, k) for k in strong)
            for d in ("UP", "DOWN"):
                if vote.get(d, 0) >= thr:
                    nn += 1
                    if r["actual"] == d: hit += 1
        print(f"  >= {thr} 同向: n={nn:>3} 命中={hit/nn*100 if nn else 0:.1f}%")

resonance_stats(train, "训练段")
resonance_stats(test, "样本外测试段")

# top 双因子组合 样本外
pairs = [("price_position","data_completeness"),("pm_momentum","price_position"),
         ("pm_consensus","data_completeness"),("pm_momentum","pm_consensus"),
         ("pm_momentum","recent_dir_15m")]
for a, b in pairs:
    for name, seg in (("训练",train),("样本外",test)):
        hit = nn = 0
        for r in seg:
            da, db = dir_of(r,a), dir_of(r,b)
            if da and da == db:
                nn += 1
                if r["actual"] == da: hit += 1
        print(f"  {a}+{b} [{name}]: n={nn:>3} 命中={hit/nn*100 if nn else 0:.1f}%")
    print()

# 对照: 市场盘口价 up_price 也看"样本外"是否本来就高(扣除市场一致效应)
print("--- 对照组: 仅 up_price>0.6(市场偏UP) 样本外命中 ---")
for name, seg in (("训练",train),("样本外",test)):
    g = [r for r in seg if r["up_price"] and r["up_price"] > 0.6]
    hit = sum(1 for r in g if r["actual"] == "UP")
    print(f"  {name}: n={len(g)} 命中={hit/len(g)*100 if g else 0:.1f}%")