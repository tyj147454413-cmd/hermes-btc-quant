#!/usr/bin/env python3
"""12 个决策点（:00,:05,...,:55）walk-forward 报告汇总对比。

读取 model/data/wf_report{,_dmNN}.json（v1 与 LSTM）的 oos 段，
汇总 AUC / Brier / acc / hit@0.65 随决策点变化的曲线，回答：
哪个决策时点的模型预测力最强（"信息量" vs "盘口已定价"的权衡）。

决策点与文件后缀映射：
    :00 -> _dm0   :05 -> _dm5   :10 -> _dm10  :15 -> (无后缀 wf_report.json)
    :20 -> _dm20  :25 -> _dm25  :30 -> _dm30  :35 -> _dm35
    :40 -> _dm40  :45 -> _dm45  :50 -> _dm50  :55 -> _dm55

用法：
    .venv/Scripts/python model/analyze_decision_points.py
输出：
    model/data/decision_point_analysis.json  原始汇总
    model/data/decision_point_curve.csv      曲线数据
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "model" / "data"

# 决策点 -> 报告文件后缀（dm15 无后缀，即 wf_report.json）
DM_SUFFIX = {0: "_dm0", 5: "_dm5", 10: "_dm10", 15: "", 20: "_dm20",
             25: "_dm25", 30: "_dm30", 35: "_dm35", 40: "_dm40",
             45: "_dm45", 50: "_dm50", 55: "_dm55"}


def load_oos(kind: str, dm: int) -> dict | None:
    """kind: 'lgbm'|'lstm'，返回报告 oos 段（缺文件/缺段返回 None）。"""
    stem = f"wf_report_lstm{DM_SUFFIX[dm]}" if kind == "lstm" else f"wf_report{DM_SUFFIX[dm]}"
    p = DATA_DIR / f"{stem}.json"
    if not p.exists():
        return None
    try:
        rep = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return rep.get("oos")


def num(x, nd=4):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, nd) if v == v else None  # NaN -> None


def main() -> int:
    dms = sorted(DM_SUFFIX)
    print(f"{'dm':>4} {'min':>4} | {'v1 AUC':>8} {'Brier':>7} {'acc':>7} "
          f"{'hit@.65':>8} | {'LSTM AUC':>9} {'LSTM Brier':>11}")
    print("-" * 78)
    rows = []
    for dm in dms:
        o_l = load_oos("lgbm", dm)
        o_s = load_oos("lstm", dm)
        if o_l is None:
            print(f"{dm:>4} {dm:>4} | {'--':>8} (no report)")
            continue
        r = {
            "decision_min": dm,
            "v1_auc": num(o_l.get("auc")),
            "v1_brier": num(o_l.get("brier")),
            "v1_acc": num(o_l.get("acc")),
            "v1_n": o_l.get("n"),
            "v1_hit65": num(o_l.get("hit@0.65")),
            "v1_n65": o_l.get("n@0.65"),
            "lstm_auc": num(o_s.get("auc")) if o_s else None,
            "lstm_brier": num(o_s.get("brier")) if o_s else None,
        }
        rows.append(r)
        la = f"{r['lstm_auc']:.4f}" if r["lstm_auc"] is not None else "--"
        lb = f"{r['lstm_brier']:.4f}" if r["lstm_brier"] is not None else "--"
        print(f"{dm:>4} {dm:>4} | {r['v1_auc']:>8.4f} {r['v1_brier']:>7.4f} "
              f"{r['v1_acc']:>7.4f} {r['v1_hit65']:>8.4f} | {la:>9} {lb:>11}")

    (DATA_DIR / "decision_point_analysis.json").write_text(
        json.dumps({"generated_note": "from existing wf_report files",
                    "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    # 曲线 CSV
    import csv
    with open(DATA_DIR / "decision_point_curve.csv", "w", newline="",
              encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["decision_min", "v1_auc", "v1_brier", "v1_acc",
                    "v1_hit65", "lstm_auc", "lstm_brier"])
        for r in rows:
            w.writerow([r["decision_min"], r["v1_auc"], r["v1_brier"],
                        r["v1_acc"], r["v1_hit65"],
                        r["lstm_auc"], r["lstm_brier"]])
    print(f"\n[OK] {len(rows)} 决策点已汇总 -> decision_point_analysis.json / curve.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())