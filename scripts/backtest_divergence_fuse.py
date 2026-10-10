# -*- coding: utf-8 -*-
"""背离熔断回测: 在分析了时刻,方向信号(book_imbalance/smart_money)与引擎side相反则SKIP
输出: 错单屏蔽率 + 对单误杀率
仅使用下单时刻可知的信号,不偷看结算。"""
import json, glob, os

def fval(v, d=None):
    if v is None: return d
    if isinstance(v, dict):
        for k in ("smart_money_pool_direction", "net_up_imbalance"):
            if k in v: return fval(v[k])
        return d
    try: return float(v)
    except: return d

# 1) 每场的最后一份快照(=分析时刻)
snap = {}
for p in glob.glob(r"data\snapshots\bitcoin-*.json"):
    slug = os.path.basename(p).split("_")[0]
    try: o = json.load(open(p, encoding="utf-8"))
    except: continue
    t = o.get("collect_time_utc", "")
    if slug not in snap or t > snap[slug][0]:
        snap[slug] = (t, o)

# 2) 结果
results = {}
for line in open(r"data\hermes_results.jsonl", encoding="utf-8"):
    line = line.strip()
    if not line: continue
    rr = json.loads(line)
    results.setdefault(rr["slug"], {}).update({"actual": rr.get("actual"), "side": rr.get("side")})

# 3) 构建每条可判样本
rows = []
for slug, (t, o) in snap.items():
    r = results.get(slug)
    if not r or not r["side"] or not r["actual"]: continue
    pm = o.get("polymarket") or {}
    ib = pm.get("book_imbalance")
    smd = fval((pm.get("smart_money_consensus_1h") or {}).get("smart_money_pool_direction"))
    up = pm.get("up_price")
    side = 1 if r["side"] == "UP" else -1
    actual = 1 if r["actual"] == "UP" else -1
    ib_sign = (ib > 0) - (ib < 0) if ib is not None else None
    sm_sign = (smd > 0) - (smd < 0) if smd is not None else None
    rows.append({"slug": slug, "side": side, "actual": actual,
                 "wrong": side != actual, "ib": ib_sign, "sm": sm_sign, "up": up})

n_wrong = sum(1 for r in rows if r["wrong"])
n_right = len(rows) - n_wrong
print(f"可判样本: {len(rows)} (错单 {n_wrong} / 对单 {n_right})")

# 4) 熔断变体
variants = {
    "V1 book_imb与side相反": lambda r: r["ib"] is not None and r["ib"] == -r["side"],
    "V2 smart_money与side相反": lambda r: r["sm"] is not None and r["sm"] == -r["side"],
    "V3 两者任一相反": lambda r: (r["ib"] is not None and r["ib"] == -r["side"]) or (r["sm"] is not None and r["sm"] == -r["side"]),
    "V4 两者都相反": lambda r: (r["ib"] is not None and r["ib"] == -r["side"]) and (r["sm"] is not None and r["sm"] == -r["side"]),
    # 信号内背离: 盘口价方向 vs 挂单失衡/聪明钱方向 打架
    "V5 盘口价与book_imb背离": lambda r: r["up"] is not None and r["ib"] is not None and ((r["up"] < 0.5 and r["ib"] > 0) or (r["up"] > 0.5 and r["ib"] < 0)),
    "V6 盘口价与smart_money背离": lambda r: r["up"] is not None and r["sm"] is not None and ((r["up"] < 0.5 and r["sm"] > 0) or (r["up"] > 0.5 and r["sm"] < 0)),
    "V7 盘口价与side同向但含book/sm背离": lambda r: (r["up"] is not None and r["ib"] is not None and ((r["up"]<0.5 and r["ib"]>0) or (r["up"]>0.5 and r["ib"]<0))) or (r["up"] is not None and r["sm"] is not None and ((r["up"]<0.5 and r["sm"]>0) or (r["up"]>0.5 and r["sm"]<0))),
}
print("\n== 背离熔断回测 ==")
print(f"{'变体':<40}{'屏蔽错单':>10}{'屏蔽率':>8}{'误杀对单':>10}{'误杀率':>8}")
for name, cond in variants.items():
    # 该变体触发(熔断)了多少错单/对单
    fuse_wrong = sum(1 for r in rows if cond(r) and r["wrong"])
    fuse_right = sum(1 for r in rows if cond(r) and not r["wrong"])
    print(f"{name:<40}{fuse_wrong:>8}/{n_wrong:<8}{fuse_wrong/n_wrong*100 if n_wrong else 0:>7.1f}%"
          f"{fuse_right:>8}/{n_right:<8}{fuse_right/n_right*100 if n_right else 0:>7.0f}%")

# 5) 保守组合: 只有强信号(ib方向明确)才熔断; 仅当信号可用率够高
print("\n== 精细: 信号可用的子集上重算 ==")
for label, pred_only in [("仅book_imb可用", lambda r: r["ib"] is not None), ("仅smart_money可用", lambda r: r["sm"] is not None)]:
        sub = [r for r in rows if pred_only(r)]
        sw = sum(1 for r in sub if r["wrong"]); sr = len(sub) - sw
        fw = sum(1 for r in sub if (r["ib"] == -r["side"]) or (r["sm"] == -r["side"]))
        fr = sum(1 for r in sub if not r["wrong"] and ((r["ib"] == -r["side"]) or (r["sm"] == -r["side"])))
        print(f"{label:<22}: 可用{len(sub):>4} | 原始错单率 {sw}/{len(sub)}={sw/len(sub)*100 if sub else 0:.0f}% | V3熔断: 屏蔽错单 {fw}/{sw}={fw/sw*100 if sw else 0:.0f}%, 误杀对单 {fr}/{sr}={fr/sr*100 if sr else 0:.0f}%")