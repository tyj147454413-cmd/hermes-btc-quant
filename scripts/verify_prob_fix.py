# -*- coding: utf-8 -*-
"""round54.1 修复验证：概率口径剔除 resonance/pm_pair 后, 26 场升档场归位。
端到端: extract_factors -> rule_engine.judge -> d.probability, 对比含/不含共振分的档位。
"""
import sys, os, glob, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import signal_validator as sv
import rule_engine as re_

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

def band(v):
    a = abs(v)
    return 3 if a >= 6 else (2 if a >= 3 else 1)

upgraded = []
tot = 0
for slug, (t, o) in snap.items():
    r = results.get(slug)
    if not r or not r["actual"]: continue
    try:
        f, inp, _n, _m = sv.extract_factors(o)
    except Exception:
        continue
    d = re_.judge(inp)
    tot += 1
    res = f.get("resonance", 0) + f.get("pm_pair", 0)
    if res == 0: continue
    # d.net 含共振; net_for_prob(修复后)=d.net - res
    s0_new = d.net
    s0_old = d.net - res
    if band(s0_new) > band(s0_old):
        upgraded.append((slug, s0_old, s0_new, d.probability, d.confidence, r["actual"], d.final_direction))

print(f"总 {tot} 场, 含共振升档场 {len(upgraded)} 场")
ok = 0
for slug, so, sn, prob, conf, act, dirn in upgraded:
    pred = dirn
    hit = pred == act
    ok += hit
    # 旧档位概率对照
    band_old = "6+" if abs(so) >= 6 else ("3-5" if abs(so) >= 3 else "0-2")
    print(f"  {slug[-16:]}: 旧|净分|={abs(so):.0f}({band_old}) -> 概率读 {prob}% 置信{conf} 判{pred} 实际{act} {'✅' if hit else '❌'}")
print(f"\n升档场命中 {ok}/{len(upgraded)} = {ok/len(upgraded)*100:.1f}%")
# 验证修复: 升档场的概率应已按旧档位给(3-5档), 而非 84.6
prob_846 = [u for u in upgraded if u[3] == 84.6]
print(f"升档场中概率=84.6(6+高置信表值)的: {len(prob_846)} 场  <- 修复后应为少数/仅真6+场")
