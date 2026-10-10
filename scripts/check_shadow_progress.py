#!/usr/bin/env python3
"""round36 影子进度查询。跑法：python3 scripts/check_shadow_progress.py
依据:data/hermes_round36_review_20260821.md 预注册门槛。
"""
import json, math, os, sys
from pathlib import Path
from collections import defaultdict

DATA = Path(__file__).resolve().parent.parent / "data"
SHADOW = DATA / "hermes_shadow_edge.jsonl"
SNAPSHOTS = DATA / "snapshots"
DONE = DATA / "hermes_done.jsonl"

def wilson(w, n, z=1.96):
    if n == 0: return (0.0, 0.0)
    ph = w / n; d = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / d
    m = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / d
    return (max(0, c - m), min(1, c + m))

def settle(slug):
    """从相邻小时快照重建结算方向"""
    base = SNAPSHOTS
    files = sorted(base.glob(f"{slug}_*.json"))
    if not files: return None
    snap = json.loads(files[-1].read_text())
    anchor = snap.get("anchor_price")
    if anchor is None: return None
    # 找下一小时锚定
    import re
    from datetime import datetime, timedelta
    M = {**{m:i+1 for i,m in enumerate(
        ['january','february','march','april','may','june','july','august',
         'september','october','november','december'])}}
    m = re.match(r'bitcoin-up-or-down-(\w+)-(\d+)-(\d+)-(\d+)(am|pm)-et', slug)
    if not m: return None
    mon, day, yr, hr, ap = m.groups()
    h = int(hr) % 12 + (12 if ap == 'pm' else 0)
    cur = datetime(int(yr), M[mon], int(day), h)
    nxt_h = cur + timedelta(hours=1)
    nxt_slug = f"bitcoin-up-or-down-{list(M.keys())[nxt_h.month-1]}-{nxt_h.day}-{nxt_h.year}-{nxt_h.hour%12 or 12}{'pm' if nxt_h.hour>=12 else 'am'}-et"
    nxt_files = sorted(base.glob(f"{nxt_slug}_*.json"))
    if not nxt_files: return None
    nxt = json.loads(nxt_files[-1].read_text())
    return 'UP' if nxt['anchor_price'] > anchor else 'DOWN'

def main():
    if not SHADOW.exists():
        print(f"影子日志 {SHADOW} 还不存在（bot 还没产生新信号）。")
        return
    rows = []
    for line in SHADOW.read_text().splitlines():
        try: rows.append(json.loads(line))
        except: continue
    n = len(rows)
    print(f"=== round36 影子进度 ===  总样本 n={n}")
    print(f"文件: {SHADOW}")
    print()
    
    # 按时段
    by_cap = defaultdict(list)
    for r in rows:
        by_cap[r.get('cap_r1')].append(r)
    print(f"{'cap_r1':>8s} {'样本':>4s} {'说明':>20s}")
    for cap in [0.52, 0.60, 0.70]:
        v = by_cap.get(cap, [])
        note = {'0.52':'edge≥20pt 过热压制',
                '0.60':'默认（实际生效）',
                '0.70':'edge 8-20pt 温和领先区'}.get(str(cap),'?')
        print(f"{cap:>8.2f} {len(v):>4d} {note:>20s}")
    print()
    
    # 门槛检查
    print("=== 预注册门槛（SYNC_MAP §round36） ===")
    # R1a: edge 8-20pt, cap=0.70, n≥60
    a = [r for r in rows if 0.08 <= (r.get('edge') or -1) < 0.20]
    a_win = sum(1 for r in a if (s:=settle(r['slug'])) and s==r['side'])
    # R1b: edge≥20pt, cap=0.52, n≥30
    b = [r for r in rows if (r.get('edge') or -1) >= 0.20]
    b_win = sum(1 for r in b if (s:=settle(r['slug'])) and s==r['side'])
    
    def report(name, v, w, threshold_n):
        if not v: 
            print(f"  {name}: 0/{threshold_n}")
            return
        n_v = len(v)
        lo, hi = wilson(w, n_v)
        avg_pm = sum(r.get('pm_snap',0) for r in v) / n_v
        avg_wb = sum(r.get('would_be_price',0) or 0 for r in v) / n_v
        print(f"  {name}: n={n_v}/{threshold_n}  影子方向胜率 {w/n_v*100:.1f}% CI[{lo*100:.1f},{hi*100:.1f}]")
        print(f"    平均 PM {avg_pm:.3f}  平均 would-be 价 {avg_wb:.3f}  保本线 = avg_pm")
        if n_v >= threshold_n:
            print(f"    【已达门槛】判定 EV=胜率×(1/avg_wb-1) - (1-胜率) = {w/n_v*(1/avg_wb-1) - (1-w/n_v):+.3f} 美元/$1")
        else:
            print(f"    距门槛还差 {threshold_n - n_v} 单")
    report("R1a (edge 8-20pt → cap 0.70)", a, a_win, 60)
    report("R1b (edge≥20pt → cap 0.52) ", b, b_win, 30)

if __name__ == "__main__":
    main()
