#!/usr/bin/env python3
"""分批提价回测：模拟把挂单上限从 0.60 提到 0.65/0.70 后的成交率、胜率与盈亏。

核心问题：用户想"分批提价到 0.60-0.70，宁可买贵换成交率"。
本脚本用历史数据回答：提价到底多赚/多亏？

两种口径（交叉验证）：
1. 【权威】真实成交价口径：用 done.jsonl 的实际 order_price + results actual，按成交价分桶
   算每档胜率 / 每$1 盈亏 / Wilson95 下界 vs 保本线。这最接近真实成交与结算。
2. 【敏感性】分析时刻 pm 价口径：用快照 polymarket 价模拟"若上限 P 能否成交"，
   看提价新增成交（0.60 不成交→0.70 成交）的方向正确率。

盈亏口径（Polymarket 二元）：每 $1 投入，方向对 → +(1-买入价)；方向错 → -1。

方法铁律：分桶胜率配 Wilson95 下界 vs 保本线；n<30 不作决策依据。

用法：python3 scripts/batch_order_review_replay.py
"""
import os, json, glob, math, sys

BASE = os.path.join(os.path.dirname(__file__), '..')
SNAP = os.path.join(BASE, 'data', 'snapshots')
DONE = os.path.join(BASE, 'data', 'hermes_done.jsonl')
RES  = os.path.join(BASE, 'data', 'hermes_results.jsonl')


def wilson95(win, n):
    if n == 0:
        return 0.0
    p = win / n
    z = 1.95996
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return center - margin


def load_results():
    actual = {}
    for line in open(RES):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get('actual') in ('UP', 'DOWN'):
            actual[d['slug']] = d['actual']
    return actual


def load_real_trades():
    """权威口径：真实成交（done order_price）+ 结算 actual 分桶。"""
    actual = load_results()
    trades = []  # (buy_price, ok)
    for line in open(DONE):
        try:
            d = json.loads(line)
        except Exception:
            continue
        sig, r = d.get('signal', {}), d.get('result', {})
        sl, side = sig.get('slug', ''), sig.get('side', '')
        op = r.get('order_price')
        if not r.get('ok') or op is None or side not in ('UP', 'DOWN'):
            continue
        if sl not in actual:
            continue
        trades.append({'buy': op, 'ok': side == actual[sl]})
    return trades


def load_pm_samples():
    """敏感性口径：快照 pm 价 + done 信号 side + 结算。"""
    actual = load_results()
    snap = {}
    for fn in glob.glob(os.path.join(SNAP, '*.json')):
        sl = os.path.basename(fn).rsplit('_', 2)[0]
        try:
            d = json.load(open(fn))
        except Exception:
            continue
        pm = d.get('polymarket', {})
        if pm.get('up_price') is not None and pm.get('down_price') is not None:
            snap[sl] = {'up': float(pm['up_price']), 'down': float(pm['down_price'])}
    samples = []
    for line in open(DONE):
        try:
            d = json.loads(line)
        except Exception:
            continue
        sig, r = d.get('signal', {}), d.get('result', {})
        sl, side = sig.get('slug', ''), sig.get('side', '')
        if not r.get('ok') or side not in ('UP', 'DOWN'):
            continue
        if sl not in snap or sl not in actual:
            continue
        pm = snap[sl]['down'] if side == 'DOWN' else snap[sl]['up']
        samples.append({'slug': sl, 'side': side, 'pm': pm,
                        'ok': side == actual[sl], 'buy': sig.get('max_price', 0.60)})
    return samples


def report_buckets(trades):
    """真实成交价分桶。"""
    print('\n=== [权威] 真实成交价分桶 ===')
    print(f'{"成交价":<10}{"n":<5}{"胜率":<9}{"Wilson":<9}{"每$1盈亏":<9}{"保本":<6}{"总P&L"}')
    buckets = {}
    for t in trades:
        buckets.setdefault(round(t['buy'] * 20) / 20, []).append(t)
    tot = 0.0
    best = None
    for k in sorted(buckets):
        lst = buckets[k]
        w = sum(1 for t in lst if t['ok'])
        n = len(lst)
        pnl = sum((1 - t['buy']) if t['ok'] else -1 for t in lst)
        tot += pnl
        print(f"{k:<12}{n:<5}{w/n*100:<9.1f}{wilson95(w,n)*100:<9.1f}{pnl/n:<+9.3f}{k*100:.0f}%  {pnl:+.2f}")
        if best is None or pnl/n > best[1]:
            best = (k, pnl/n, n)
    print(f'TOTAL P&L: {tot:+.2f}')
    print(f'\n每$1盈亏最优档：{best[0]}（{best[1]:+.3f}/$，n={best[2]}）')
    return best


def report_pm_tiers(samples):
    """pm 价口径：各上限累计可成交 + 提价新增成交。"""
    print('\n=== [敏感性] 快照 pm 价口径 ===')
    print('--- 各档上限（累计可成交）---')
    for P in [0.60, 0.65, 0.70]:
        traded = [s for s in samples if s['pm'] <= P]
        w = sum(1 for s in traded if s['ok'])
        n = len(traded)
        pnl = sum((1 - s['buy']) if s['ok'] else -1 for s in traded)
        print(f'P={P}: 成交{n}/{len(samples)} 胜率{w/n*100 if n else 0:.1f}% Wilson{wilson95(w,n)*100 if n else 0:.1f}% '
              f'每$1 {pnl/n if n else 0:+.3f} 保本{P*100:.0f}%')
    print('--- 提价"新增成交"（lower 不成交→higher 成交）---')
    for lo, hi in [(0.60, 0.65), (0.65, 0.70), (0.60, 0.70)]:
        add = [s for s in samples if lo < s['pm'] <= hi]
        w = sum(1 for s in add if s['ok'])
        n = len(add)
        pnl = sum((1 - s['buy']) if s['ok'] else -1 for s in add)
        print(f'{lo}~{hi}: 新增{n} 胜率{w/n*100 if n else 0:.1f}% Wilson{wilson95(w,n)*100 if n else 0:.1f}% '
              f'每$1 {pnl/n if n else 0:+.3f} 保本{hi*100:.0f}%')


def main():
    trades = load_real_trades()
    print(f'真实成交样本: {len(trades)}')
    best = report_buckets(trades)

    samples = load_pm_samples()
    print(f'\npm 价样本: {len(samples)}')
    report_pm_tiers(samples)

    # 结论
    print('\n=== 判定 ===')
    if best and best[2] >= 30:
        k, pnl, n = best
        if pnl > 0:
            print(f'每$1盈亏最优档 {k} 为正（{pnl:+.3f}，n={n}）→ 该档可作为落地上限参考')
        else:
            print(f'每$1盈亏最优档 {k} 仍为负（{pnl:+.3f}，n={n}）→ 全部成交档均不盈利，维持 0.60 封顶')
    else:
        print('样本不足（n<30），不作决策依据，维持 0.60')


if __name__ == '__main__':
    main()
