#!/usr/bin/env python3
"""
Hermes WF 关卡（C3 / 审计 P1-7 制度化，2026-08-23）。

背景（审计 P1-7 原文）：
    "上线必须过 walk-forward"此前零流程强制（全项目无引用）；walk_forward.py 的候选集
    是 7 个玩具策略，生产 Hermes 引擎不在其中 → WF gate 不住真实规则变更；
    OOS 仅 15 单即上了 LIVE。

本脚本补两个强制点：
    A. 候选集 = walk_forward.CANDIDATES(7 玩具基线) + **production_engine**
       （replay_common.replay_judge_full 对每份快照做两遍 extreme_wall 引擎复算，
        与 regime_split_replay.py 同源——规则/引擎改动即反映为该候选的 OOS 指标变化）
    B. 每次运行产出带唯一编号的报告：data/wf_reports/WF-<时间>-<指纹>.md + index.jsonl。
       check_rule_anchor.sh 与 daily_health_check 按「当前主版本是否有 WF 报告」做机器检查：
       **主版本变更（round NN→MM）前必须先跑本脚本拿到报告编号，才能登记 SYNC_MAP。**

判定口径（v1，改动须同步本注释与报告模板）：
    PASS         OOS n≥30 且 Wilson95 下界 > 50% 且 OOS EV > 0
    CONDITIONAL  n≥15 且 OOS EV > 0（样本不足下结论，但方向为正）
    FAIL         其余（含样本不足）

诚实边界（报告脚注固定声明）：
    静态规则无参数拟合，严格意义的"样本外"不存在；本关卡口径 = 时间分段稳健性 +
    与玩具基线对照 + 全量留痕。它的价值是**强制留痕与衰减可见**，不是证明因果。

安全声明：只读快照/features/results；只写 data/wf_reports/*。

用法：
    python3 scripts/wf_gate.py run [--train 120 --test 40] [--note "..."]
    python3 scripts/wf_gate.py check --latest
    python3 scripts/wf_gate.py check --report WF-20260823-1234-ab12
    python3 scripts/wf_gate.py check --latest --require-pass production_engine
    python3 scripts/wf_gate.py list

退出码（check 子命令）：0=通过｜3=报告缺失/损坏｜4=--require-pass 未达标
"""

from __future__ import annotations

import argparse
try:
    import fcntl
except ImportError:  # 9/2: Windows 无 fcntl，append_index 降级无锁
    fcntl = None
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REPORT_DIR = DATA_DIR / "wf_reports"
INDEX_FILE = REPORT_DIR / "index.jsonl"
PROMPT_EXEC = ROOT / "prompt_exec.md"

# 判定阈值（v1 冻结；调整 = 规则变更，本身应附新 WF 报告说明）
PASS_MIN_N = 30
COND_MIN_N = 15

_ANCHOR_RE = re.compile(r"round(\d+)(?:\.(\d+))?")


# ====================================================================
# 版本锚点（与 daily_health_check._anchor_main_version 同口径，勿单方漂移）
# ====================================================================

def current_major(path: Path | None = None) -> str | None:
    """prompt_exec.md 版本锚点行 → 主版本号字符串（round41.4 → "41"）。"""
    p = path or PROMPT_EXEC
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if "版本锚点" in line:
            m = _ANCHOR_RE.search(line)
            return m.group(1) if m else None
    return None


# ====================================================================
# 报告索引（index.jsonl）
# ====================================================================

def load_index(path: Path | None = None) -> list[dict]:
    p = path or INDEX_FILE
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def append_index(entry: dict, path: Path | None = None) -> None:
    """追加一条索引记录（fcntl 排他锁防并发写坏）。"""
    p = path or INDEX_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
        finally:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def file_md5(path: Path) -> str | None:
    try:
        return hashlib.md5(path.read_bytes()).hexdigest()[:12]
    except OSError:
        return None


