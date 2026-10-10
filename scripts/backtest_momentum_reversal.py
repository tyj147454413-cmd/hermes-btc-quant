# -*- coding: utf-8 -*-
"""动量反转因子回测（2026-09-03 用户设计验证）。

用户设计（9/3 拍板）：
  策略B「翻转事件」：动量方向翻转（Δ 序列变号）= 信号本身，按翻转后新方向计 ±2
  策略C「贴线反转」：B + PM 贴线（up_cur 距 0.50 ≤0.05，代理"接近锚定价格"）→ 可信度最高档

对照：
  策略A「现行位移」：pm_momentum 现行口径——|Δ_cur|≥0.02 按 Δ_cur 方向计分（round49 ×2）
  附加对照：「逆翻转读」——翻转事件按旧方向（翻转前趋势）计分，检验翻转是延续还是诱饵

口径：
  - 信号时刻 = :35 快照（引擎 :35 生成信号）；缺 :35 退 :30
  - Δ = 相邻 5min 快照 up_cur 差（book-book 同源口径的代理）
  - ground truth = hermes_results.jsonl 的 actual（获胜方向）
  - 入场价 = 信号时刻该方向侧的 PM 价（up_cur / dn_cur）
  - EV 每$1 = wr×(1−p) − (1−wr)×p
  - 漏检率：翻转发生在信号时刻之后的场次占比（5min 粒度盲区量化）

纪律：只读 data/，不写任何真实数据文件；报告落 data/backtest_momentum_reversal_20260903.md
"""
import json
from collections import defaultdict
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
TIE_BAND = 0.05          # PM 贴线代理：up_cur ∈ [0.45,0.55]
MIN_MOVE = 0.02          # 现行 pm_momentum 触发阈值（round41.8.10 口径）
SIGNAL_MIN = 35          # 信号时刻（分钟）
REPORT = DATA / "backtest_momentum_reversal_20260903.md"


def load_pm_series(granularity: str = "5m"):
    """slug -> {minute: (up_cur, dn_cur)}，取每天最后一个窗口的同分钟值。
    granularity='5m'：pm_YYYYMM.jsonl（历史口径）；'1m'：pm_1m_YYYYMM.jsonl（9/3 新档）。"""
    series = defaultdict(dict)
    pat = "pm_2026*.jsonl" if granularity == "5m" else "pm_1m_2026*.jsonl"
    files = sorted(DATA.glob(f"pm_prices/{pat}"))
    if granularity == "1m" and not files:
        return series
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                slug, m = r.get("slug_cur"), r.get("slot_minute")
                if slug and m is not None:
                    series[slug][int(m)] = (float(r.get("up_cur") or 0),
                                            float(r.get("dn_cur") or 0))
    return series


