#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Regime-Switching 方案回测脚本 v3（修正版）
==========================================
数据源: data/hermes_results.jsonl（权威结算）+ data/hermes_done.jsonl（信号详情）

v3 修正（相对 v2）:
  1. [Bug2 前视偏差] _alt 方向交替改用 actual[i-1] != actual[i-2]（决策时点已知数据），
     不再使用 actual[i]（本小时结果，信号时刻未知）
  2. [Bug4 数据缺口] 相邻结算小时 ts 间隔 >65min 视为缺口：jump=None 并单独计数，
     杜绝跨多小时跳变按小时级阈值误判
  3. [Bug3 摆动市定义] 主指标改为近3小时方向变号次数(_flips, 衡量"反复")，
     锚定跳变幅度降为辅助（跳变需叠加交替才判摆动）
  4. [新增] EV 分析: 方向胜率 + EV(胜率-均价) 双指标，胜率≠盈亏
  5. [新增] always-UP 基准率对照
  6. [新增] n<30 子组统一标注"观察级"，不立铁律
  7. [新增] walk-forward: 前70%定规则 / 后30%样本外验证

验证假设:
  H1 Regime 识别: 新旧两种摆动市定义的分组胜率差（目标差>10pp）
  H2 位置效应:    key_levels 区间位置 >85% / <15% 时的实际方向分布
                  （注意: 此代理≠24h分位，结论仅作参考）
  H3 反手改判:    连续≥3小时实际同向 → 下一小时反向胜率（目标>55%）
  H4 PM共识规则:  PM≥60% 时顺向 vs 反向的胜率与 EV

