#!/usr/bin/env python3
"""
贴线区影子记录器（第一层·零风险落地，2026-08-23 用户批准）。

目的：为 docs/proposal_tiezone_winrate_fix_20260823.md 的投票调制器积累前瞻样本，
     验证「顺引擎票数 0→4 单调梯度」在新数据上是否复现；≥30 场前瞻结算样本后复核。

设计约束：
- 不改任何规则文件（prompt_exec.md / CLAUDE.md / SKILL 锚点均不动）；
- 不碰下单链路（bot / signal 写入完全无感知）；快照本来就落盘，本脚本纯盘后离线补算；
- 幂等：状态存 data/tie_votes_shadow_state.json，输出整体重写 data/tie_votes_shadow.jsonl，
  重复运行不产生重复记录。

票池定义（与研究报告一致，均为"与引擎最终方向同向则 +1 票"）：
  taker_bs_ratio(即时)、MACD hist 5m 符号、多空比斜率(long_short_ratio_trend 首尾差)、
  支阻不对称(weak_resist/weak_support 距离差)。

用法：
  python3 scripts/tie_votes_shadow.py           # 增量扫描新快照并刷新输出
  python3 scripts/tie_votes_shadow.py --summary # 只打印前瞻进度
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from replay_common import load_results_actual, replay_judge_full  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SNAP_DIR = ROOT / "data" / "snapshots"
STATE_FILE = ROOT / "data" / "tie_votes_shadow_state.json"
OUT_FILE = ROOT / "data" / "tie_votes_shadow.jsonl"
VERDICT_FILE = ROOT / "data" / "tiezone_verdict.md"
FLAG_FILE = ROOT / "data" / "tiezone_READY.flag"
BASELINE_CUTOFF = "2026-08-23"  # 结算日 ≤ 此日期记 history_baseline，之后为 forward
TIE_MAX = 0.15

# 转正三条件（docs/proposal_tiezone_winrate_fix_20260823.md §落地状态）
CRITERIA_MIN_FORWARD = 30   # ① 前瞻已结算 ≥30 场
CRITERIA_LOW_MAX_PCT = 60.0  # ③ 低票组(≤1票)胜率 < 60%


def _fv(v):
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def compute_record(md: dict) -> dict | None:
    """从单份快照算投票记录。非贴线区返回 None。"""
    rep = replay_judge_full(md)
    if rep is None:
        return None
    d, _factors, inp = rep
    dev = _fv(inp.get("deviation_pct"))
    final = d.final_direction
    if dev is None or abs(dev) >= TIE_MAX or final not in ("UP", "DOWN"):
        return None

    cf = md.get("capital_flow") or {}
    tf5 = (md.get("timeframes") or {}).get("5m") or {}
    kl = md.get("key_levels") or {}
    cur = _fv(md.get("current_price"))
    members = {}

    t = _fv(cf.get("taker_bs_ratio"))
    if t is not None and t != 1:
        members["taker_now"] = "UP" if t > 1 else "DOWN"
    mh = _fv(tf5.get("macd_hist"))
    if mh is not None and mh != 0:
        members["macd_5m"] = "UP" if mh > 0 else "DOWN"
    lv = [_fv(x) for x in (cf.get("long_short_ratio_trend") or []) if _fv(x) is not None]
    if len(lv) >= 2 and lv[-1] != lv[0]:
        members["ls_slope"] = "UP" if lv[-1] > lv[0] else "DOWN"
    ws, wr = _fv(kl.get("weak_support")), _fv(kl.get("weak_resistance"))
    if cur and ws and wr:
        ka = (wr - cur) / cur * 100 - (cur - ws) / cur * 100
        if abs(ka) > 1e-9:
            members["kl_asym"] = "UP" if ka > 0 else "DOWN"

    pm_up = _fv((md.get("polymarket") or {}).get("up_price"))
    votes = sum(1 for v in members.values() if v == final)
    try:
        settle_day = md["timestamp"][:10]
    except Exception:
        settle_day = None
    return {
        "dev_pct": round(dev, 4),
        "engine_dir": final,
        "votes": votes,
        "members": members,
        "pm_conf": (round(pm_up if final == "UP" else 1 - pm_up, 3) if pm_up is not None else None),
        "settle_day": settle_day,
        "snapshot_mtime": md.get("collect_time_utc"),
    }


def evaluate_criteria(forward_done: list[dict]) -> dict:
    """转正三条件判定。返回 verdict: 'pending' / 'pass' / 'fail' 及明细。

    ② 梯度复现 = 高票组(≥3票)胜率 > 前瞻整体基线 > 低票组(≤1票)胜率。
    """
    n = len(forward_done)
    wins = [v for v in forward_done if v.get("actual") == v["engine_dir"]]
    overall = len(wins) / n * 100 if n else None
    low = [v for v in forward_done if v["votes"] <= 1]
    high = [v for v in forward_done if v["votes"] >= 3]
    low_rate = (sum(1 for v in low if v.get("actual") == v["engine_dir"]) / len(low) * 100) if low else None
    high_rate = (sum(1 for v in high if v.get("actual") == v["engine_dir"]) / len(high) * 100) if high else None

    checks = {
        "c1_n30": n >= CRITERIA_MIN_FORWARD,
        "c2_gradient": bool(overall is not None and low_rate is not None and high_rate is not None
                            and high_rate > overall > low_rate),
        "c3_low_lt60": bool(low_rate is not None and low_rate < CRITERIA_LOW_MAX_PCT),
    }
    if n < CRITERIA_MIN_FORWARD:
        verdict = "pending"
    elif all(checks.values()):
        verdict = "pass"
    else:
        verdict = "fail"
    return {"verdict": verdict, "n": n, "overall": overall,
            "low_n": len(low), "low_rate": low_rate,
            "high_n": len(high), "high_rate": high_rate,
            "checks": checks}


def write_verdict(res: dict) -> None:
    """样本达标时落盘结论产物（flag + 结论报告），供任何后续会话/监控直接发现。"""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    ok = res["verdict"] == "pass"
    lines = [
        f"# 贴线区投票器前瞻验证结论（{now}）",
        "",
        f"- 前瞻已结算：**{res['n']} 场**（条件①≥30：{'✅' if res['checks']['c1_n30'] else '❌'}）",
        f"- 整体基线命中率：{res['overall']:.1f}%",
        f"- 低票组(≤1票)：{res['low_rate']:.1f}%（{res['low_n']} 场，条件③<60%：{'✅' if res['checks']['c3_low_lt60'] else '❌'}）",
        f"- 高票组(≥3票)：{res['high_rate']:.1f}%（{res['high_n']} 场，条件②梯度：{'✅' if res['checks']['c2_gradient'] else '❌'}）",
        "",
        (f"## 结论：✅ 通过——可向用户报批第二层(强制复核)/第三层(双弱区SKIP)上线"
         if ok else
         "## 结论：❌ 未通过——梯度未在新数据复现，通知用户放弃或延长观察"),
        "",
        "> 本文件由 scripts/tie_votes_shadow.py 自动生成，依据 docs/proposal_tiezone_winrate_fix_20260823.md 写死的转正条件。",
    ]
    VERDICT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    FLAG_FILE.write_text(("PASS" if ok else "FAIL") + "\n" + now + "\n", encoding="utf-8")


def main() -> int:
    args = set(sys.argv[1:])
    state = {"slugs": {}}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    known = state.get("slugs", {})
    actuals = load_results_actual()

    by_slug = defaultdict(list)
    for p in sorted(SNAP_DIR.glob("*.json")):
        by_slug[p.name.split("_202")[0]].append(p)

    added = updated = 0
    for slug, paths in sorted(by_slug.items()):
        latest = sorted(paths, key=lambda x: x.stat().st_mtime)[-1]
        mtime = datetime.fromtimestamp(latest.stat().st_mtime, tz=timezone.utc).isoformat()
        rec = known.get(slug)
        if rec and rec.get("snapshot_mtime") == mtime and rec.get("status") == "settled":
            continue
        if rec and rec.get("snapshot_mtime") == mtime and slug in actuals:
            rec["actual"] = actuals[slug]
            rec["status"] = "settled"
            known[slug] = rec
            updated += 1
            continue
        if rec and rec.get("snapshot_mtime") == mtime:
            continue
        try:
            md = json.loads(latest.read_text(encoding="utf-8"))
        except Exception:
            continue
        info = compute_record(md)
        if info is None:
            continue
        info["slug"] = slug
        info["cohort"] = "history_baseline" if (info["settle_day"] or "") <= BASELINE_CUTOFF else "forward"
        if slug in actuals:
            info["actual"] = actuals[slug]
            info["status"] = "settled"
        else:
            info["actual"] = None
            info["status"] = "pending"
        info["snapshot_mtime"] = mtime
        known[slug] = info
        added += 1

    state["slugs"] = known
    state["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    lines = [json.dumps(v, ensure_ascii=False) for _, v in sorted(known.items())]
    OUT_FILE.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    fwd = [v for v in known.values() if v.get("cohort") == "forward"]
    fwd_done = [v for v in fwd if v.get("status") == "settled"]
    base = [v for v in known.values() if v.get("cohort") == "history_baseline"]

    # 转正三条件自动判定：样本达标即落盘结论产物（幂等，结论不变不重写）
    res = evaluate_criteria(fwd_done)
    if res["verdict"] != "pending":
        existing = None
        if FLAG_FILE.exists():
            try:
                existing = FLAG_FILE.read_text(encoding="utf-8").splitlines()[0].strip()
            except Exception:
                existing = None
        if existing != res["verdict"].upper():
            write_verdict(res)
            print(f"★ 贴线区前瞻验证出结论了：{res['verdict'].upper()} → {VERDICT_FILE.name}")

    crit_line = (f"转正三条件: ①前瞻{res['n']}/30 ②梯度{'✅' if res['checks']['c2_gradient'] else '⏳'}"
                 f"(低{res['low_rate'] if res['low_rate'] is not None else '—'}/整体{res['overall'] if res['overall'] is not None else '—'}/高{res['high_rate'] if res['high_rate'] is not None else '—'})"
                 f" ③低票<60%{'✅' if res['checks']['c3_low_lt60'] else '⏳'}")

    if "--summary" in args:
        print(f"基线 {len(base)} 场 | 前瞻已记 {len(fwd)} 场(其中已结算 {len(fwd_done)})")
        print(crit_line)
        if len(fwd_done) >= 10:
            buckets = defaultdict(lambda: [0, 0])
            for v in fwd_done:
                buckets[v["votes"]][0] += 1
                buckets[v["votes"]][1] += 1 if v.get("actual") == v["engine_dir"] else 0
            for k in sorted(buckets):
                n, w = buckets[k]
                print(f"  前瞻 {k}票: {w}/{n}={w/n*100:.0f}%")
        print(f"> 前瞻结算 ≥30 场后重跑 scripts/tiezone_combo_probe_20260823.py 口径复核梯度")
        return 0

    print(f"新增 {added} 条 / 补结算 {updated} 条；总计 {len(known)} 场"
          f"（基线 {len(base)}，前瞻 {len(fwd)}，前瞻已结算 {len(fwd_done)}）")
    print(crit_line)
    print(f"输出 {OUT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