def load_results():
    """slug -> {actual, side, buy_price}（仅已结算场）"""
    out = {}
    f = DATA / "hermes_results.jsonl"
    if not f.exists():
        return out
    with open(f, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            slug, actual = r.get("slug"), r.get("actual")
            if slug and actual in ("UP", "DOWN"):
                out[slug] = {"actual": actual,
                             "side": r.get("side"),
                             "buy_price": r.get("buy_price")}
    return out


def eval_side(prices, minute, direction):
    """信号时刻 direction 侧的入场价（无快照返回 None）"""
    if minute not in prices:
        return None
    up, dn = prices[minute]
    return up if direction == "UP" else dn


def stats_table(rows):
    """rows: [(won: bool|None, price: float)] -> dict(n, wr, ev)"""
    n = len(rows)
    if n == 0:
        return {"n": 0, "wr": None, "ev": None}
    wins = sum(1 for w, _ in rows if w)
    wr = wins / n
    ev = sum((wr * (1 - p) - (1 - wr) * p) for _, p in rows) / n
    return {"n": n, "wr": wr, "ev": ev}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--granularity", choices=["5m", "1m"], default="5m",
                    help="价格序列粒度：5m=历史口径；1m=9/3 新采集档")
    args = ap.parse_args()

    pm = load_pm_series(args.granularity)
    if args.granularity == "1m" and len(pm) < 10:
        print(f"⚠️ 1m 档覆盖不足 {len(pm)} 场（需 ≥10 场才有统计意义），"
              f"1min 采集 9/3 才上线，建议明天之后重跑。以下仍输出当前样本结果（仅供口径验证）。\n")
    res = load_results()
    slugs = [s for s in pm if s in res]
    buckets = defaultdict(list)   # name -> [(won, price)]
    detail = []                   # 每场明细（漏检分析用）
    v_shape = 0                   # 信号后才翻转的场次（盲区）
    v_shape_evaluable = 0

    # 1m 口径：Δ 用相邻快照差（粒度即段长）；5m 口径：相邻 5min 快照差
    seg_len = 1 if args.granularity == "1m" else 5

    for slug in slugs:
        prices = pm[slug]
        actual = res[slug]["actual"]
        mins = sorted(prices)
        # 信号时刻快照
        sig_m = SIGNAL_MIN if SIGNAL_MIN in prices else (30 if 30 in prices else None)
        if sig_m is None:
            continue
        # 信号时刻前相邻两段 Δ（需 ≥3 个快照点）
        prior = [m for m in mins if m <= sig_m]
        if len(prior) < 3:
            continue
        m2, m1, m0 = prior[-3], prior[-2], prior[-1]
        d_prev = prices[m1][0] - prices[m2][0]
        d_cur = prices[m0][0] - prices[m1][0]

        flip = (d_prev * d_cur < 0)                       # 变号 = 翻转事件
        flip_dir = ("UP" if d_cur > 0 else "DOWN") if d_cur != 0 else None
        old_dir = "UP" if d_prev > 0 else ("DOWN" if d_prev < 0 else None)
        disp_dir = flip_dir                                # 现行位移口径方向 = Δ_cur 方向
        tie = abs(prices[sig_m][0] - 0.50) <= TIE_BAND

        rec = {"slug": slug, "actual": actual, "sig_m": sig_m,
               "d_prev": round(d_prev, 3), "d_cur": round(d_cur, 3),
               "flip": flip, "tie": tie, "up_cur": prices[sig_m][0]}

        # 策略A 现行位移
        if disp_dir and abs(d_cur) >= MIN_MOVE:
            p = eval_side(prices, sig_m, disp_dir)
            if p:
                buckets["A_现行位移"].append((disp_dir == actual, p))
        # 策略B 翻转事件（新方向）
        if flip and flip_dir and abs(d_cur) >= MIN_MOVE:
            p = eval_side(prices, sig_m, flip_dir)
            if p:
                buckets["B_翻转新方向"].append((flip_dir == actual, p))
            # 逆翻转读（旧方向）
            p_old = eval_side(prices, sig_m, old_dir)
            if p_old and old_dir:
                buckets["X_逆翻转旧方向"].append((old_dir == actual, p_old))
            # 策略C 贴线反转
            if tie:
                buckets["C_贴线翻转"].append((flip_dir == actual, p))
            # 按翻转前后方向分层（诊断：翻转=延续还是诱饵）
            tag = f"B_flip_{old_dir}to{flip_dir}"
            buckets[tag].append((flip_dir == actual, p))
        # 无翻转时的位移对照（区分"趋势延续"与"翻转"两种市况下的现行口径表现）
        if not flip and disp_dir and abs(d_cur) >= MIN_MOVE:
            p = eval_side(prices, sig_m, disp_dir)
            if p:
                buckets["A2_无翻转纯趋势"].append((disp_dir == actual, p))

        # 漏检率：信号时刻价格位置与 actual 相反、但信号后价格走向 actual（V 型）
        # 代理判定：actual 侧在信号时刻处于劣势（up_cur<0.5 而 actual=UP，反之亦然）
        up_sig = prices[sig_m][0]
        behind = (actual == "UP" and up_sig < 0.5) or (actual == "DOWN" and up_sig > 0.5)
        if behind:
            v_shape_evaluable += 1
            # 信号后是否存在向 actual 方向的翻转（:35 之后 Δ 变号指向 actual）
            after = [m for m in mins if m > sig_m]
            flipped_after = False
            for i in range(1, len(after)):
                da = prices[after[i]][0] - prices[after[i - 1]][0]
                db = prices[after[i - 1]][0] - prices[after[i - 2]][0] if i >= 2 else d_cur
                if db * da < 0 and (("UP" if da > 0 else "DOWN") == actual):
                    flipped_after = True
                    break
            if flipped_after:
                v_shape += 1
        detail.append(rec)

    # —— 汇总 ——
    lines = ["# 动量反转因子回测报告（2026-09-03）", "",
             f"- 粒度口径：{args.granularity}（Δ 段长 = {seg_len} 分钟快照差）",
             f"- 样本：{len(detail)} 场（pm_prices 快照 × results 已结算，202608-202609）",
             f"- 信号时刻 :{SIGNAL_MIN}，贴线代理 |up_cur−0.50|≤{TIE_BAND}，现行触发阈值 |Δ|≥{MIN_MOVE}", "",
             "| 策略 | n | 胜率 | EV/每$1 |", "|---|---|---|---|"]
    order = ["A_现行位移", "A2_无翻转纯趋势", "B_翻转新方向", "X_逆翻转旧方向", "C_贴线翻转"]
    for name in order + sorted(k for k in buckets if k not in order):
        s = stats_table(buckets.get(name, []))
        if s["n"] == 0:
            continue
        lines.append(f"| {name} | {s['n']} | {s['wr']*100:.1f}% | {s['ev']:+.3f} |")

    if v_shape_evaluable:
        lines += ["", f"## 5min 粒度盲区（V 型漏检）", "",
                  f"- 信号时刻落后于最终获胜方：{v_shape_evaluable} 场",
                  f"- 其中信号后发生向获胜方翻转（信号时不可见）：{v_shape} 场"
                  f"（占落后场 {v_shape/v_shape_evaluable*100:.1f}%）",
                  "- 解读：这部分场次任何 :35 时点的因子都看不到，需 1min 级快照才能提前捕捉"]

    # 翻转方向迁移矩阵样本量提示
    lines += ["", "## 翻转迁移分层", ""]
    for k in sorted(buckets):
        if k.startswith("B_flip_"):
            s = stats_table(buckets[k])
            lines.append(f"- {k[7:]}: n={s['n']} wr={s['wr']*100:.1f}%" if s['n'] else f"- {k[7:]}: n=0")

    report = "\n".join(lines) + "\n"
    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write(report)
    print(report)
    print(f"报告已写入 {REPORT}")


if __name__ == "__main__":
    main()
