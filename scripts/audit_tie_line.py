#!/usr/bin/env python3
"""贴线专列设置合理性统计分析。

对 91 个可匹配结算的快照重放：
1. 贴线模式 vs 常规矩阵的正确率对比
2. 贴线专列核心因子（price_position / taker_bs / taker_1h / wall_divergence）的判别力
3. 各因子取值方向命中率（评估权重 ×2/×3 是否合理）
4. 前置门阈值：波动率 / 偏差分档 / 摆动市 的正确率
"""
import os, json, re, sys
from collections import defaultdict
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import signal_validator as sv

SNAP_DIR = 'data/snapshots'
RESULTS = [json.loads(l) for l in open('data/hermes_results.jsonl').read().strip().splitlines()]
RES = {d['slug']: d for d in RESULTS if d['actual'] in ('UP','DOWN') and d['side'] in ('UP','DOWN')}

def snap_slug(fn):
    return fn.rsplit('_', 2)[0]

def load_snaps():
    by = {}
    for fn in os.listdir(SNAP_DIR):
        by.setdefault(snap_slug(fn), []).append(fn)
    return by

def main():
    by = load_snaps()
    # 只用最新一份快照（:28 或该场最后一次）
    samples = []
    for slug, fns in by.items():
        if slug not in RES:
            continue
        fns_sorted = sorted(fns)
        fn = fns_sorted[-1]  # 最新
        try:
            md = json.load(open(os.path.join(SNAP_DIR, fn)))
            factors, inp, notes, mode = sv.extract_factors(md)
            r = RES[slug]
            samples.append({
                'slug': slug, 'mode': mode, 'factors': factors, 'inp': inp,
                'side': r['side'].upper(), 'actual': r['actual'].upper(),
                'buy_price': r['buy_price'],
            })
        except Exception:
            pass
    return samples

def hit(s):
    return s['side'] == s['actual']

def bucket_report(samples, key_fn, title):
    buckets = defaultdict(lambda: [0, 0])
    for s in samples:
        k = key_fn(s)
        buckets[k][0] += 1
        if hit(s):
            buckets[k][1] += 1
    print(f'\n=== {title} ===')
    for k in sorted(buckets, key=lambda x: str(x)):
        t, w = buckets[k]
        print(f'  {str(k):<30} {w}/{t} = {w/t*100:.1f}%')

if __name__ == '__main__':
    samples = main()
    print(f'有效重放样本: {len(samples)}')

    # 1. 贴线模式 vs 常规
    bucket_report(samples, lambda s: s['mode'], '按模式')

    # 2. 贴线模式下各核心因子取值命中率
    tie = [s for s in samples if '贴线' in s['mode']]
    print(f'\n>>> 贴线模式样本: {len(tie)}')

    # price_position 方向
    def pp_dir(s):
        v = s['factors'].get('price_position', 0)
        return 'price_pos=' + str(int(v))
    bucket_report(tie, pp_dir, '贴线: price_position 取值')

    def taker_bs_dir(s):
        v = s['factors'].get('taker_bs', 0)
        return 'taker_bs=' + str(int(v))
    bucket_report(tie, taker_bs_dir, '贴线: taker_bs 取值')

    def taker1h_dir(s):
        v = s['factors'].get('taker_1h', 0)
        return 'taker_1h=' + str(int(v))
    bucket_report(tie, taker1h_dir, '贴线: taker_1h 取值')

    def wall_dir(s):
        v = s['factors'].get('wall_divergence', 0)
        return 'wall_div=' + str(int(v))
    bucket_report(tie, wall_dir, '贴线: wall_divergence 取值')

    # 3. 偏差分档
    bucket_report(samples, lambda s: f"dev_abs={abs(s['inp'].get('deviation_pct',0)):.2f}%", '按|偏差|分档')

    # 4. 波动率分档
    bucket_report(samples, lambda s: f"vol={s['inp'].get('volatility_5m',0):.2f}%", '按波动率分档')

    # 5. 摆动市
    bucket_report(samples, lambda s: f"regime={s['inp'].get('regime','?')}", '按regime')

    # 6. 常规矩阵下的因子判别力（对比贴线）
    reg = [s for s in samples if '贴线' not in s['mode']]
    print(f'\n>>> 常规矩阵样本: {len(reg)}')
    bucket_report(reg, lambda s: 'macd_4h='+str(int(s['factors'].get('macd_4h',0))), '常规: macd_4h 取值')
    bucket_report(reg, lambda s: 'kline='+str(int(s['factors'].get('kline',0))), '常规: kline 取值')