def make_report_id(ts: datetime, seed: str) -> str:
    h = hashlib.md5(seed.encode("utf-8")).hexdigest()[:4]
    return f"WF-{ts.strftime('%Y%m%d-%H%M')}-{h}"


def verdict(n: int, wilson_lo: float, ev: float) -> str:
    """关卡判定（口径见模块 docstring，v1 冻结）。"""
    if n >= PASS_MIN_N and wilson_lo > 50.0 and ev > 0:
        return "PASS"
    if n >= COND_MIN_N and ev > 0:
        return "CONDITIONAL"
    return "FAIL"


def gate_status_for_major(major: str | None = None,
                          index_path: Path | None = None,
                          prompt_path: Path | None = None) -> tuple[bool, str]:
    """轻量状态查询（供 daily_health_check 第 11 项调用）：当前主版本是否有 WF 报告。"""
    mj = major or current_major(prompt_path)
    if not mj:
        return False, "WF关卡: prompt_exec.md 主版本锚点解析失败"
    entries = load_index(index_path)
    if not entries:
        return False, "WF关卡: 无任何 WF 报告（主版本变更前须先跑 scripts/wf_gate.py run）"
    hits = [e for e in entries if str(e.get("for_major")) == str(mj)]
    latest = entries[-1]
    if hits:
        ok_detail = (f"WF关卡: 主版本{mj}已有 {len(hits)} 份WF报告｜"
                     f"最新 {latest.get('id')} 覆盖主版本 {latest.get('for_major')}")
        # 报告文件被删也算失守
        missing = [e["id"] for e in hits[-3:]
                   if e.get("id") and not (REPORT_DIR / f"{e['id']}.md").exists()]
        if missing:
            return False, f"WF关卡: 报告文件丢失 {missing}"
        return True, ok_detail
    return False, (f"WF关卡: ❌主版本{mj}无WF报告(最新 {latest.get('id')} 只覆盖主版本 "
                   f"{latest.get('for_major')})——主版本变更登记前须先跑 wf_gate run")


# ====================================================================
# run：滚动验证 + 留痕（重路径，pandas/引擎懒加载）
# ====================================================================

def build_production_sides(lab_slugs: list[str]) -> dict[str, str]:
    """对已结算样本逐份快照跑生产引擎复算，返回 slug→side 映射。

    与 regime_split_replay.py 同源（replay_common.replay_judge_full 两遍 extreme_wall）。
    引擎/规则任何改动都会改变本映射 → production_engine 候选的 OOS 指标随之变化。
    """
    from replay_common import get_latest_snapshots, replay_judge_full  # 懒加载引擎依赖

    slugs = set(lab_slugs)
    sides: dict[str, str] = {}
    snaps = get_latest_snapshots()
    for slug, path in snaps.items():
        if slug not in slugs:
            continue
        try:
            md = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        replay = replay_judge_full(md)
        if replay is None:
            continue
        d, _factors, _inp = replay
        if d.final_direction in ("UP", "DOWN"):
            sides[slug] = d.final_direction
    return sides