用法: python3 scripts/backtest_regime.py
"""
import json
import re
from datetime import datetime

DATA_DIR = '/Users/tangyijun/Desktop/hermes btc/data'
JUMP_THR = 0.0012   # 锚定跳变 0.12%
PM_THR = 0.60       # PM 共识阈值
GAP_SEC = 65 * 60   # 相邻结算小时允许最大间隔（超出视为缺口）
MIN_N = 30          # 子组最小样本门槛，低于此标注"观察级"


def load_jsonl(path):
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def parse_ts(s):
    """尽力解析 ts 为 epoch 秒；失败返回 None"""
    if not s:
        return None
    if isinstance(s, (int, float)):
        return float(s) / 1000 if s > 1e12 else float(s)
    try:
        return datetime.fromisoformat(str(s).replace('Z', '+00:00')).timestamp()
    except Exception:
        return None


def pct(x):
    return f'{x*100:.1f}%' if x is not None else 'N/A'


def tag(cnt):
    """样本量标注：不足 MIN_N 一律观察级"""
    return '' if cnt >= MIN_N else '（观察级 n<%d）' % MIN_N


def win_rate(grp):
    if not grp:
        return None, 0
    c = sum(1 for r in grp if r['side'] == r['actual'])
    return c / len(grp), len(grp)


def main():
    results = load_jsonl(f'{DATA_DIR}/hermes_results.jsonl')
    done = load_jsonl(f'{DATA_DIR}/hermes_done.jsonl')
    sig = {}
    for d in done:
        s = d.get('signal', {})
        if s.get('slug'):
            sig[s['slug']] = s

    results.sort(key=lambda r: r.get('ts') or '')
    for r in results:
        r['sig'] = sig.get(r['slug'])
        r['_t'] = parse_ts(r.get('ts'))

    n = len(results)
    overall, _ = win_rate(results)
    up_base = sum(1 for r in results if r['actual'] == 'UP') / n
    print('=' * 76)
    print(f'样本: {n} 小时 ({results[0]["slug"][-22:]} ~ {results[-1]["slug"][-22:]})')
    print(f'模型总体胜率: {pct(overall)}')
    print(f'[基准率] 实际 UP 占比: {pct(up_base)}  (always-UP 基线, 所有胜率应与此对照)')
    print('=' * 76)

    # ---------- 缺口检测（Bug4 修复） ----------
    gap_cnt = 0
    for i in range(1, n):
        t0, t1 = results[i-1]['_t'], results[i]['_t']
        contiguous = (t0 is not None and t1 is not None and 0 < t1 - t0 <= GAP_SEC)
        results[i]['_contig'] = contiguous
        if not contiguous:
            gap_cnt += 1
    results[0]['_contig'] = False
    unparsed = sum(1 for r in results if r['_t'] is None)
    print(f'\n[数据质量] 相邻小时缺口: {gap_cnt} 处 | ts 无法解析: {unparsed} 条'
          f'（缺口处 jump/alt/flips 均不计）')

    # ---------- 锚定跳变（含 gap 检查） ----------
    prev_a = None
    for r in results:
        a = (r['sig'] or {}).get('anchor_price')
        a = float(a) if a else None
        if a and prev_a and r['_contig']:
            r['_jump'] = abs(a - prev_a) / prev_a
        else:
            r['_jump'] = None
        r['_anchor'] = a
        prev_a = a

    # ---------- 方向交替（Bug2 修复：只用决策时点已知数据） ----------
    # _alt[i]: 前两小时方向是否交替（actual[i-1] vs actual[i-2]，信号时刻已知）
    for i in range(n):
        if i >= 2 and results[i]['_contig'] and results[i-1]['_contig']:
            results[i]['_alt'] = (results[i-1]['actual'] != results[i-2]['actual'])
        else:
            results[i]['_alt'] = None

    # ---------- 方向变号次数（Bug3 修复：摆动市主指标） ----------
    # _flips[i]: 近3个已知小时(actual[i-3..i-1])的方向变号次数 0-2，要求4小时连续
    for i in range(n):
        if (i >= 3 and results[i]['_contig'] and results[i-1]['_contig']
                and results[i-2]['_contig']):
            seq = [results[i-3]['actual'], results[i-2]['actual'], results[i-1]['actual']]
            results[i]['_flips'] = sum(1 for a, b in zip(seq, seq[1:]) if a != b)
        else:
            results[i]['_flips'] = None

    # ---------- 摆动市两种定义 ----------
    def is_swing_old(r):
        return r['_jump'] is not None and r['_jump'] >= JUMP_THR

    def is_swing_new(r):
        # 主指标: 近3小时方向全交替(变号2次) → 明确的反复摆动
        if r['_flips'] is not None and r['_flips'] >= 2:
            return True
        # 辅助: 跳变有效且超阈值 且 前两小时有交替（幅度+反复双重确认）
        if (r['_jump'] is not None and r['_jump'] >= JUMP_THR
                and r['_alt'] is True):
            return True
        return False

    def is_trend_new(r):
        # 只统计判定了 regime 的样本（数据齐全）
        if r['_flips'] is None and r['_jump'] is None:
            return False
        return not is_swing_new(r)

    # ========== H1: Regime 识别（新旧定义对照） ==========
    print('\n【H1】Regime 识别：摆动市 vs 趋势市 分组胜率（新旧定义对照）')
    for name, pred in [('旧定义(跳变≥0.12%)', is_swing_old),
                       ('新定义(变号≥2 或 跳变+交替)', is_swing_new)]:
        sw = [r for r in results if pred(r)]
        if '旧定义' in name:
            tr = [r for r in results if r['_jump'] is not None and not pred(r)]
        else:
            tr = [r for r in results if is_trend_new(r)]
        w1, c1 = win_rate(sw)
        w2, c2 = win_rate(tr)
        print(f'  {name}:')
        print(f'    摆动市 {c1}样本 胜率 {pct(w1)}{tag(c1)} | 趋势市 {c2}样本 胜率 {pct(w2)}{tag(c2)}'
              + (f' | 差 {pct(w2-w1)}' if w1 is not None and w2 is not None else ''))

    # 连输窗口覆盖率（修正后定义，无前视）
    streaks, cur = [], []
    for r in results:
        if r['side'] != r['actual']:
            cur.append(r)
        else:
            if len(cur) >= 3:
                streaks.append(cur)
            cur = []
    if len(cur) >= 3:
        streaks.append(cur)
    st_total = sum(len(st) for st in streaks)
    cov_old = sum(1 for st in streaks for r in st if is_swing_old(r))
    cov_new = sum(1 for st in streaks for r in st if is_swing_new(r))
    print(f'  连输窗口(≥3连败): {len(streaks)}个 共{st_total}样本')
    print(f'  覆盖率: 旧定义 {cov_old}/{st_total}={pct(cov_old/st_total if st_total else None)}'
          f' | 新定义(无前视) {cov_new}/{st_total}={pct(cov_new/st_total if st_total else None)}')

    # ========== H2: 位置效应 ==========
    print('\n【H2】位置效应：key_levels 区间位置 pos=(cur-ss)/(sr-ss)')
    print('  ⚠️ 注意: 此代理为强弱支撑压力窄带位置，≠原方案的24h分位，结论仅作参考')
    pos_data = []
    for r in results:
        s = r.get('sig')
        if not s:
            continue
        kl = s.get('key_levels') or {}
        if isinstance(kl, str):
            try:
                m1 = re.search(r'强支撑(\d+\.?\d*)', kl)
                m2 = re.search(r'强压(\d+\.?\d*)', kl)
                kl = {'strong_support': float(m1.group(1)) if m1 else None,
                      'strong_resistance': float(m2.group(1)) if m2 else None}
            except Exception:
                kl = {}
        ss, sr, cur_p = kl.get('strong_support'), kl.get('strong_resistance'), s.get('current_price')
        if not all([ss, sr, cur_p]) or sr == ss:
            continue
        r['_pos'] = (cur_p - ss) / (sr - ss)
        pos_data.append(r)

    for label, lo, hi in [('顶部区(>85%)', 0.85, None), ('底部区(<15%)', None, 0.15),
                          ('中部区(15-85%)', 0.15, 0.85)]:
        grp = [r for r in pos_data if (lo is None or r['_pos'] >= lo) and (hi is None or r['_pos'] <= hi)]
        if not grp:
            continue
        wr, cnt = win_rate(grp)
        up_rate = sum(1 for r in grp if r['actual'] == 'UP') / len(grp)
        print(f'  {label}: {cnt}样本{tag(cnt)} | 模型胜率 {pct(wr)} | 实际UP率 {pct(up_rate)}'
              f' (基准{pct(up_base)})')

    # ========== H3: 反手改判 ==========
    print('\n【H3】反手改判：连续≥3小时实际同向 → 下一小时')
    print('  ⚠️ 注意: 顺/逆为同一统计量互补两面，非独立对照；重叠窗口自相关')
    actuals = [r['actual'] for r in results]
    rev_hit, rev_tot = 0, 0
    for i in range(3, n):
        if actuals[i-1] == actuals[i-2] == actuals[i-3]:
            pred_rev = 'UP' if actuals[i-1] == 'DOWN' else 'DOWN'
            rev_tot += 1
            rev_hit += 1 if actuals[i] == pred_rev else 0
    if rev_tot:
        print(f'  连续3同向→反向: {rev_hit}/{rev_tot} = {pct(rev_hit/rev_tot)}{tag(rev_tot)}')

    # ========== H4: PM 共识（胜率 + EV 双指标） ==========
    print('\n【H4】PM 共识 (≥60%)：顺向 vs 反向 —— 胜率 + EV')
    print('  EV 近似 = 胜率 − 均价（每份期望盈亏；顺共识价=共识侧pm价，逆共识价=对侧pm价）')

    def pm_info(r):
        s = r.get('sig')
        if not s:
            return None, None, None
        up, down = s.get('pm_up_price'), s.get('pm_down_price')
        if up is None or down is None:
            return None, None, None
        pm_dir = 'UP' if up >= PM_THR else ('DOWN' if down >= PM_THR else None)
        if pm_dir is None:
            return None, None, None
        same = (pm_dir == r['side'])
        entry = up if r['side'] == 'UP' else down  # 买入侧 pm 价作入场价代理
        return pm_dir, same, entry

    def ev_report(scope_name, grp):
        same_grp, rev_grp = [], []
        for r in grp:
            _, same, entry = pm_info(r)
            if same is True:
                same_grp.append((r, entry))
            elif same is False:
                rev_grp.append((r, entry))
        for label, g in [('顺共识', same_grp), ('逆共识', rev_grp)]:
            if not g:
                print(f'  {scope_name} {label}: 无样本')
                continue
            wins = sum(1 for r, _ in g if r['side'] == r['actual'])
            wr = wins / len(g)
            avg_px = sum(e for _, e in g) / len(g)
            ev = wr - avg_px
            print(f'  {scope_name} {label}: {len(g)}样本{tag(len(g))} | 胜率 {pct(wr)}'
                  f' | 均价 {avg_px:.3f} | EV {ev:+.3f}')

    swing_new = [r for r in results if is_swing_new(r)]
    trend_new = [r for r in results if is_trend_new(r)]
    for scope_name, grp in [('全样本', results), ('摆动市(新定义)', swing_new),
                            ('趋势市(新定义)', trend_new)]:
        ev_report(scope_name, grp)

    # ========== Walk-Forward 样本外验证 ==========
    print('\n【Walk-Forward】前70%定规则 → 后30%样本外验证')
    cut = int(n * 0.7)
    train, test = results[:cut], results[cut:]
    print(f'  训练段: {len(train)}样本 ({train[0]["slug"][-22:]}~{train[-1]["slug"][-22:]})')
    print(f'  验证段: {len(test)}样本 ({test[0]["slug"][-22:]}~{test[-1]["slug"][-22:]})')
    for name, grp in [('训练段', train), ('验证段', test)]:
        wr_all, c_all = win_rate(grp)
        sw = [r for r in grp if is_swing_new(r)]
        tr = [r for r in grp if is_trend_new(r)]
        w_sw, c_sw = win_rate(sw)
        w_tr, c_tr = win_rate(tr)
        same_grp = [r for r in grp if pm_info(r)[1] is True]
        rev_grp = [r for r in grp if pm_info(r)[1] is False]
        w_s, c_s = win_rate(same_grp)
        w_r, c_r = win_rate(rev_grp)
        print(f'  {name}: 总体 {pct(wr_all)}({c_all}) | 摆动 {pct(w_sw)}({c_sw}) vs 趋势 {pct(w_tr)}({c_tr})'
              f' | 顺共识 {pct(w_s)}({c_s}) vs 逆共识 {pct(w_r)}({c_r})')

    # ========== 结论判定 ==========
    print('\n' + '=' * 76)
    print('结论判定标准:')
    print('  H1: 新定义下摆动市胜率仍显著低于趋势市(>10pp) → regime识别有效')
    print('  H2: 代理口径已变更(窄带≠24h分位)，仅作参考不作规则依据')
    print('  H3: 反向胜率>55% 且 n≥30 → 反手改判有效，否则仅作辅助')
    print('  H4: 顺共识 胜率与EV 双优 → 顺共识规则成立；逆共识 n<30 一律观察级')
    print('  WF: 验证段仍保持 摆动<趋势 / 顺>逆 → 规则可落地，否则只出观察结论')
    print('=' * 76)


if __name__ == '__main__':
    main()
