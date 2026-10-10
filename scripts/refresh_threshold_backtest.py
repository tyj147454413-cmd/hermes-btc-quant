#!/usr/bin/env python3
"""
refresh 阈值收紧误伤回测（round35 监控环待回测项 #1）

问题：5pm-et 类贴线抖动场，0.10% 阈值 last-check 放过，:13-:15 跨轴后信号过期。
药方：阈值收紧到 0.05%（贴线抖动场）+ 写后 60 秒复核。
回测：在 211 场可复算样本里，找"多快照贴线抖动场"，
       对比 0.10% vs 0.05% 阈值触发的 refresh 次数 + 后续胜率变化。

用法：python3 scripts/refresh_threshold_backtest.py
"""
from __future__ import annotations
import json, sys, math
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent))
from signal_validator import extract_factors
from rule_engine import judge

# 用脚本所在目录的相对路径（sandbox/宿主都能跑）
_HERE = Path(__file__).resolve().parent
_PROJECT = _HERE.parent
SNAPSHOT_DIR = _PROJECT / "data" / "snapshots"
RESULTS_FILE = _PROJECT / "data" / "hermes_results.jsonl"


def wilson_lo(p: float, n: int, z: float = 1.96) -> float:
    """Wilson 95% CI 下界"""
    if n == 0:
        return 0.0
    denom = 1 + z*z/n
    center = (p + z*z/(2*n)) / denom
    half = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / denom
    return max(0.0, center - half) * 100


def load_results() -> dict[str, dict]:
    out = {}
    if not RESULTS_FILE.exists():
        return out
    for line in RESULTS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("slug") and r.get("actual"):
            out[r["slug"]] = r
    return out


def get_snapshots_by_slug() -> dict[str, list[dict]]:
    """返回每个 slug 的多个快照（按时间排序）"""
    out = defaultdict(list)
    for p in sorted(SNAPSHOT_DIR.glob("*.json")):
        # 文件名格式：<slug>_<YYYYMMDD>_<HHMMSS>.json
        name = p.stem
        parts = name.rsplit("_", 2)
        if len(parts) != 3:
            continue
        slug = parts[0]
        try:
            ts = parts[1] + parts[2]  # YYYYMMDDHHMMSS
            data = json.loads(p.read_text(encoding="utf-8"))
            data["_path"] = p
            data["_snap_ts"] = ts
            out[slug].append(data)
        except (json.JSONDecodeError, ValueError):
            continue
    return {k: sorted(v, key=lambda x: x["_snap_ts"]) for k, v in out.items()}


def get_dev_pct(snap: dict) -> float:
    """快照的 |deviation%|"""
    cur = snap.get("current_price")
    anchor = snap.get("anchor_price")
    if not cur or not anchor:
        return 999.0
    return abs(cur - anchor) / anchor * 100


