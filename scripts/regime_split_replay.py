#!/usr/bin/env python3
"""
四维胜率表官方校准工具（round35 起，学习系统统计层执行器）。

职责（只读分析，不改引擎、不写信号、selftest 零影响）：
1. 四维全拆分实测：(|净分|档 × 贴线 × PM顺逆 × Regime) 分组统计实际胜率 + Wilson95 下界
2. 时间切分检查：每格输出前半/后半胜率，区分"稀释"（小样本运气摊平）与"恶化"（机制变化）
3. 漂移告警：与引擎 rule_engine._probability 现行值逐格比对（按引擎回退逻辑取值），
   偏差 >10pp 的格子追加写入 data/drift_alerts.jsonl（供监控环消费）
4. 教训反事实（保留）：5pm-et 2026-08-20 两教训的历史反事实检查

触发条件（docs/hermes_learning_architecture.md §9 阶段1 合约）：
- 新增 ≥50 场已结算，或每周日先到（当前手工触发；阶段 3 接 automation）
- 消费方式：当前人工每周日查 drift_alerts.jsonl；连续 4 周未消费 → 阶段 3 提案强制接 automation

统计纪律（架构文档 §5）：
- Wilson score 95%（非 Wald 正态近似）；Wilson 数字必须标明归属格子，禁止跨格挪用
- 全部格子 n<30 时仅告警不落表（护栏2）；合并口径 n≥30 才可提议落表
- 落表须走完整 SYNC_MAP 链（round35 先例）+ 30 场监控窗

用法：python3 scripts/regime_split_replay.py [--no-alert]
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from replay_common import (
    load_results_actual as load_results,
    get_latest_snapshots,
    wilson,
    engine_current,
    replay_judge_full,
    ALERTS_FILE,
)

DRIFT_THRESHOLD_PP = 10.0  # 漂移告警阈值（百分点）

# 注：engine_current / load_results / get_latest_snapshots / wilson 已迁至 scripts/replay_common.py
# 改引擎表必须同步 replay_common.engine_current（SYNC_MAP #11）。

def bucket_net(net: float) -> str:
    an = abs(net)
    if an >= 6:
        return "≥6"
    if an >= 3:
        return "3-5"
    return "0-2"


def main() -> int:
    no_alert = "--no-alert" in sys.argv
    results = load_results()
    slugs = get_latest_snapshots()

    records: list[dict] = []

    for slug, path in sorted(slugs.items()):
        actual = results.get(slug)
        if not actual:
            continue
        try:
            md = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        replay = replay_judge_full(md)
        if replay is None:
            continue
        d, factors, inp = replay

        tie_mode = bool(inp.get("_tie_mode"))
        regime = inp.get("regime") or "?"
        pm_up = inp.get("pm_up")
        net_dir = d.net_direction or d.final_direction
        pm_dir = "UP" if (pm_up is not None and pm_up > 50) else ("DOWN" if pm_up is not None else None)
        pm_same: bool | None = (net_dir == pm_dir) if pm_dir else None
        if pm_same is None:
            continue  # PM 缺失不入四维分组

        records.append({
            "slug": slug,
            "net": d.net,
            "net_bucket": bucket_net(d.net),
            "tie_mode": tie_mode,
            "pm_same": pm_same,
            "regime": regime,
            "direction": d.final_direction,
            "actual": actual,
            "win": d.final_direction == actual,
            "macd_1h_hist": inp.get("macd_1h_hist"),
            "macd_4h_hist": inp.get("macd_4h_hist"),
            "dev_pct": float(inp.get("deviation_pct") or 0),
            "order_flow": factors.get("order_flow", 0),
        })

    total = len(records)
    total_wins = sum(1 for r in records if r["win"])
    print("# 四维 Regime 拆分重放（官方校准工具，round35）\n")
    print(f"- 样本：{total} 场（快照×已结算匹配，PM 缺失已剔除四维分组）")
    print(f"- 全量胜率：{total_wins}/{total} = {total_wins / total * 100:.1f}%")
    print(f"- 触发合约：新增≥50场已结算或每周日（架构文档 §9）；本次运行 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n")

    # ================= 四维全拆分 + 时间切分 + 漂移比对 =================
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in records:
        groups[(r["net_bucket"], r["tie_mode"], r["pm_same"], r["regime"])].append(r)

    print("## 四维全拆分实测（|净分|档 × 贴线 × PM顺逆 × Regime）\n")
    print("| 净分档 | 贴线 | PM | Regime | n | 胜 | 胜率 | Wilson95区间 | 引擎现值 | 偏差 | 前半→后半 | n<30 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")

    alerts: list[dict] = []
    for bucket in ["≥6", "3-5", "0-2"]:
        for tie in [False, True]:
            for same in [True, False]:
                for regime_ in ["摆动市", "趋势市"]:
                    g = groups.get((bucket, tie, same, regime_), [])
                    n = len(g)
                    if n == 0:
                        continue
                    w = sum(1 for r in g if r["win"])
                    rate = w / n * 100
                    wl, wu = wilson(w, n)
                    eng = engine_current(bucket, tie, same, regime_)
                    drift = rate - eng
                    # 时间切分（按 slug 排序≈时间序）
                    g_sorted = sorted(g, key=lambda r: r["slug"])
                    half = max(1, n // 2)
                    seg1 = g_sorted[:half]
                    seg2 = g_sorted[half:]
                    r1 = sum(1 for x in seg1 if x["win"]) / len(seg1) * 100 if seg1 else 0
                    r2 = sum(1 for x in seg2 if x["win"]) / len(seg2) * 100 if seg2 else 0
                    trend_mark = ""
                    if len(seg1) >= 5 and len(seg2) >= 5 and r1 - r2 >= 15:
                        trend_mark = " ⚠️恶化"
                    flag = "⚠️" if n < 30 else ""
                    drift_mark = f"{drift:+.1f}" + (" 🚨" if abs(drift) > DRIFT_THRESHOLD_PP else "")
                    print(f"| {bucket} | {'贴线' if tie else '非贴线'} | {'顺' if same else '逆'} | {regime_} "
                          f"| {n} | {w} | {rate:.1f}% | [{wl:.1f}, {wu:.1f}] | {eng:.1f} | {drift_mark} "
                          f"| {r1:.0f}%→{r2:.0f}%{trend_mark} | {flag} |")

                    if abs(drift) > DRIFT_THRESHOLD_PP:
                        alerts.append({
                            "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "cell": f"|净分|{bucket} × {'贴线' if tie else '非贴线'} × "
                                    f"{'顺PM' if same else '逆PM'} × {regime_}",
                            "n": n,
                            "win_rate": round(rate, 1),
                            "engine_current": eng,
                            "drift_pp": round(drift, 1),
                            "wilson95": [round(wl, 1), round(wu, 1)],
                            "time_split": [round(r1, 1), round(r2, 1)],
                            "split_verdict": ("恶化" if trend_mark else ("稀释或正常" if n >= 30 else "n<30仅告警")),
                        })

    # ================= 漂移告警落盘 =================
    print(f"\n## 漂移告警（|偏差|>{DRIFT_THRESHOLD_PP:.0f}pp，追加写入 {ALERTS_FILE.name}）\n")
    if alerts:
        for a in alerts:
            print(f"- 🚨 {a['cell']}: 实测 {a['win_rate']}% vs 引擎 {a['engine_current']}% "
                  f"(偏差 {a['drift_pp']:+}pp, n={a['n']}, 切分 {a['time_split']}, {a['split_verdict']})")
        if not no_alert:
            with ALERTS_FILE.open("a", encoding="utf-8") as f:
                for a in alerts:
                    f.write(json.dumps(a, ensure_ascii=False) + "\n")
            print(f"\n已追加 {len(alerts)} 条 → {ALERTS_FILE}")
        else:
            print("\n(--no-alert：仅打印不落盘)")
    else:
        print("- 无 >10pp 漂移格")

    # 落表提醒（护栏2）
    big_n = [a for a in alerts if a["n"] >= 30]
    if big_n:
        print(f"\n⚠️ 落表候选：{len(big_n)} 个告警格 n≥30，可按 SYNC_MAP 链提议落表（round35 先例）；"
              f"n<30 告警格仅监控（护栏2）。")

    # ================= 教训反事实（历史检查，保留） =================
    print("\n## 反事实：5pm-et 教训1（摆动市+4H≥+700+1H≤-30+|dev|阈值 → 强翻 UP）\n")
    for ver_label, dev_max in [("严格 <0.05%", 0.05), ("宽松 <0.10%", 0.10)]:
        hits = [r for r in records
                if r["regime"] == "摆动市"
                and (r["macd_4h_hist"] or 0) >= 700
                and (r["macd_1h_hist"] or 0) <= -30
                and abs(r["dev_pct"]) < dev_max]
        down_hits = [r for r in hits if r["direction"] == "DOWN"]
        flip_win = sum(1 for r in down_hits if r["actual"] == "UP")
        flip_lose = sum(1 for r in down_hits if r["actual"] == "DOWN")
        print(f"- {ver_label}: 命中 {len(hits)} 场，其中引擎方向 DOWN（有翻向需求）{len(down_hits)} 场，"
              f"翻对 {flip_win} / 翻错 {flip_lose}")

    print("\n## 反事实：5pm-et 教训3（摆动市 order_flow ×2→×1）\n")
    swing_of2 = [r for r in records if r["regime"] == "摆动市" and abs(r["order_flow"]) == 2]
    print(f"- 触发 {len(swing_of2)} 场（历史结论：翻转 1 场且翻错，净效果 -1，已否决——见 "
          f"data/hermes_model_library_review_20260821.md）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
