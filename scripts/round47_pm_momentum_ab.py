#!/usr/bin/env python3
"""
PM 动量权重 ×2 vs ×3 历史对照实验（round47）
对 82 笔带 up_price_delta 的快照，分别用 W=2 和 W=3 跑 replay_judge_full，
对比 S₀/pm_momentum 分/final_direction/probability/confidence，并用 hermes_results 算 PnL diff。
"""
import sys, os, json, importlib
from pathlib import Path

ROOT = Path(os.environ.get("HERMES_DIR", "/Users/tangyijun/Desktop/hermes btc"))
sys.path.insert(0, str(ROOT / "scripts"))

import signal_validator
import rule_engine
from replay_common import load_results_full, SNAPSHOT_DIR

# 备份原权重
W_ORIG = signal_validator.W_PM_MOMENTUM
print(f"[init] W_PM_MOMENTUM 初始值 = {W_ORIG}")

def reload_engine():
    """修改 W 后让 extract_factors 用新值"""
    importlib.reload(signal_validator)
    importlib.reload(rule_engine)

def collect_delta_samples():
    """收集所有带 up_price_delta 的快照"""
    samples = []
    for f in sorted(SNAPSHOT_DIR.glob("*.json")):
        try:
            with open(f) as fp:
                d = json.load(fp)
            pm = d.get("polymarket", {}) or {}
            if pm.get("up_price_delta") is not None:
                slug = d.get("slug") or f.stem
                samples.append((slug, d))
        except Exception:
            continue
    return samples

def replay_one(md, w_val):
    """用指定 W 跑一次"""
    signal_validator.W_PM_MOMENTUM = w_val
    from signal_validator import extract_factors
    from rule_engine import judge
    try:
        factors, inp, notes, mode = extract_factors(md)
    except Exception as e:
        return None
    pm = factors.get("pm_momentum")
    s0_raw = sum(v for k, v in factors.items()
                 if k not in ("polymarket",) and isinstance(v, (int, float)))
    inp["extreme_wall"] = False
    inp.pop("_buy_wall_ratio", None)
    inp.pop("_sell_wall_ratio", None)
    inp.pop("taker_bs_ratio", None)
    d = judge(inp)
    return {
        "pm_momentum_factor": pm,
        "factors_net_excl_pm": s0_raw - (pm or 0),
        "final_direction": d.final_direction,
        "probability": getattr(d, "probability", None),
        "confidence": getattr(d, "confidence", None),
        "max_price": getattr(d, "max_price", None),
        "notes": notes,
    }

def main():
    samples = collect_delta_samples()
    print(f"[sample] 带 up_price_delta 的快照 = {len(samples)}")
    results = load_results_full()
    print(f"[results] hermes_results.jsonl 结算样本 = {len(results)}")

    # 先跑 ×3（当前 round47）
    print("\n=== Pass 1: W_PM_MOMENTUM = 3 (当前 round47) ===")
    out_w3 = {}
    for slug, md in samples:
        r = replay_one(md, 3)
        out_w3[slug] = r

    # 再跑 ×2（前身 round46 之前）
    print("=== Pass 2: W_PM_MOMENTUM = 2 (前身 round46) ===")
    out_w2 = {}
    for slug, md in samples:
        r = replay_one(md, 2)
        out_w2[slug] = r

    # 恢复原值
    signal_validator.W_PM_MOMENTUM = W_ORIG
    print(f"[done] W_PM_MOMENTUM 恢复 = {W_ORIG}")

    # 对比
    diffs = []
    flips = 0
    same = 0
    for slug, _ in samples:
        a = out_w3.get(slug) or {}
        b = out_w2.get(slug) or {}
        da = a.get("final_direction")
        db = b.get("final_direction")
        changed = (da != db)
        if changed:
            flips += 1
        else:
            same += 1
        diffs.append({
            "slug": slug,
            "delta": (results.get(slug) or {}).get("actual"),
            "w3_dir": da, "w3_prob": a.get("probability"),
            "w3_conf": a.get("confidence"),
            "w3_pm_fac": a.get("pm_momentum_factor"),
            "w2_dir": db, "w2_prob": b.get("probability"),
            "w2_conf": b.get("confidence"),
            "w2_pm_fac": b.get("pm_momentum_factor"),
            "w3_s0": a.get("factors_net_excl_pm"),
            "w2_s0": b.get("factors_net_excl_pm"),
            "dir_changed": changed,
        })

    # 算 PnL 对比（仅方向变化的样本才有意义）
    def calc_pnl(rows, dir_key):
        total, win, loss, skip = 0, 0, 0, 0
        hit = 0
        n_with_result = 0
        for r in rows:
            actual = r["delta"]
            d = r[dir_key]
            if actual not in ("UP", "DOWN") or d not in ("UP", "DOWN"):
                skip += 1
                continue
            n_with_result += 1
            if d == actual:
                win += 1
                hit += 1
            else:
                loss += 1
        return {"n": n_with_result, "win": win, "loss": loss,
                "skip": skip, "win_rate": hit / n_with_result if n_with_result else 0}

    p_w3 = calc_pnl(diffs, "w3_dir")
    p_w2 = calc_pnl(diffs, "w2_dir")

    print(f"\n=== 总览 ===")
    print(f"总样本: {len(samples)}")
    print(f"方向变化样本: {flips}")
    print(f"方向不变样本: {same}")
    print(f"\n×3 命中率: {p_w3['win_rate']:.1%}  ({p_w3['win']}/{p_w3['n']}, skip={p_w3['skip']})")
    print(f"×2 命中率: {p_w2['win_rate']:.1%}  ({p_w2['win']}/{p_w2['n']}, skip={p_w2['skip']})")

    # 仅看方向变化的样本对比
    flipped = [r for r in diffs if r["dir_changed"]]
    if flipped:
        print(f"\n=== 方向变化样本 ({len(flipped)} 笔) ===")
        for r in flipped[:30]:
            print(f"  {r['slug'][:55]:55s}  ×3={r['w3_dir']}({r['w3_prob']:.0%})  ×2={r['w2_dir']}({r['w2_prob']:.0%})  actual={r['delta']}")

    # 保存到 json 供后续分析
    out_path = ROOT / "data" / "round47_pm_momentum_ab.json"
    out_path.write_text(json.dumps({
        "summary": {
            "n_total": len(samples),
            "n_flip": flips,
            "n_same": same,
            "win_rate_w3": p_w3["win_rate"],
            "win_rate_w2": p_w2["win_rate"],
            "n_with_result_w3": p_w3["n"],
            "n_with_result_w2": p_w2["n"],
        },
        "details": diffs,
    }, indent=2, ensure_ascii=False))
    print(f"\n[save] 详细结果 → {out_path}")

if __name__ == "__main__":
    main()