def cmd_run(args: argparse.Namespace) -> int:
    import pandas as pd  # noqa: F401  (iterrows 判 NaN 用；懒加载保持 import 轻量)
    import walk_forward as wfl  # 懒加载 pandas + 玩具候选集

    def eval_fn(rows, fn):
        """与 walk_forward.eval_trades 完全同口径（ask≤MAX_PRICE、每单 1 share），
        但直接接受判定函数——使 production_engine 与玩具候选走同一条经济口径。"""
        n = w = 0
        pnl = 0.0
        for _, r in rows.iterrows():
            side = fn(r)
            if side not in ("UP", "DOWN"):
                continue
            ask = r["pm_up"] if side == "UP" else r["pm_down"]
            if pd.isna(ask) or ask <= 0 or ask > wfl.MAX_PRICE:
                continue
            if r["actual"] not in ("UP", "DOWN"):
                continue
            n += 1
            if r["actual"] == side:
                w += 1
                pnl += 1 - ask
            else:
                pnl -= ask
        return n, w, pnl

    df = wfl.load_df()
    lab = df[df["actual"].isin(["UP", "DOWN"])].reset_index(drop=True)
    n_total = len(lab)

    # 生产引擎接入候选集（审计 P1-7 要求①）：只决定方向，定价口径与玩具候选一致(eval_trades 内 ask≤0.60)
    prod_sides = build_production_sides(lab["slug"].tolist())
    candidates: dict[str, object] = dict(wfl.CANDIDATES)
    candidates["production_engine"] = lambda r: prod_sides.get(r["slug"])

    names = list(candidates.keys())
    print(f"已结算样本: {n_total}｜train={args.train} test={args.test}｜"
          f"候选集({len(names)}): {', '.join(names)}")
    print(f"production_engine 重放命中: {len(prod_sides)}/{n_total} 场")

    # 逐折滚动：每个候选在同一批 test 窗上独立累积 OOS（关卡要的是各自成绩，不是择优）
    oos = {nm: {"n": 0, "w": 0, "pnl": 0.0} for nm in names}
    folds_prod: list[dict] = []
    i = args.train
    fold_idx = 0
    while i + args.test <= n_total:
        tr = lab.iloc[i - args.train:i]
        te = lab.iloc[i:i + args.test]
        fold_idx += 1
        for nm in names:
            tn, tw_, tpnl = eval_fn(te, candidates[nm])
            oos[nm]["n"] += tn
            oos[nm]["w"] += tw_
            oos[nm]["pnl"] += tpnl
            if nm == "production_engine":
                folds_prod.append({"fold": fold_idx, "train_end": tr.iloc[-1]["slug"],
                                   "oos_n": tn, "oos_w": tw_,
                                   "oos_wr": round(tw_ / tn * 100, 1) if tn else 0.0,
                                   "oos_pnl": round(tpnl, 2)})
        i += args.test

    # 全样本参考（静态规则的 IS 代理）+ 汇总判定
    ts_now = datetime.now(timezone.utc)
    summary: dict[str, dict] = {}
    for nm in names:
        fn_full_n, fn_full_w, fn_full_pnl = eval_fn(lab, candidates[nm])
        full_ev = fn_full_pnl / fn_full_n if fn_full_n else 0.0
        on, ow, opnl = oos[nm]["n"], oos[nm]["w"], oos[nm]["pnl"]
        lo, hi = wfl.wilson(ow, on)
        oos_ev = opnl / on if on else 0.0
        summary[nm] = {
            "full_n": fn_full_n, "full_wr": round(fn_full_w / fn_full_n * 100, 1) if fn_full_n else 0.0,
            "full_ev": round(full_ev, 4),
            "oos_n": on, "oos_w": ow,
            "oos_wr": round(ow / on * 100, 1) if on else 0.0,
            "wilson95": [round(lo, 1), round(hi, 1)],
            "oos_ev": round(oos_ev, 4),
            "decay_pp": round((full_ev - oos_ev) * 100, 1),
            "verdict": verdict(on, lo, oos_ev),
        }

    report_id = make_report_id(
        ts_now, f"{args.train}/{args.test}/{n_total}/{len(prod_sides)}/{ts_now.isoformat()}")

    # 数据指纹（provenance：换数据集后旧报告不可比）
    feats_csv = wfl.FEATURES_CSV
    fingerprint = {
        "features_csv": feats_csv.name if feats_csv.exists() else None,
        "features_rows": int(n_total),
        "features_mtime": (datetime.fromtimestamp(feats_csv.stat().st_mtime, timezone.utc).isoformat()
                           if feats_csv.exists() else None),
        "snapshots_matched": len(prod_sides),
    }

    # ---------- 写报告 ----------
    lines = [
        f"# Walk-forward 关卡报告 {report_id}",
        "",
        f"- 生成(UTC)：{ts_now.isoformat()}",
        f"- 规则主版本：round{current_major() or '?'}（for_major 记录见 index.jsonl）",
        f"- 参数：train={args.train} test={args.test}｜完成折数：{fold_idx}｜已结算样本：{n_total}",
        f"- 数据指纹：features_rows={fingerprint['features_rows']} "
        f"mtime={fingerprint['features_mtime']} snapshots_matched={fingerprint['snapshots_matched']}",
        f"- 备注：{args.note or '—'}",
        "",
        "## 候选汇总（OOS = 各 test 窗独立累积，非择优链）",
        "",
        "| 候选 | 全量n | 全量胜率 | 全量EV | OOS n | OOS 胜率 | Wilson95 | OOS EV | 衰减(pp) | 判定 |",
        "|---|---:|---:|---:|---:|---:|---|---:|---:|---|",
    ]
    order = ["production_engine"] + [x for x in names if x != "production_engine"]
    for nm in order:
        s = summary[nm]
        wl, wh = s["wilson95"]
        mark = {"PASS": "✅", "CONDITIONAL": "🟡", "FAIL": "❌"}[s["verdict"]]
        lines.append(f"| {nm} | {s['full_n']} | {s['full_wr']}% | {s['full_ev']*100:+.1f}% "
                     f"| {s['oos_n']} | {s['oos_wr']}% | [{wl:.1f}, {wh:.1f}] "
                     f"| {s['oos_ev']*100:+.1f}% | {s['decay_pp']:+.1f} | {mark} {s['verdict']} |")

    lines += ["", "## production_engine 逐折明细", "",
              "| 折 | 训练窗止 | OOS n | OOS 胜 | OOS 胜率 | OOS P&L/share |",
              "|---:|---|---:|---:|---:|---:|"]
    for f_ in folds_prod:
        lines.append(f"| {f_['fold']} | {f_['train_end'][-28:]} | {f_['oos_n']} "
                     f"| {f_['oos_w']} | {f_['oos_wr']}% | {f_['oos_pnl']:+.2f} |")

    lines += [
        "",
        "## 判定口径（v1 冻结）",
        "",
        f"- PASS：OOS n≥{PASS_MIN_N} 且 Wilson95 下界>50% 且 OOS EV>0",
        f"- CONDITIONAL：OOS n≥{COND_MIN_N} 且 OOS EV>0（样本不足以定论，方向为正）",
        "- FAIL：其余（含样本不足）",
        "",
        "> ⚠️ 诚实边界：候选为静态规则、无参数拟合，严格「样本外」不存在。",
        "> 本关卡的制度价值 = **主版本变更强制留痕 + 与玩具基线对照 + 时间分段衰减可见**，不构成因果证明。",
        "> 主版本升级登记 SYNC_MAP 时必须引用本报告编号（审计 P1-7 要求②）。",
        "",
    ]

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    rp = REPORT_DIR / f"{report_id}.md"
    rp.write_text("\n".join(lines), encoding="utf-8")
    md5 = file_md5(rp) or ""

    entry = {
        "id": report_id,
        "ts_utc": ts_now.isoformat(),
        "for_major": current_major(),
        "train": args.train,
        "test": args.test,
        "samples": int(n_total),
        "folds": fold_idx,
        "production_hits": len(prod_sides),
        "fingerprint": fingerprint,
        "note": args.note or "",
        "md5": md5,
        "candidates": summary,
    }
    append_index(entry)

    # ---------- 控制台摘要 ----------
    print("\n=== WF 关卡结果 ===")
    for nm in order:
        s = summary[nm]
        print(f"  {nm:20s} OOS {s['oos_n']:3d}单 胜率{s['oos_wr']:5.1f}% "
              f"EV{s['oos_ev']*100:+6.1f}% → {s['verdict']}")
    print(f"\n报告编号: {report_id}（主版本 round{entry['for_major']}）")
    print(f"文件: {rp}")
    print(f"登记 SYNC_MAP 时引用本编号。")
    return 0


