#!/usr/bin/env python3
"""处理型 alpha 三合一检验：pm_delta 过度反应 / dm 尾部 EV / 时段季节性（ROADMAP P0.3）。

消费 data/ml_shadow_predictions.jsonl 全量（所有 dm），结算后做三个假设检验：
  A. pm_delta 过度反应（fade the move）：假设零售在小时内大幅压价/拉升后存在过度反应，
     「买被压价的一侧」应有正 EV。检验：按 |pm_delta| 分桶，对比「逆势买入」的 paper PnL
     与实际胜率 vs 决策点市价 pm_up_dec。
  B. dm 尾部动量：假设临近结算的决策点（dm50/55）做市商来不及完全消化新信息，
     尾部 EV 应好于早期。检验：分 dm 的 EV 结算 PnL（p_model 方向 × 当时 PM 价）。
  C. 时段季节性：分 ET 小时的上涨率 + 模型 EV，找系统性定价偏差的时段。

结算：复用 cross_validate_shadow.settle（Binance 镜像 1h K线 + tmp/klines_cache.json 缓存）。

解读纪律：n<100 的分组只看趋势不下结论；本脚本是探索性的，结论须待样本积累复核。

用法（生产机）：
    .venv/Scripts/python model/alpha_processing_test.py
    .venv/Scripts/python model/alpha_processing_test.py --fade-thr 0.15
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cross_validate_shadow import load_cache, save_cache, settle  # noqa: E402

SHADOW_DEFAULT = ROOT / "data" / "ml_shadow_predictions.jsonl"
OUT_DEFAULT = ROOT / "model" / "data" / "alpha_processing_report.json"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def load_all_dm(path: Path) -> dict[tuple[str, int], dict]:
    """全 dm 加载：同 (slug, dm) 去重，优先保留带 pm 价的实时行。"""
    uniq: dict[tuple[str, int], dict] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("anchor_price") is None:
            continue
        key = (r.get("slug"), r.get("decision_min"))
        cur = uniq.get(key)
        if cur is None:
            uniq[key] = r
            continue
        # 评分：实时行(hours_ago=0) + 带 pm_up + 带 pm_up_dec 优先
        def score(x):
            return (not x.get("hours_ago"),
                    x.get("pm_up") is not None,
                    x.get("pm_up_dec") is not None)
        if score(r) > score(cur):
            uniq[key] = r
    return uniq


def main() -> int:
    ap = argparse.ArgumentParser(description="处理型 alpha 三合一检验")
    ap.add_argument("--shadow", default=str(SHADOW_DEFAULT))
    ap.add_argument("--dm", type=int, default=35,
                    help="小时级独立口径的锚定决策点（默认 35）")
    ap.add_argument("--fade-thr", type=float, default=0.10,
                    help="pm_delta 过度反应分组阈值（默认 0.10）")
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    args = ap.parse_args()

    rows_by_key = load_all_dm(Path(args.shadow))
    rows = sorted(rows_by_key.values(), key=lambda r: (r["anchor_time_utc"], r.get("decision_min") or 0))
    print(f"[alpha] 影子样本 {len(rows)} 条（全 dm，去重后）")

    cache = load_cache()
    labeled = []
    for r in rows:
        s = settle(r["slug"], r["anchor_time_utc"], r["anchor_price"], cache)
        if s is not None:
            labeled.append((r, s["label"]))
    save_cache(cache)
    print(f"[alpha] 结算成功 {len(labeled)} / {len(rows)}")
    if not labeled:
        return 1

    rep = {"generated_at": datetime.now(timezone.utc).isoformat(), "n_total": len(labeled)}

    # ================= A. pm_delta 过度反应 =================
    # 逆势买入(fade)：pm_delta<0 → UP 被压价 → 买 UP；pm_delta>0 → 买 DOWN
    # 顺势买入(follow)：反向，作对照组
    # 注意：同小时不同 dm 的行共享标签（相关样本），统计推断必须用小时级独立口径
    fade_rows = [(r, lb) for r, lb in labeled
                 if r.get("pm_delta") is not None and r.get("pm_up_dec") is not None]
    # 小时级独立样本：每 slug 取一条（优先指定 dm，其次最近的可用 dm）
    hour_best: dict[str, dict] = {}
    for r, lb in fade_rows:
        cur = hour_best.get(r["slug"])
        if cur is None:
            hour_best[r["slug"]] = {"r": r, "lb": lb}
            continue
        if abs((r.get("decision_min") or 0) - args.dm) < \
           abs((cur["r"].get("decision_min") or 0) - args.dm):
            hour_best[r["slug"]] = {"r": r, "lb": lb}
    hour_rows = [(v["r"], v["lb"]) for v in hour_best.values()]
    rep["A_pm_delta"] = {"n_all_rows": len(fade_rows), "n_hours": len(hour_rows),
                         "fade_thr": args.fade_thr}
    print(f"\n===== A. pm_delta 过度反应（全行 {len(fade_rows)} / 独立小时 {len(hour_rows)}）=====")

    def fade_follow_stats(sub):
        """返回 (n, 均价, 逆势均EV, 逆势PnL, 顺势PnL)。"""
        fade_pnl = follow_pnl = price_sum = 0.0
        for r, lb in sub:
            fade_side_up = r["pm_delta"] < 0  # UP 被压 → fade 买 UP
            price = r["pm_up_dec"] if fade_side_up else 1 - r["pm_up_dec"]
            won = (lb == 1) == fade_side_up
            fade_pnl += (1 - price) if won else -price
            fprice = (1 - r["pm_up_dec"]) if fade_side_up else r["pm_up_dec"]
            fwon = (lb == 1) != fade_side_up
            follow_pnl += (1 - fprice) if fwon else -fprice
            price_sum += price
        n = len(sub)
        return n, price_sum / n, fade_pnl / n, fade_pnl, follow_pnl

    if len(hour_rows) >= 20:
        buckets = [(0.0, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 1.01)]
        print(f"  [小时级独立口径]（每 slug 一条，最近 {args.dm} 分）")
        print(f"  {'|pm_delta|':<12}{'n':>4}{'市价':>7}{'逆势均EV':>9}{'逆势PnL':>9}{'顺势PnL':>9}")
        for lo, hi in buckets:
            sub = [(r, lb) for r, lb in hour_rows if lo <= abs(r["pm_delta"]) < hi]
            if not sub:
                continue
            n, mp, fev, fp, fo = fade_follow_stats(sub)
            rep["A_pm_delta"][f"hour_bucket_{lo:.2f}_{hi:.2f}"] = {
                "n": n, "mean_price": round(mp, 4), "fade_ev": round(fev, 4),
                "fade_pnl": round(fp, 2), "follow_pnl": round(fo, 2)}
            print(f"  {lo:.2f}-{hi:.2f}{'':<4}{n:>4}{mp:>7.3f}{fev:>+9.3f}{fp:>+9.2f}{fo:>+9.2f}")
        # 大幅移动整体（超过阈值，小时级）
        big = [(r, lb) for r, lb in hour_rows if abs(r["pm_delta"]) >= args.fade_thr]
        if big:
            n, mp, fev, fp, fo = fade_follow_stats(big)
            rep["A_pm_delta"]["hour_big_move"] = {
                "n": n, "fade_thr": args.fade_thr, "fade_pnl": round(fp, 2),
                "follow_pnl": round(fo, 2)}
            print(f"  大幅移动(≥{args.fade_thr}): n={n} 逆势PnL={fp:+.2f} 顺势PnL={fo:+.2f}")
    else:
        print(f"  [!] 独立小时 n={len(hour_rows)} < 20，样本不足，只记录不下结论")

    # ================= B. dm 尾部 EV =================
    print(f"\n===== B. dm 尾部 EV（p_model 方向 × 当时 PM 价）=====")
    by_dm = defaultdict(list)
    for r, lb in labeled:
        side = "UP" if (r.get("direction_model") or r.get("direction") or "UP") == "UP" else "DOWN"
        pm = r.get("pm_up") if side == "UP" else r.get("pm_down")
        if pm is None:
            continue
        won = (side == "UP" and lb == 1) or (side == "DOWN" and lb == 0)
        pnl = (1 - pm) if won else -pm
        ev = r.get("ev_up") if side == "UP" else r.get("ev_down")
        by_dm[r.get("decision_min")].append({"pnl": pnl, "ev_pos": ev is not None and ev > 0})
    print(f"  {'dm':>4}{'n':>5}{'全PnL':>9}{'均PnL':>8}{'正EV PnL':>10}")
    dm_stats = {}
    for dm in sorted(by_dm):
        v = by_dm[dm]
        pnl_all = sum(x["pnl"] for x in v)
        pnl_ev = sum(x["pnl"] for x in v if x["ev_pos"])
        dm_stats[dm] = {"n": len(v), "pnl_all": round(pnl_all, 2),
                        "pnl_avg": round(pnl_all / len(v), 4) if v else None,
                        "pnl_ev_only": round(pnl_ev, 2)}
        print(f"  {dm:>4}{len(v):>5}{pnl_all:>+9.2f}{pnl_all/len(v):>+8.3f}{pnl_ev:>+10.2f}")
    late = [x for dm in (50, 55) for x in by_dm.get(dm, [])]
    early = [x for dm in range(0, 50, 5) for x in by_dm.get(dm, [])]
    if late and early:
        late_avg = sum(x["pnl"] for x in late) / len(late)
        early_avg = sum(x["pnl"] for x in early) / len(early)
        rep["B_dm_tail"] = {"by_dm": dm_stats,
                            "late_avg_pnl": round(late_avg, 4),
                            "early_avg_pnl": round(early_avg, 4),
                            "n_late": len(late), "n_early": len(early)}
        print(f"  → 尾部(dm50/55) 均PnL={late_avg:+.4f} vs 早期(dm0-45) 均PnL={early_avg:+.4f}")
        if len(late) < 100:
            print(f"  [!] 尾部 n={len(late)} < 100，只看趋势不下结论")
    else:
        rep["B_dm_tail"] = {"by_dm": dm_stats}

    # ================= C. 时段季节性 =================
    # 按 slug 去重（同小时不同 dm 共享标签，不能重复计数）
    print(f"\n===== C. 时段季节性（按 ET 小时，独立小时口径）=====")
    hour_label: dict[str, int] = {}   # slug -> label（每个小时一条）
    slug_et_hour: dict[str, int] = {}  # slug -> ET 小时
    for r, lb in labeled:
        hour_label.setdefault(r["slug"], lb)
        slug_et_hour.setdefault(r["slug"], (int(r["anchor_time_utc"][11:13]) - 4) % 24)
    by_hour = defaultdict(list)
    for slug, lb in hour_label.items():
        by_hour[slug_et_hour[slug]].append(lb)
    print(f"  {'ET时':>5}{'n':>5}{'上涨率':>8}")
    hour_stats = {}
    for h in sorted(by_hour):
        v = by_hour[h]
        up = sum(v) / len(v)
        hour_stats[h] = {"n": len(v), "up_rate": round(up, 4)}
        print(f"  {h:>5}{len(v):>5}{up:>8.3f}")
    rep["C_hour_seasonality"] = hour_stats

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[alpha] 报告 -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
