#!/usr/bin/env python3
"""
G4/H9 价格位置回退（翻向）可靠度回测 — 诊断增强 2026-08-23（无规则语义变化，锚点仍 round41.3）

问题：当 |偏差|≥0.15% 且评分表 S₀ 方向与价格位置方向相反时，
      "翻向价格位置"（现行 G4 回退）是否比"保留 S₀ 方向"更可靠？

方法：
- 全量快照（data/snapshots/ 每 slug 取最新一份）经 replay_common.replay_judge_full
  引擎重放（两遍 extreme_wall，与实盘写信号链路同源），取 d.s0 / d.pm_row / d.risks。
- 结算 actual 取 data/hermes_results.jsonl（复盘权威口径）。
- 引擎实际触发 G4 的场次用 risks 中 "✅G4/H9 价格位置硬约束" 标记核对。

对比策略（仅在"G4 场景"子集：|dev|≥0.15% 且 S₀≠0 且 S₀ 方向 ≠ 价格位置方向）：
  A) 现行规则·翻向价格位置：方向 = price_dir
  B) 反事实·保留 S₀ 方向：  方向 = s0_dir
  C) 参考·跟 PM 共识：      方向 = pm_dir（pm_side = max(pm_up, 100-pm_up)）

分层：偏差桶 [0.15,0.30) / [0.30,0.60) / ≥0.60；|S₀| 档 1-2 / 3-5 / ≥6。
输出：data/hermes_g4_flip_reliability_20260823.md + stdout 摘要。
只读回测：不改信号队列、不写 drift_alerts。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from replay_common import (  # noqa: E402
    DATA_DIR,
    get_latest_snapshots,
    load_results_actual,
    replay_judge_full,
    wilson,
)

OUT_MD = DATA_DIR / "hermes_g4_flip_reliability_20260823.md"
DEV_TH = 0.15


def _bucket_dev(dev: float) -> str:
    if dev < 0.30:
        return "[0.15,0.30)"
    if dev < 0.60:
        return "[0.30,0.60)"
    return "≥0.60"


def _bucket_s0(abs_s0: float) -> str:
    if abs_s0 <= 2:
        return "|S0|1-2"
    if abs_s0 <= 5:
        return "|S0|3-5"
    return "|S0|≥6"


def _line(name: str, rows: list[dict]) -> str:
    """rows: list of dicts with keys dir(actual direction picked), ok(bool)."""
    n = len(rows)
    if n == 0:
        return f"| {name} | 0 | — | — | — |"
    w = sum(1 for r in rows if r["ok"])
    lo, hi = wilson(w, n)
    return f"| {name} | {n} | {w}/{n}={w/n*100:.1f}% | [{lo*100:.1f}%, {hi*100:.1f}%] | {sum(r.get('delta_b', 0) for r in rows):+d} |"


def main() -> int:
    actuals = load_results_actual()
    snaps = get_latest_snapshots()
    print(f"快照 {len(snaps)} 个 slug，results_actual {len(actuals)} 条")

    replayed, skipped = [], 0
    engine_g4_fired = 0
    cond_g4 = []  # 条件口径的 G4 场景
    all_dev15 = []  # 全部 |dev|≥0.15%（价格位置基准）

    for slug, path in sorted(snaps.items()):
        act = actuals.get(slug)
        if not act:
            continue
        try:
            md = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            skipped += 1
            continue
        rep = replay_judge_full(md)
        if rep is None:
            skipped += 1
            continue
        d, _factors, inp = rep
        dev = inp.get("deviation_pct")
        pm_up = inp.get("pm_up")
        if dev is None or pm_up is None:
            skipped += 1
            continue
        dev = abs(float(dev))
        price_dir = "UP" if float(dev) >= 0 and dev > 0 else ("UP" if float(dev) > 0 else "DOWN")
        # 注意 dev 已取绝对值前先记符号
        raw_dev = inp.get("deviation_pct")
        price_dir = "UP" if float(raw_dev) > 0 else "DOWN"
        s0 = float(d.s0 or 0.0)
        s0_dir = "UP" if s0 > 0 else ("DOWN" if s0 < 0 else "")
        pm_dir = "UP" if float(pm_up) >= 50 else "DOWN"
        pm_side = max(float(pm_up), 100.0 - float(pm_up))

        rec = {
            "slug": slug, "dev": dev, "raw_dev": float(raw_dev), "price_dir": price_dir,
            "s0": s0, "s0_dir": s0_dir, "pm_row": float(d.pm_row or 0.0),
            "pm_dir": pm_dir, "pm_side": pm_side,
            "final": d.final_direction, "actual": act,
            "engine_g4": any("G4/H9 价格位置硬约束" in r for r in d.risks),
        }
        replayed.append(rec)
        if rec["engine_g4"]:
            engine_g4_fired += 1
        if dev >= DEV_TH:
            all_dev15.append(rec)
            if s0_dir and s0_dir != price_dir:
                cond_g4.append(rec)

    def strat(rows, keyfn):
        out: dict[str, list] = {}
        for r in rows:
            out.setdefault(keyfn(r), []).append(r)
        return out

    def evaluate(rows, a_key, b_key):
        """返回 (A_rows, B_rows)：A=现行方向 a_key，B=反事实方向 b_key。"""
        A = [{"ok": r[a_key] == r["actual"]} for r in rows]
        B = [{"ok": r[b_key] == r["actual"]} for r in rows]
        return A, B

    # ===== 追加口径 P1/P2（8/23 回测补充）=====
    # P1 "G4 必须翻场"（引擎视角）：|dev|≥0.15% 且 净分方向(含PM行) 仍逆价格位置
    #    ——只有这类场引擎才会真正走 G4 回退（或已被 G2/G3 提前改向）。
    # P2 "PM 行翻转场"：S₀≠0 且 PM 行把净分方向从 S₀ 方向翻到另一侧（今天 3AM 场的机制）。
    for r in replayed:
        net = r["s0"] + r.get("pm_row", 0.0)
        r["net_dir"] = "UP" if net > 0 else ("DOWN" if net < 0 else "")
        r["net"] = net

    p1 = [r for r in all_dev15 if r["net_dir"] and r["net_dir"] != r["price_dir"]]
    p2 = [r for r in replayed if r["s0_dir"] and r["net_dir"] and r["s0_dir"] != r["net_dir"]]

    lines: list[str] = []
    lines.append("# 翻向可靠度回测 — 8/23（G4 价格位置回退 + PM 行翻转 双口径）\n")
    lines.append(f"- 回测时点：2026-08-23（脚本 scripts/g4_flip_reliability_backtest.py）")
    lines.append(f"- 样本：成功重放且有结算 actual 的场次 {len(replayed)}（跳过解析失败 {skipped}）；引擎 risks 实标 G4 回退 {engine_g4_fired} 场")
    lines.append(f"- |偏差|≥0.15% 场次：{len(all_dev15)}；S₀ 逆价格位置场：{len(cond_g4)}；净分仍逆价格位置场(P1)：{len(p1)}；PM 行翻转场(P2)：{len(p2)}\n")

    def table(rows, a_key, b_key, a_name, b_name):
        A, B = evaluate(rows, a_key, b_key)
        out = [f"| 策略 | N | 胜率 | Wilson95% |", "|---|---|---|---|"]
        for nm, rr in ((a_name, A), (b_name, B)):
            n = len(rr)
            if n == 0:
                out.append(f"| {nm} | 0 | — | — |")
                continue
            w = sum(1 for x in rr if x["ok"])
            lo, hi = wilson(w, n)
            out.append(f"| {nm} | {n} | {w}/{n}={w/n*100:.1f}% | [{lo:.1f}%, {hi:.1f}%] |")
        return "\n".join(out)

    lines.append("## 一、P1 口径：净分方向逆价格位置（|dev|≥0.15%）——G4 回退是否值得翻\n")
    lines.append(table(p1, "price_dir", "net_dir", "A 翻向价格位置（现行G4）", "B 保留净分方向（反事实）"))
    lines.append("")
    lines.append("## 二、P2 口径：PM 行把方向从 S₀ 翻转——今天 3AM 场的机制\n")
    lines.append(table(p2, "net_dir", "s0_dir", "A 翻向PM行后方向（现行）", "B 保留S₀方向（反事实）"))
    lines.append("")
    # P2 按 PM 档位分层
    lines.append("### P2 按 PM 共识档位分层\n")
    lines.append("| PM档 | N | A现行胜率 | B保留S₀胜率 | A-B差 |")
    lines.append("|---|---|---|---|---|")
    for k, kf in (("60-80%", lambda r: 60 <= r["pm_side"] < 80), ("≥80%", lambda r: r["pm_side"] >= 80)):
        g = [r for r in p2 if kf(r)]
        if not g:
            lines.append(f"| {k} | 0 | — | — | — |")
            continue
        ga, gb = evaluate(g, "net_dir", "s0_dir")
        wa = sum(1 for x in ga if x["ok"]) / len(ga) * 100
        wb = sum(1 for x in gb if x["ok"]) / len(gb) * 100
        lines.append(f"| {k} | {len(g)} | {wa:.1f}% | {wb:.1f}% | {wa-wb:+.1f}pp |")
    lines.append("")

    lines.append("## 三、参考口径：仅看 S₀（不含 PM 行）逆价格位置场\n")
    A0, B0, C0 = evaluate(cond_g4, "price_dir", "s0_dir"), None, None
    lines.append(table(cond_g4, "price_dir", "s0_dir", "A 翻向价格位置", "B 保留S₀方向"))
    lines.append("")

    lines.append("## 四、基准：全部 |偏差|≥0.15% 场，无脑跟价格位置\n")
    if all_dev15:
        wp = sum(1 for r in all_dev15 if r["price_dir"] == r["actual"])
        lo, hi = wilson(wp, len(all_dev15))
        lines.append(f"- {wp}/{len(all_dev15)} = {wp/len(all_dev15)*100:.1f}%（Wilson [{lo:.1f}%, {hi:.1f}%]）")
        lines.append(f"- 注：今天 3AM ET 场（PM行翻转型，S₀-1+PM+2→净分+1→UP）结算结果尚未入 results，未计入样本。\n")
    else:
        lines.append("- 无样本\n")

    lines.append("## 五、P1 逐场明细\n")
    lines.append("| slug | 偏差% | S₀ | PM行 | 净分 | 价格位置 | PM(%) | 实际 | A翻位置 | B留净分 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in sorted(p1, key=lambda x: x["slug"]):
        a_ok = "✅" if r["price_dir"] == r["actual"] else "❌"
        b_ok = "✅" if r["net_dir"] == r["actual"] else "❌"
        lines.append(f"| {r['slug'].replace('bitcoin-up-or-down-','')} | {r['raw_dev']:+.2f} | {r['s0']:+.0f} | {r.get('pm_row',0):+.0f} | {r['net']:+.0f} | {r['price_dir']} | {r['pm_dir']}({r['pm_side']:.0f}) | {r['actual']} | {a_ok} | {b_ok} |")

    lines.append("\n## 六、P2 逐场明细\n")
    lines.append("| slug | S₀ | PM行 | 净分 | PM(%) | 实际 | A现行 | B留S₀ |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in sorted(p2, key=lambda x: x["slug"]):
        a_ok = "✅" if r["net_dir"] == r["actual"] else "❌"
        b_ok = "✅" if r["s0_dir"] == r["actual"] else "❌"
        lines.append(f"| {r['slug'].replace('bitcoin-up-or-down-','')} | {r['s0']:+.0f} | {r.get('pm_row',0):+.0f} | {r['net']:+.0f} | {r['pm_dir']}({r['pm_side']:.0f}) | {r['actual']} | {a_ok} | {b_ok} |")

    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"报告写入 {OUT_MD}")

    # stdout 摘要
    def rate(rows, key):
        return (sum(1 for r in rows if r[key] == r["actual"]), len(rows)) if rows else (0, 0)
    for nm, rows, key in (("P1 A翻价格位置", p1, "price_dir"), ("P1 B留净分", p1, "net_dir"),
                          ("P2 A现行(PM行后)", p2, "net_dir"), ("P2 B留S₀", p2, "s0_dir")):
        w, n = rate(rows, key)
        if n:
            print(f"{nm}: {w}/{n} = {w/n*100:.1f}%")
    if cond_g4:
        wa, n = rate(cond_g4, "price_dir"); wb, _ = rate(cond_g4, "s0_dir")
        print(f"参考·S₀逆位置场: A翻位置 {wa}/{n}={wa/n*100:.1f}% vs B留S₀ {wb}/{n}={wb/n*100:.1f}%")
    if all_dev15:
        wp = sum(1 for r in all_dev15 if r["price_dir"] == r["actual"])
        print(f"基准·全部|dev|≥0.15%跟价格位置: {wp}/{len(all_dev15)} = {wp/len(all_dev15)*100:.1f}%")
    print(f"引擎risks实际标G4回退: {engine_g4_fired} 场")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