# ====================================================================
# check / list：核对留痕（放行口）
# ====================================================================

def check_entry(entry: dict, require_pass: str | None = None) -> tuple[bool, str]:
    """校验一条索引记录：文件存在 + md5 一致 + （可选）指定候选 PASS。"""
    rid = entry.get("id", "")
    rp = REPORT_DIR / f"{rid}.md"
    if not rp.exists():
        return False, f"❌ 报告文件缺失: {rp}"
    got = file_md5(rp)
    want = entry.get("md5")
    if want and got != want:
        return False, f"❌ 报告 md5 不一致({rid}): index={want} 实际={got}（文件被改动？）"
    cands = entry.get("candidates", {})
    pe = cands.get("production_engine")
    msg = (f"✅ {rid}｜主版本{entry.get('for_major')}｜"
           f"production_engine: OOS {pe.get('oos_n')}单 胜率{pe.get('oos_wr')}% "
           f"EV{pe.get('oos_ev', 0)*100:+.1f}% → {pe.get('verdict')}" if pe
           else f"✅ {rid}（无 production_engine 字段——旧版报告）")
    if require_pass:
        c = cands.get(require_pass)
        if not c:
            return False, f"❌ {rid} 无候选 {require_pass} 的记录"
        if c.get("verdict") != "PASS":
            return False, (f"❌ {rid} 候选 {require_pass} 未达 PASS"
                           f"(verdict={c.get('verdict')})——不放行")
        return True, msg + f"｜require-pass[{require_pass}]=PASS ✓"
    return True, msg