def main():
    results = load_results()
    snaps_by_slug = get_snapshots_by_slug()

    # 筛：必须有多快照 + 在 results 里
    candidates = {s: snaps for s, snaps in snaps_by_slug.items()
                  if len(snaps) >= 2 and s in results}
    print(f"=== 多快照 + 已结算场次：{len(candidates)} 场 ===\n")

    # 定义：贴线抖动场 = 首快照 |dev| < 0.10% + 跨轴（多快照间 dev 跨过 0）
    jittered = []
    for slug, snaps in candidates.items():
        first = snaps[0]
        dev_first = get_dev_pct(first)
        if dev_first >= 0.10:
            continue
        # 跨轴：任意两个快照 dev 符号不同（cur vs anchor）
        signs = set()
        for s in snaps:
            cur = s.get("current_price")
            anchor = s.get("anchor_price")
            if cur and anchor:
                signs.add(1 if cur >= anchor else -1)
        if -1 in signs and 1 in signs:
            jittered.append((slug, snaps, dev_first))

    print(f"=== 贴线抖动场（首快照 |dev|<0.10% + 跨轴）：{len(jittered)} 场 ===\n")

    # 对比 0.10% vs 0.05% 阈值
    # refresh 触发：last-check 看到的实时 |dev| < 阈值 → 触发
    # 这里"实时"=每个快照都测，看哪些快照会触发 refresh
    # 然后看实际结算 vs 首快照预测方向是否一致
    summary_010 = {"refresh_triggers": 0, "post_refresh_wins": 0, "post_refresh_total": 0,
                   "first_dir_wins": 0, "first_dir_total": 0}
    summary_005 = {"refresh_triggers": 0, "post_refresh_wins": 0, "post_refresh_total": 0,
                   "first_dir_wins": 0, "first_dir_total": 0}

    extra_005_only = []  # 0.05% 多触发的场次

    for slug, snaps, dev_first in jittered:
        actual = results[slug]["actual"]
        # 首快照引擎方向（extract_factors 返回 tuple，judge 返回 Decision dataclass）
        try:
            _f1, ji1, _n1, _m1 = extract_factors(snaps[0])
            j1 = judge(ji1)
            first_dir = j1.final_direction
        except Exception:
            continue

        # 实际 vs 首快照预测
        first_correct = (first_dir == actual)
        summary_010["first_dir_total"] += 1
        summary_005["first_dir_total"] += 1
        if first_correct:
            summary_010["first_dir_wins"] += 1
            summary_005["first_dir_wins"] += 1

        # 各阈值 refresh 触发次数
        n_010 = sum(1 for s in snaps if get_dev_pct(s) < 0.10)
        n_005 = sum(1 for s in snaps if get_dev_pct(s) < 0.05)

        # 如果 refresh 触发，看后续快照里"翻对"次数（任一刷新后方向与实际一致 → 翻对）
        def post_refresh_correct(threshold):
            for s in snaps:
                if get_dev_pct(s) < threshold:
                    try:
                        _ff, jii, _nn, _mm = extract_factors(s)
                        jj = judge(jii)
                        if jj.final_direction == actual:
                            return True
                    except Exception:
                        pass
            return False

        # 0.10% 阈值
        if n_010 >= 1:
            summary_010["refresh_triggers"] += 1
            summary_010["post_refresh_total"] += 1
            if post_refresh_correct(0.10):
                summary_010["post_refresh_wins"] += 1
        # 0.05% 阈值
        if n_005 >= 1:
            summary_005["refresh_triggers"] += 1
            summary_005["post_refresh_total"] += 1
            if post_refresh_correct(0.05):
                summary_005["post_refresh_wins"] += 1
        # 多触发记录
        if n_005 > n_010:
            extra_005_only.append({"slug": slug, "n_010": n_010, "n_005": n_005,
                                    "dev_first": dev_first, "first_dir": first_dir,
                                    "actual": actual, "first_correct": first_correct})

    print("=== 0.10% 阈值（当前）===")
    print(f"  refresh 触发场次: {summary_010['refresh_triggers']}/{len(jittered)}")
    if summary_010['refresh_triggers']:
        wr = summary_010['post_refresh_wins']/summary_010['refresh_triggers']*100
        print(f"  refresh 后方向胜率: {wr:.1f}% ({summary_010['post_refresh_wins']}/{summary_010['refresh_triggers']})")
        wlo = wilson_lo(summary_010['post_refresh_wins']/summary_010['refresh_triggers'], summary_010['refresh_triggers'])
        print(f"  Wilson95 下界: {wlo:.1f}%")
    if summary_010['first_dir_total']:
        fr = summary_010['first_dir_wins']/summary_010['first_dir_total']*100
        print(f"  首快照引擎方向胜率: {fr:.1f}%")

    print("\n=== 0.05% 阈值（建议收紧）===")
    print(f"  refresh 触发场次: {summary_005['refresh_triggers']}/{len(jittered)}")
    if summary_005['refresh_triggers']:
        wr = summary_005['post_refresh_wins']/summary_005['refresh_triggers']*100
        print(f"  refresh 后方向胜率: {wr:.1f}% ({summary_005['post_refresh_wins']}/{summary_005['refresh_triggers']})")
        wlo = wilson_lo(summary_005['post_refresh_wins']/summary_005['refresh_triggers'], summary_005['refresh_triggers'])
        print(f"  Wilson95 下界: {wlo:.1f}%")
    if summary_005['first_dir_total']:
        fr = summary_005['first_dir_wins']/summary_005['first_dir_total']*100
        print(f"  首快照引擎方向胜率: {fr:.1f}%")

    print(f"\n=== 0.05% 阈值多触发的场次: {len(extra_005_only)} 场 ===")
    for e in extra_005_only:
        marker = "✅ 翻对" if e['first_correct'] else "❌ 翻错"
        print(f"  {e['slug']} | n_010={e['n_010']} n_005={e['n_005']} "
              f"dev_first={e['dev_first']:.3f}% first={e['first_dir']} actual={e['actual']} {marker}")

    # 关键差值
    if summary_010['refresh_triggers'] and summary_005['refresh_triggers']:
        delta_triggers = summary_005['refresh_triggers'] - summary_010['refresh_triggers']
        print(f"\n=== 关键差值 ===")
        print(f"  多触发的 refresh: +{delta_triggers} 场（误伤候选）")
        delta_wins = summary_005['post_refresh_wins'] - summary_010['post_refresh_wins']
        print(f"  多翻对的场次: +{delta_wins} 场（救场）")
        if delta_triggers > 0:
            ratio = delta_wins / delta_triggers * 100
            print(f"  误伤率: {(1 - delta_wins/delta_triggers)*100:.1f}%（{delta_triggers-delta_wins}/{delta_triggers} 翻错）")

    # 总体胜率（与引擎基线对比）
    print(f"\n=== 引擎基线对比 ===")
    if summary_010['first_dir_total']:
        base_wr = summary_010['first_dir_wins']/summary_010['first_dir_total']*100
        print(f"  首快照方向胜率（不 refresh）: {base_wr:.1f}%")
    if summary_010['refresh_triggers'] and summary_010['refresh_triggers'] > 0:
        ref_wr = summary_010['post_refresh_wins']/summary_010['refresh_triggers']*100
        if summary_010['first_dir_total']:
            base_wr = summary_010['first_dir_wins']/summary_010['first_dir_total']*100
            print(f"  refresh 后方向胜率（0.10%）: {ref_wr:.1f}%  提升: {ref_wr-base_wr:+.1f}pp")
    if summary_005['refresh_triggers'] and summary_005['refresh_triggers'] > 0:
        ref_wr = summary_005['post_refresh_wins']/summary_005['refresh_triggers']*100
        if summary_005['first_dir_total']:
            base_wr = summary_005['first_dir_wins']/summary_005['first_dir_total']*100
            print(f"  refresh 后方向胜率（0.05%）: {ref_wr:.1f}%  提升: {ref_wr-base_wr:+.1f}pp")


if __name__ == "__main__":
    main()