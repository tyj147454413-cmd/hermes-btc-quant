#!/usr/bin/env python3
"""贴线模式优化方案反事实回测。

基线：92 场快照按当前规则(round21)重放。
变体：
  V1: F1 阈值 ≤-3 → ≤-4（剔除擦线场）
  V2: 贴线模式 wall_divergence 权重 ×2→×1
  V3: 贴线模式 wall_divergence 权重 ×2→0（不计分）
  V4: 贴线模式 taker_bs 权重 ×1→0（不计分）
  V5: 贴线模式 price_position 极端位也 ×2（恢复权重）
  V6: 组合（F1≤-4 + wall_div×1 + taker_bs×1）
比较：各变体在贴线模式(n=26)/常规(n=66)/全量(92)的初始方向与最终方向正确率。
"""
import os, json, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import signal_validator as sv
import rule_engine as re

SNAP = 'data/snapshots'
RES = [json.loads(l) for l in open('data/hermes_results.jsonl').read().strip().splitlines()]
RES = {d['slug']: d for d in RES if d['actual'] in ('UP','DOWN') and d['side'] in ('UP','DOWN')}

def slug(fn): return fn.rsplit('_', 2)[0]

def load_samples():
    by = {}
    for fn in os.listdir(SNAP):
        by.setdefault(slug(fn), []).append(fn)
    out = []
    for sl, fns in by.items():
        if sl not in RES:
            continue
        md = json.load(open(os.path.join(SNAP, sorted(fns)[-1])))
        try:
            factors, inp, notes, mode = sv.extract_factors(md)
        except Exception:
            continue
        out.append({'slug': sl, 'mode': mode, 'factors': factors, 'inp': inp,
                    'actual': RES[sl]['actual'].upper(), 'side': RES[sl]['side'].upper()})
    return out

def judge_with(s, f1_net=-3.0, wall_w=2, takerbs_w=1, pp_extreme_w=1):
    """用可调参数跑 judge，返回 (init_dir, final_dir, is_f1)。"""
    import copy
    factors = dict(s['factors'])
    inp = dict(s['inp'])
    inp['factors'] = factors
    inp['_tie_mode'] = bool('贴线' in s['mode'])
    # 调整权重
    mode = s['mode']
    if '贴线' in mode:
        if 'wall_divergence' in factors and factors['wall_divergence'] != 0:
            factors['wall_divergence'] = factors['wall_divergence'] / 2 * wall_w
        if 'taker_bs' in factors and factors['taker_bs'] != 0:
            factors['taker_bs'] = factors['taker_bs'] * takerbs_w  # 原 ×1
    # 覆盖 F1 阈值
    orig = re.CW_F1_DOWN_NET
    re.CW_F1_DOWN_NET = f1_net
    try:
        d = re.judge(inp)
    finally:
        re.CW_F1_DOWN_NET = orig
    is_f1 = any('翻转级' in r for r in d.risks)
    return d.net_direction, d.final_direction, is_f1, d.net

def report(samples, f1_net=-3.0, wall_w=2, takerbs_w=1, pp_extreme_w=1, label=''):
    """返回 {subset: (init_acc, final_acc, n)}"""
    out = {}
    for sub, lst in (('贴线', [s for s in samples if '贴线' in s['mode']]),
                     ('常规', [s for s in samples if '贴线' not in s['mode']]),
                     ('全量', samples)):
        if not lst:
            continue
        init_w = final_w = 0
        f1s = []
        for s in lst:
            init_dir, final_dir, is_f1, net = judge_with(s, f1_net, wall_w, takerbs_w, pp_extreme_w)
            if init_dir == s['actual']:
                init_w += 1
            if final_dir == s['actual']:
                final_w += 1
            if is_f1:
                f1s.append((net, s['slug'], final_dir, s['actual']))
        out[sub] = {
            'n': len(lst),
            'init': f'{init_w}/{len(lst)}={init_w/len(lst)*100:.1f}%',
            'final': f'{final_w}/{len(lst)}={final_w/len(lst)*100:.1f}%',
            'f1_count': len(f1s),
            'f1_correct': sum(1 for n,_,fd,act in f1s if fd==act),
        }
    return out

def show(samples, f1_net=-3.0, wall_w=2, takerbs_w=1, pp_extreme_w=1, label=''):
    print(f'--- {label} ---')
    r = report(samples, f1_net, wall_w, takerbs_w, pp_extreme_w, label)
    for sub in ('贴线', '常规', '全量'):
        if sub in r:
            x = r[sub]
            print(f'  {sub}(n={x["n"]}): 初始{x["init"]} 最终{x["final"]} | F1触发{x["f1_count"]}场 对{x["f1_correct"]}场')
    print()

if __name__ == '__main__':
    samples = load_samples()
    print(f'样本总数: {len(samples)}')
    show(samples, f1_net=-3.0, label='基线(round21: F1≤-3, wall×2, taker_bs×1)')
    show(samples, f1_net=-4.0, label='V1: F1阈值≤-4')
    show(samples, f1_net=-3.0, wall_w=1, label='V2: wall_div ×2→×1')
    show(samples, f1_net=-3.0, wall_w=0, label='V3: wall_div ×2→0')
    show(samples, f1_net=-3.0, takerbs_w=0, label='V4: taker_bs ×1→0')
    show(samples, f1_net=-4.0, wall_w=1, takerbs_w=1, label='V6组合: F1≤-4 + wall×1 + taker_bs×1')
