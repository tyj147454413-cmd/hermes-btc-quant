#!/usr/bin/env python3
"""pm 多时点轨迹特征（B 线：盘感增强的样本原料加工）。

从 data/ml_shadow_predictions.jsonl（每个决策点一条 pm_up 采样）出发，
按小时（slug）聚合出"当小时 PM 盘口演变轨迹"，构造 9 个轨迹特征：

    pm_traj_n            可用采样点数（<=max_dm 的决策点数）
    pm_up_dec            决策点最新 pm_up（与 fetch_pm_feats 同口径）
    pm_delta             首末差（同口径，保留对照）
    pm_traj_mean         轨迹均值（盘口中枢）
    pm_traj_std          轨迹波动（盘口摇摆度）
    pm_range             全程振幅 max-min
    pm_last3_slope       最后 3 点最小二乘斜率（最新动能，比两点差抗噪）
    pm_firsthalf_ret     前段变化（最早→中点）
    pm_secondhalf_ret    后段变化（中点→最新）
    pm_decel             后段-前段（动能加速度，>0=动能增强）

防泄漏口径：--max-dm 控制"决策点可见窗口"（train dm35 行只能用 <=:35 的采样，
即 0/5/10/15/20/25/30/35 共 8 点；复盘全轨迹用 --max-dm 55）。
兼容 fetch_pm_feats：pm_up_dec/pm_delta 两列可直接替换原两点特征。

用法：
    .venv/Scripts/python model/pm_traj_feats.py --max-dm 35           # 训练口径
    .venv/Scripts/python model/pm_traj_feats.py --max-dm 55 --verbose # 全轨迹 + 分布明细
产出：
    model/data/pm_traj_features.csv   每个 slug 一行的轨迹特征
    （统计缺口打印在 stdout）
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA_DIR = ROOT / "model" / "data"
SHADOW_DEFAULT = ROOT / "data" / "ml_shadow_predictions.jsonl"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def load_pm_series(path: Path, max_dm: int) -> dict[str, list[tuple[int, float]]]:
    """slug -> [(decision_min, pm_up), ...] 升序。"""
    if not path.exists():
        return {}
    by_slug: dict[str, list[tuple[int, float]]] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("pm_up") is None:
            continue
        dm = r.get("decision_min")
        if dm is None or dm > max_dm:
            continue
        # 不滤 hours_ago：与 hermes_common.fetch_pm_feats 口径一致，
        # 补录行同样是该 dm 时刻的真实 pm_up 采样。
        by_slug.setdefault(r["slug"], []).append((dm, float(r["pm_up"])))
    for slug in by_slug:
        seen, uniq = set(), []
        for dm, v in sorted(by_slug[slug]):
            if dm not in seen:
                seen.add(dm)
                uniq.append((dm, v))
        by_slug[slug] = uniq
    return by_slug


def lin_slope(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den, 6)


def main() -> int:
    ap = argparse.ArgumentParser(description="pm 多时点轨迹特征")
    ap.add_argument("--shadow", default=str(SHADOW_DEFAULT))
    ap.add_argument("--max-dm", type=int, default=35,
                    help="决策点可见窗口上界（训练口径 35；全轨迹 55）")
    args = ap.parse_args()

    by_slug = load_pm_series(Path(args.shadow), args.max_dm)
    if not by_slug:
        print(f"[X] 无样本（{args.shadow}）")
        return 1

    rows = []
    for slug, pts in sorted(by_slug.items()):
        dms = [d for d, _ in pts]
        pms = [p for _, p in pts]
        n = len(pts)
        mid_idx = n // 2
        r = {
            "slug": slug, "pm_traj_n": n,
            "pm_up_dec": pms[-1],
            "pm_delta": round(pms[-1] - pms[0], 4),
            "pm_traj_mean": round(sum(pms) / n, 4),
            "pm_traj_std": round(
                (sum((p - sum(pms) / n) ** 2 for p in pms) / n) ** 0.5, 4),
            "pm_range": round(max(pms) - min(pms), 4),
            "pm_last3_slope": lin_slope(dms[-3:], pms[-3:]) if n >= 3 else None,
            "pm_firsthalf_ret": round(pms[mid_idx] - pms[0], 4) if n >= 2 else None,
            "pm_secondhalf_ret": round(pms[-1] - pms[mid_idx], 4) if n >= 2 else None,
        }
        if r["pm_firsthalf_ret"] is not None and r["pm_secondhalf_ret"] is not None:
            r["pm_decel"] = round(r["pm_secondhalf_ret"] - r["pm_firsthalf_ret"], 4)
        rows.append(r)

    # —— 样本缺口统计 ——
    cnt = Counter(r["pm_traj_n"] for r in rows)
    print(f"\n===== pm 轨迹特征（max_dm={args.max_dm}）=====")
    print(f"已聚合小时数: {len(rows)}")
    for k in sorted(cnt, reverse=True):
        print(f"  采样点 {k:>2} 个的小时: {cnt[k]}")
    usable = sum(v for k, v in cnt.items() if k >= 5)
    print(f"轨迹特征可用（>=5 点）小时: {usable}  "
          f"= 可直接扩入 gs 训练集的样本上限")

    # —— 轨迹形态摘要 ——
    slopes = [r["pm_last3_slope"] for r in rows if r["pm_last3_slope"] is not None]
    if slopes:
        pos = sum(1 for s in slopes if s > 0)
        print(f"最后3点斜率: 均值 {round(sum(slopes)/len(slopes), 4)} | "
              f"上行占比 {round(pos/len(slopes), 3)}（{pos}/{len(slopes)}）")

    import csv
    out = DATA_DIR / "pm_traj_features.csv"
    fields = ["slug", "pm_traj_n", "pm_up_dec", "pm_delta", "pm_traj_mean",
              "pm_traj_std", "pm_range", "pm_last3_slope", "pm_firsthalf_ret",
              "pm_secondhalf_ret", "pm_decel"]
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"[OK] -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())