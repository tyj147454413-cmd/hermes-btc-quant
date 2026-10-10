#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
factor_hitrate_study.py — 因子级大清洗（第四步·长期）
=====================================================
目的：全历史统计每个因子的方向命中率——哪些因子在哪类行情真有效、哪些是噪音。
铁律：只做统计诊断，不改 rule_engine 权重、不改任何规则文件。

方法：
  - 每个小时 slug 取「最后一份快照」（最接近决策时刻的市场状态）
  - 与 hermes_results.jsonl 的 actual（结算真值）按 slug 连接
  - 每个因子输出 UP/DOWN 方向票，统计 票方向==实际方向 的命中率
  - Wilson 95% 置信区间；样本 <15 标注「样本不足」
  - 切片：波动率档 / |偏离|档 / 时段(ET) / PM共识强度 / 前后半段稳定性
  - 附带朴素 EV：假设按快照隐含价买入票方向，每 $100 本金期望美元
"""
import json, math, os, sys, glob, re
from collections import defaultdict

HERMES_DIR = os.environ.get("HERMES_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERMES_DIR, "data")
SNAP_DIR = os.path.join(DATA, "snapshots")

# ---------------- 工具 ----------------
def wilson(p, n, z=1.96):
    if n == 0: return (0.0, 1.0)
    den = 1 + z*z/n
    center = (p + z*z/(2*n)) / den
    half = z*math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / den
    return (max(0, center-half), min(1, center+half))

def sgn(x, th=0.0):
    if x is None: return None
    if x > th: return "UP"
    if x < -th: return "DOWN"
    return None

SLUG_RE = re.compile(r"bitcoin-up-or-down-(\w+)-(\d+)-(\d+)-(\d+)(am|pm)-et")

def slug_et_hour(slug):
    m = SLUG_RE.search(slug)
    if not m: return None
    h = int(m.group(4))
    ap = m.group(5)
    if ap == "am":
        return 0 if h == 12 else h
    return 12 if h == 12 else h + 12

def month_num(s):
    return {"january":1,"february":2,"march":3,"april":4,"may":5,"june":6,"july":7,
            "august":8,"september":9,"october":10,"november":11,"december":12}.get(s.lower(), 0)

def slug_day(slug):
    m = SLUG_RE.search(slug)
    if not m: return (0, 0, 0)
    return (month_num(m.group(1)), int(m.group(3)), int(m.group(2)))  # 月固定8月为主

# ---------------- 载入 ----------------
def load_last_snapshots():
    """slug -> 最后一份快照"""
    out = {}
    cnt = defaultdict(int)
    files = sorted(glob.glob(os.path.join(SNAP_DIR, "*.json")))
    for f in files:
        base = os.path.basename(f)               # <slug>_<yyyymmdd_hhmmss>.json
        slug = base.rsplit("_", 2)[0]
        try:
            d = json.load(open(f))
        except Exception:
            continue
        cnt[slug] += 1
        ts = base.rsplit("_", 2)[1] + base.rsplit("_", 2)[2].replace(".json", "")
        if slug not in out or ts > out[slug][0]:
            out[slug] = (ts, d)
    return {k: v[1] for k, v in out.items()}, cnt

def load_results():
    """slug -> actual"""
    res = {}
    p = os.path.join(DATA, "hermes_results.jsonl")
    if not os.path.exists(p): return res
    for line in open(p):
        line = line.strip()
        if not line: continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        res[r.get("slug")] = r.get("actual")
    return res

# ---------------- 因子提取（预注册口径，不事后挑） ----------------
def tf(snap, name):
    return snap.get("timeframes", {}).get(name, {}) or {}

def extract_factors(s):
    """返回 dict: 因子名 -> 'UP'/'DOWN'/None"""
    f = {}
    anchor = s.get("anchor_price"); cur = s.get("current_price")
    dev = None
    if anchor and cur:
        dev = (cur - anchor) / anchor * 100.0
    f["偏离延续(dev符号)"] = sgn(dev, 0.02)
    dt = s.get("deviation_trend", {})
    # 偏离扩张=顺当前方向票；收缩=反向票
    cur_side = sgn(dev, 0.02)
    if dt.get("expanding"): f["偏离扩张"] = cur_side
    elif dt.get("shrinking"): f["偏离收缩反转"] = ("DOWN" if cur_side == "UP" else "UP") if cur_side else None
    else: f["偏离扩张"] = None
    f["偏离收缩反转"] = f.get("偏离收缩反转")

    # 多周期 recent_dir 动量
    for tfn in ["1m", "5m", "15m", "1H", "4H"]:
        rd = tf(s, tfn).get("recent_dir")
        f[f"recent_dir_{tfn}"] = rd if rd in ("UP", "DOWN") else None

    # 连续涨跌（5m/1H）：≥3 同向才出票
    for tfn in ["5m", "1H"]:
        t = tf(s, tfn)
        cu, cd = t.get("consecutive_up") or 0, t.get("consecutive_down") or 0
        f[f"连续K线_{tfn}"] = ("UP" if cu >= 3 else "DOWN" if cd >= 3 else None)

    # MACD：15m hist 符号 + 加速度；5m hist 符号
    t15 = tf(s, "15m")
    f["MACD_15m"] = sgn(t15.get("macd_hist"), 0.5)
    acc = None
    if t15.get("macd_hist") is not None and t15.get("macd_hist_prev") is not None:
        acc = t15["macd_hist"] - t15["macd_hist_prev"]
    f["MACD加速度_15m"] = sgn(acc, 0.25)
    f["MACD_5m"] = sgn(tf(s, "5m").get("macd_hist"), 0.5)

    # 布林/RSI 均值回归（15m，预注册为反向口径）
    bp = t15.get("bb_pos")
    f["布林位置MR_15m"] = ("DOWN" if (bp is not None and bp > 0.8) else "UP" if (bp is not None and bp < 0.2) else None)
    rsi = t15.get("rsi")
    f["RSI超买超卖MR_15m"] = ("DOWN" if (rsi is not None and rsi > 65) else "UP" if (rsi is not None and rsi < 35) else None)

    # 资金面
    cf = s.get("capital_flow", {})
    f["TakerB/S"] = sgn((cf.get("taker_bs_ratio") or 0) - 1.0, 0.05)
    f["TakerB/S_1h均值"] = sgn((cf.get("taker_bs_avg_1h") or 0) - 1.0, 0.03)
    fr = cf.get("funding_rate")
    f["资金费率"] = sgn(fr, 0.0) if fr is not None else None
    lsr = cf.get("long_short_ratio")
    f["多空比"] = sgn((lsr or 0) - 1.0, 0.02)
    of = cf.get("order_flow", {})
    f["订单流失衡_5m"] = sgn(of.get("imbalance_5m"), 0.10)
    f["订单流失衡_15m"] = sgn(of.get("imbalance_15m"), 0.10)
    oi_sig = (s.get("oi_price_relation") or {}).get("signal")
    f["OI价格关系"] = oi_sig if oi_sig in ("UP", "DOWN") else None

    # 宏观趋势 1H/4H 序列（从 klines_1h 自算最近4根方向）
    kl = s.get("klines_1h") or []
    dirs4 = []
    for i in range(max(1, len(kl) - 4), len(kl)):
        try:
            o, c = float(kl[i]["open"]), float(kl[i]["close"])
            dirs4.append(1 if c > o else -1)
        except Exception:
            pass
    if len(dirs4) == 4:
        if sum(dirs4) >= 3: f["宏观1H4H序列"] = "UP"
        elif sum(dirs4) <= -3: f["宏观1H4H序列"] = "DOWN"
        else: f["宏观1H4H序列"] = None
    else:
        f["宏观1H4H序列"] = None

    # 24h 高低位置（动量口径：上高位追涨）
    pp = s.get("price_position_24h", {})
    pctile = pp.get("current_percentile")
    f["24h位置动量"] = ("UP" if (pctile is not None and pctile > 70) else "DOWN" if (pctile is not None and pctile < 30) else None)

    # 恐贪指数 反向（prompt 口径 F&G 低=contrarian 看涨）
    fg = (s.get("sentiment") or {}).get("fear_greed_index")
    f["恐贪反向"] = ("UP" if (fg is not None and fg < 30) else "DOWN" if (fg is not None and fg > 70) else None)

    # 历史时段季节性
    hist = s.get("historical", {})
    ur = hist.get("up_rate")
    f["历史时段UP率"] = ("UP" if (ur is not None and ur > 0.55) else "DOWN" if (ur is not None and ur < 0.45) else None)

    # 上小时动量延续
    ph = s.get("prev_hour", {})
    f["上小时动量延续"] = sgn(ph.get("change_pct"), 0.05)

    # 开盘结构
    op = s.get("opening", {})
    od = op.get("dir")
    f["开盘方向"] = od if od in ("UP", "DOWN") else None
    if op.get("has_fakeout"):
        f["假突破陷阱(round39)"] = ("DOWN" if od == "UP" else "UP") if od in ("UP", "DOWN") else None
    else:
        f["假突破陷阱(round39)"] = None
    vr = (s.get("v_reversal_15m") or {}).get("detected")
    f["V反确认"] = (("DOWN" if od == "UP" else "UP") if od in ("UP", "DOWN") else None) if vr else None

    # PM 共识领涨领跌
    pm = s.get("polymarket") or {}
    up_p = pm.get("up_price")
    f["PM共识跟随"] = ("UP" if (up_p is not None and up_p > 0.60) else "DOWN" if (up_p is not None and up_p < 0.40) else None)

    return f, {"dev": dev, "vol5m": s.get("volatility_5m"), "et_hour": slug_et_hour(s.get("slug", "")),
               "up_price": up_p, "ts": s.get("timestamp", "")}

# ---------------- 主流程 ----------------
def main():
    snaps, cnt = load_last_snapshots()
    results = load_results()
    print(f"唯一slug快照: {len(snaps)}  结算真值: {len(results)}")

    rows = []
    for slug, s in sorted(snaps.items()):
        actual = results.get(slug)
        if actual not in ("UP", "DOWN"):
            continue
        fac, meta = extract_factors(s)
        rows.append({"slug": slug, "actual": actual, "fac": fac, **meta})
    print(f"可归因小时数(快照×结算): {len(rows)}")
    if not rows:
        print("无数据可分析"); return

    # 时间排序键（月,日,时）
    rows.sort(key=lambda r: (r["slug"][16:26], slug_et_hour(r["slug"]) or 0))

    FACTORS = list(rows[0]["fac"].keys())
    half = len(rows) // 2

    def evaluate(subset, label=""):
        out = []
        for fname in FACTORS:
            n = h = ev_dollar = 0.0
            n_ev = 0
            for r in subset:
                v = r["fac"].get(fname)
                if v is None: continue
                n += 1
                if v == r["actual"]: h += 1
                # 朴素EV：按快照隐含价入场
                p = r.get("up_price")
                if p and 0.01 < p < 0.99:
                    pe = p if v == "UP" else 1 - p
                    # 每$100本金: 赢得 (100/pe -100), 输 -100
                    ev_dollar += ((100.0/pe - 100.0) if v == r["actual"] else -100.0)
                    n_ev += 1
            if n == 0:
                continue
            hr = h / n
            lo, hi = wilson(hr, n)
            out.append({
                "factor": fname, "n": int(n), "hit": int(h), "rate": hr,
                "ci": (lo, hi),
                "ev_per_$100": round(ev_dollar / n_ev, 1) if n_ev >= 10 else None,
                "weak": n < 15,
            })
        out.sort(key=lambda x: -x["n"])
        return out

    def fmt_table(evals, title):
        lines = [f"\n### {title}\n", "| 因子 | 出票样本 | 命中 | 命中率 | 95%CI | 朴素EV/$100 | 判定 |", "|---|---|---|---|---|---|---|"]
        for e in evals:
            verdict = "🟢有效" if (not e["weak"] and e["rate"] >= 0.58 and e["ci"][0] > 0.50) else (
                      "🔴反向" if (not e["weak"] and e["rate"] <= 0.42 and e["ci"][1] < 0.50) else (
                      "⚪噪音" if not e["weak"] else "样本不足"))
            ev_s = f"{e['ev_per_$100']:+.1f}" if e["ev_per_$100"] is not None else "—"
            lines.append(f"| {e['factor']} | {e['n']} | {e['hit']} | {e['rate']*100:.1f}% | [{e['ci'][0]*100:.0f}%,{e['ci'][1]*100:.0f}%] | {ev_s} | {verdict} |")
        return "\n".join(lines)

    # ---- 报告组装 ----
    rep = ["# 因子级大清洗 · 全历史命中率统计（第四步·长期）",
           f"\n生成: 快照×结算 可归因样本 = **{len(rows)} 小时**（{rows[0]['slug'][:40]} … {rows[-1]['slug'][:40]}）",
           "口径：每小时取最后一份快照的因子读数为方向票，对比结算真值；只统计，不改权重。\n"]

    rep.append(fmt_table(evaluate(rows), "一、全体样本总表"))

    # 切片函数
    def sliced(name, fn):
        sub = [r for r in rows if fn(r)]
        if len(sub) < 15:
            rep.append(f"\n### {name}：样本不足({len(sub)})，跳过")
            return
        rep.append(fmt_table(evaluate(sub), f"{name}（n={len(sub)}）"))

    vols = sorted([r["vol5m"] for r in rows if r["vol5m"] is not None])
    med_v = vols[len(vols)//2] if vols else 0.4
    sliced(f"二a、低波动(vol5m<{med_v:.2f}%)", lambda r: (r["vol5m"] or 0) < med_v)
    sliced(f"二b、高波动(vol5m≥{med_v:.2f}%)", lambda r: (r["vol5m"] or 999) >= med_v)

    sliced("三a、贴线期(|dev|<0.05%)", lambda r: abs(r["dev"] or 0) < 0.05)
    sliced("三b、明确偏离(|dev|≥0.15%)", lambda r: abs(r["dev"] or 0) >= 0.15)

    sliced("四a、亚盘深夜(ET 20-02时)", lambda r: (r["et_hour"] or 0) >= 20 or (r["et_hour"] or 0) < 2)
    sliced("四b、欧盘(ET 2-8时)", lambda r: 2 <= (r["et_hour"] or 0) < 8)
    sliced("四c、美盘(ET 8-16时)", lambda r: 8 <= (r["et_hour"] or 0) < 16)
    sliced("四d、美盘晚(ET 16-20时)", lambda r: 16 <= (r["et_hour"] or 0) < 20)

    sliced("五a、PM平衡(|up_price-0.5|<0.08)", lambda r: abs((r["up_price"] or 0.5) - 0.5) < 0.08)
    sliced("五b、PM偏侧(≥0.08)", lambda r: abs((r["up_price"] or 0.5) - 0.5) >= 0.08)

    rep.append(fmt_table(evaluate(rows[:half]), f"六a、前半段稳定性(n={half})"))
    rep.append(fmt_table(evaluate(rows[half:]), f"六b、后半段稳定性(n={len(rows)-half})"))

    # 净分架构验证
    score_rows = []
    for r in rows:
        sc = sum(1 if v == "UP" else -1 for v in r["fac"].values() if v)
        score_rows.append((sc, r))
    rep.append("\n### 七、净分S₀代理 vs 实际（验证评分架构本身）\n")
    rep.append("| 净分桶 | n | 领先方胜率 | 说明 |")
    rep.append("|---|---|---|---|")
    for lo, hi, note in [(-99, -4, "强DOWN票池"), (-3, -1, "弱DOWN"), (0, 0, "零票/对冲"), (1, 3, "弱UP"), (4, 99, "强UP票池")]:
        sub = [(s, r) for s, r in score_rows if lo <= s <= hi]
        if not sub: continue
        wins = sum(1 for s, r in sub if (r["actual"] == "UP" and s > 0) or (r["actual"] == "DOWN" and s < 0))
        nn = len(sub)
        wr = wins / nn * 100
        rep.append(f"| {lo if lo>-99 else '≤'}{'~' if lo!=-99 else ''}{hi if hi<99 else '≥'} ({note}) | {nn} | {wr:.1f}% | {'有效' if nn>=15 and wr>=55 else ('反向!' if nn>=15 and wr<=45 else '噪音/样本不足')} |")

    rep.append("\n---\n**诚实声明**：① 全部为样本内统计，未做 Bonferroni 多重比较校正（约 27 因子×12 切片，纯随机也会有假阳性）；② 「朴素EV」按快照隐含价近似成交价，与真实执行有偏差；③ 结论仅供第五步权重设计参考，本轮不落地任何规则变更。")

    text = "\n".join(rep)
    out_path = os.path.join(HERMES_DIR, "docs", "factor_hitrate_study_20260824.md")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    open(out_path, "w").write(text)
    print(text[:3000])
    print(f"\n...\n完整报告已写入: {out_path}")

if __name__ == "__main__":
    main()
