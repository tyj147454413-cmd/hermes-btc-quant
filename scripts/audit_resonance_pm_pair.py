# -*- coding: utf-8 -*-
"""round54 审计：resonance + pm_pair 进净分后, 引擎方向决策的反事实对比。
S0_new = sum(factors)  (含 resonance/pm_pair, 当前生产代码)
S0_old = S0_new - resonance - pm_pair  (反事实: 未加这两个因子的旧引擎)
对比两版的方向命中率 + 列出被翻转的场次明细, 验证新增因子是改进还是伤害。
"""
import sys, os, glob, json
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
        f, _j, _n, _ = sv.extract_factors(o)
    except Exception:
        continue
    s0_new = sum(f.values())
    s0_old = s0_new - f.get("resonance", 0) - f.get("pm_pair", 0)
    rows.append({"t": t, "slug": slug, "actual": r["actual"], "side": r.get("side"),
                 "f": f, "s0_new": s0_new, "s0_old": s0_old})

rows.sort(key=lambda r: r["t"])
n = len(rows)
test = rows[int(n*0.75):]

def hit(seg, key):
    tot = win = 0
    for r in seg:
        if r[key] == 0: continue
        pred = "UP" if r[key] > 0 else "DOWN"
        tot += 1
        if pred == r["actual"]: win += 1
    return win, tot

print(f"总 {n} 场 | 样本外段(最晚25%) {len(test)} 场")
for name, seg in (("全量", rows), ("样本外", test)):
    w_old, t_old = hit(seg, "s0_old")
    w_new, t_new = hit(seg, "s0_new")
    print(f"\n[{name}] 旧净分: {w_old}/{t_old} = {w_old/t_old*100:.1f}% | 新净分: {w_new}/{t_new} = {w_new/t_new*100:.1f}%")
    diff = (w_new - w_old)
    print(f"  方向判决数: 旧 {t_old} -> 新 {t_new} | 命中变化: {diff:+d}")

# 翻转场次明细: 新旧方向不同的场
flips = [r for r in rows if (r["s0_old"] > 0) != (r["s0_new"] > 0) and not (r["s0_old"] == 0 or r["s0_new"] == 0)]
flip_good = sum(1 for r in flips if (("UP" if r["s0_new"] > 0 else "DOWN") == r["actual"]))
print(f"\n=== 方向被翻转的场次: {len(flips)} 场, 其中翻对 {flip_good} 场, 翻错 {len(flips)-flip_good} 场 ===")
for r in flips[:12]:
    pred_new = "UP" if r["s0_new"] > 0 else "DOWN"
    ok = "✅翻对" if pred_new == r["actual"] else "❌翻错"
    print(f"  {r['slug'][-14:]}: S0 {r['s0_old']:+.1f} -> {r['s0_new']:+.1f} "
          f"(res={r['f'].get('resonance',0):+.0f} pair={r['f'].get('pm_pair',0):+.0f}) 判{pred_new} 实际{r['actual']} {ok}")

# 零平票边界: S0_old==0 旧引擎不判, 新引擎有判的场景
zero_to_judge = [r for r in rows if r["s0_old"] == 0 and r["s0_new"] != 0]
zg = sum(1 for r in zero_to_judge if ("UP" if r["s0_new"] > 0 else "DOWN") == r["actual"])
print(f"\n=== 旧净分为0(不判) -> 新有判: {len(zero_to_judge)} 场, 判对 {zg} ({zg/len(zero_to_judge)*100 if zero_to_judge else 0:.0f}%) ===")