def cmd_check(args: argparse.Namespace) -> int:
    entries = load_index()
    if not entries:
        print("❌ index.jsonl 为空——先跑 python3 scripts/wf_gate.py run")
        return 3
    if args.report:
        entry = next((e for e in entries if e.get("id") == args.report), None)
        if entry is None:
            print(f"❌ 找不到报告 {args.report}")
            return 3
    else:
        entry = entries[-1]
    ok, msg = check_entry(entry, require_pass=args.require_pass)
    print(msg)
    st_ok, st_detail = gate_status_for_major()
    print(st_detail)
    if not ok:
        return 3 if "缺失" in msg or "不一致" in msg or "找不到" in msg else 4
    if args.require_pass and "require-pass" not in msg:
        return 4
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    entries = load_index()
    if not entries:
        print("(无任何 WF 报告)")
        return 0
    print(f"共 {len(entries)} 份 WF 报告：")
    for e in entries:
        pe = (e.get("candidates") or {}).get("production_engine") or {}
        print(f"- {e.get('id')}｜主版本{e.get('for_major')}｜{e.get('ts_utc', '')[:19]}｜"
              f"samples={e.get('samples')}｜production_engine={pe.get('verdict', '?')}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="WF 关卡（C3 制度化）：run=跑并留痕 / check=核对 / list")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="滚动验证全部候选（含 production_engine）并写报告")
    p_run.add_argument("--train", type=int, default=120)
    p_run.add_argument("--test", type=int, default=40)
    p_run.add_argument("--note", default="", help="本次运行备注（如'round41.4 变更前基线'）")
    p_run.set_defaults(fn=cmd_run)

    p_chk = sub.add_parser("check", help="核对报告留痕（cron/流程用退出码判断）")
    p_chk.add_argument("--report", default=None, help="报告编号（缺省=最新一份）")
    p_chk.add_argument("--latest", action="store_true", help="取最新一份（默认行为，占位显式化）")
    p_chk.add_argument("--require-pass", default=None,
                       help="要求指定候选 verdict=PASS 才退出 0（如 production_engine）")
    p_chk.set_defaults(fn=cmd_check)

    p_ls = sub.add_parser("list", help="列出全部报告")
    p_ls.set_defaults(fn=cmd_list)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
