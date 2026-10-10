#!/usr/bin/env python3
"""
图表分析.md v1.4 特征有效性分析

对每个历史特征单独判断方向，对比 actual 统计胜率，找出哪些信号真正有效、
哪些是噪音/反指。核心是回答：图表分析.md 框架里，到底哪些"看图方法"有统计优势。

纯本地，无网络。
"""
# pyright: reportAny=false

import json
import math
import sys
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
DATA_DIR = THIS_DIR.parent / "data"
SNAP_DIR = DATA_DIR / "snapshots"
RESULTS = DATA_DIR / "hermes_results.jsonl"


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def wilson_ci(w: int, n: int, z: float = 1.96):
    if n == 0:
        return 0.0, 0.0
    p = w / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return center - half, center + half


def _s(v, d=0.0):
    try:
        return float(v) if v is not None else d
    except (TypeError, ValueError):
        return d


def main() -> int:
    results = load_jsonl(RESULTS)
    actual_map = {r.get("slug", ""): r.get("actual") for r in results}

    rows = []
    for f in sorted(SNAP_DIR.glob("*.json")):
        if f.stat().st_size < 40000:
            continue
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        slug = d.get("slug", "")
        if slug not in actual_map:
            continue
        ph = d.get("prev_hour") or {}
        cf = d.get("capital_flow") or {}
        hist = d.get("historical") or {}
        kl = d.get("klines_1h") or []

        # 方向序列（已收盘，去掉当前小时）
        dir_seq = []
        if kl:
            for k in kl[:-1]:
                dir_seq.append("UP" if k["c"] >= k["o"] else "DOWN")
        consecutive = 0
        last_dir = dir_seq[-1] if dir_seq else None
        if dir_seq:
            for sd in reversed(dir_seq):
                if sd == last_dir:
                    consecutive += 1
                else:
                    break

        # prev 方向
        prev_side = ph.get("side")
        taker = _s(cf.get("taker_bs_ratio"), 1.0)
        oi = _s(cf.get("oi_change_24h_pct"), 0.0)
        lsr = _s(cf.get("long_short_ratio"), 1.0)
        of = _s((cf.get("order_flow") or {}).get("imbalance_5m"), 0.0)
        up_rate = _s(hist.get("up_rate"), 0.5)
        fear = _s((d.get("sentiment") or {}).get("fear_greed_index"), 50)

        rows.append({
            "slug": slug, "actual": actual_map[slug],
            "prev_side": prev_side, "dir_seq": dir_seq, "last_dir": last_dir,
            "consecutive": consecutive, "taker": taker, "oi": oi,
            "lsr": lsr, "of": of, "up_rate": up_rate, "fear": fear,
        })

    n = len(rows)
    print(f"样本: {n}")

    def report(name, pred_fn, cond_desc):
        sub = [r for r in rows if pred_fn(r)]
        if not sub:
            print(f"  {name}: 无样本")
            return
        w = sum(1 for r in sub if r["pred"] == r["actual"])
        lo, hi = wilson_ci(w, len(sub))
        # 同方向对比：该桶内 prev 方向延续
        print(f"  {name}: {w}/{len(sub)} = {w/len(sub):.1%} [{lo:.1%},{hi:.1%}]")

    print("\n=== 特征1: prev_hour 方向延续（连庄基础） ===")
    for r in rows:
        r["pred"] = r["prev_side"]
    w = sum(1 for r in rows if r["pred"] == r["actual"])
    lo, hi = wilson_ci(w, n)
    print(f"  [全部] 押prev方向: {w}/{n} = {w/n:.1%} [{lo:.1%},{hi:.1%}]")
    # 分连庄长度
    for name, cond in [("连庄≥2", lambda r: r["consecutive"] >= 2),
                       ("连庄≥3", lambda r: r["consecutive"] >= 3),
                       ("连庄1(无连庄)", lambda r: r["consecutive"] == 1),
                       ("连庄≥5", lambda r: r["consecutive"] >= 5)]:
        sub = [r for r in rows if cond(r)]
        if sub:
            w = sum(1 for r in sub if r["prev_side"] == r["actual"])
            lo, hi = wilson_ci(w, len(sub))
            print(f"  {name}: {w}/{len(sub)} = {w/len(sub):.1%} [{lo:.1%},{hi:.1%}]")

    print("\n=== 特征2: 历史时段 up_rate ===")
    for name, cond, pred_up in [
        ("up_rate>0.6判UP", lambda r: r["up_rate"] > 0.6, True),
        ("up_rate<0.4判DOWN", lambda r: r["up_rate"] < 0.4, False),
        ("up_rate>0.55判UP", lambda r: r["up_rate"] > 0.55, True),
        ("up_rate>0.6判DOWN(反向试)", lambda r: r["up_rate"] > 0.6, False),
    ]:
        sub = [r for r in rows if cond(r)]
        if sub:
            for r in sub:
                r["pred"] = "UP" if pred_up else "DOWN"
            w = sum(1 for r in sub if r["pred"] == r["actual"])
            lo, hi = wilson_ci(w, len(sub))
            print(f"  {name}: {w}/{len(sub)} = {w/len(sub):.1%} [{lo:.1%},{hi:.1%}]")

    print("\n=== 特征3: 庄家剧本 taker_bs_ratio ===")
    for name, cond, pred in [
        ("taker>1.3判UP(买)", lambda r: r["taker"] > 1.3, "UP"),
        ("taker>1.3判DOWN(反向)", lambda r: r["taker"] > 1.3, "DOWN"),
        ("taker<0.77判DOWN(卖)", lambda r: r["taker"] < 0.77, "DOWN"),
        ("taker<0.77判UP(反向)", lambda r: r["taker"] < 0.77, "UP"),
        ("taker>1.5判UP", lambda r: r["taker"] > 1.5, "UP"),
    ]:
        sub = [r for r in rows if cond(r)]
        if sub:
            for r in sub:
                r["pred"] = pred
            w = sum(1 for r in sub if r["pred"] == r["actual"])
            lo, hi = wilson_ci(w, len(sub))
            print(f"  {name}: {w}/{len(sub)} = {w/len(sub):.1%} [{lo:.1%},{hi:.1%}]")

    print("\n=== 特征4: 散户反指 long_short_ratio ===")
    for name, cond, pred in [
        ("lsr>1.3判DOWN(拥挤反指)", lambda r: r["lsr"] > 1.3, "DOWN"),
        ("lsr>1.3判UP(顺势)", lambda r: r["lsr"] > 1.3, "UP"),
        ("lsr>1.5判DOWN", lambda r: r["lsr"] > 1.5, "DOWN"),
        ("lsr<1.1判UP", lambda r: r["lsr"] < 1.1, "UP"),
    ]:
        sub = [r for r in rows if cond(r)]
        if sub:
            for r in sub:
                r["pred"] = pred
            w = sum(1 for r in sub if r["pred"] == r["actual"])
            lo, hi = wilson_ci(w, len(sub))
            print(f"  {name}: {w}/{len(sub)} = {w/len(sub):.1%} [{lo:.1%},{hi:.1%}]")

    print("\n=== 特征5: 订单流 order_flow ===")
    for name, cond, pred in [
        ("of>0.1判UP", lambda r: r["of"] > 0.1, "UP"),
        ("of<-0.1判DOWN", lambda r: r["of"] < -0.1, "DOWN"),
        ("of<-0.1判UP(反向)", lambda r: r["of"] < -0.1, "UP"),
    ]:
        sub = [r for r in rows if cond(r)]
        if sub:
            for r in sub:
                r["pred"] = pred
            w = sum(1 for r in sub if r["pred"] == r["actual"])
            lo, hi = wilson_ci(w, len(sub))
            print(f"  {name}: {w}/{len(sub)} = {w/len(sub):.1%} [{lo:.1%},{hi:.1%}]")

    print("\n=== 特征6: 恐贪 fear ===")
    for name, cond, pred in [
        ("fear<25判UP(恐慌反指)", lambda r: r["fear"] < 25, "UP"),
        ("fear<25判DOWN", lambda r: r["fear"] < 25, "DOWN"),
        ("fear>70判DOWN(贪婪反指)", lambda r: r["fear"] > 70, "DOWN"),
    ]:
        sub = [r for r in rows if cond(r)]
        if sub:
            for r in sub:
                r["pred"] = pred
            w = sum(1 for r in sub if r["pred"] == r["actual"])
            lo, hi = wilson_ci(w, len(sub))
            print(f"  {name}: {w}/{len(sub)} = {w/len(sub):.1%} [{lo:.1%},{hi:.1%}]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
