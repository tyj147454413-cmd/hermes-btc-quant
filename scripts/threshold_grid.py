#!/usr/bin/env python3
"""
Hermes 阈值敏感性网格（C4 / 计划书任务 1.3 补全，2026-08-23）。

背景（审计 P1-10）：
    计划书点名 9 项阈值只覆盖 2 项（贴线/G4）；反转0.23、封顶0.60、Taker分级、墙比、
    H12深度、F1净分、概率0.67 全部未扫。本脚本补齐剩余 7 项（另附赠 G4 偏差阈值复扫）。

方法：
    决策层阈值（反转门槛/F1净分/H12深度/G4偏差/极端墙比/Taker极值）——
    每个网格点生成一个「源码参数化替换」的引擎副本（精确字符串替换+出现次数校验，
    替换失败即报错不静默），对全部已结算快照做两遍 extreme_wall 完整重放，与基准点逐场比对。
    执行层阈值（封顶价/概率上报封顶）——用基准重放的缓存结果后置计算，不动决策。

判定口径（计划书"相邻点表现"准则）：
    ✅稳健   基准点与相邻点胜率差 ≤2pp（数字怎么定都行，不敏感）
    🚨疑似过拟合  基准点比最好的相邻点还高 ≥5pp 且 n≥30（只在这个点上好，旁边都差）
    ➖中性   其余；若整行翻转数=0 另标「网格内零影响」

安全声明：只读快照/results；生产引擎文件零改动（替换发生在内存副本）；
          只写 data/threshold_sensitivity_report*.md。

用法：
    python3 scripts/threshold_grid.py                # 全量网格
    python3 scripts/threshold_grid.py --quick        # 快照抽样 120 场（调试用）
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
RESULTS_FILE = DATA_DIR / "hermes_results.jsonl"
ENGINE_PATH = ROOT / "scripts" / "rule_engine.py"
REPORT_CANONICAL = DATA_DIR / "threshold_sensitivity_report.md"

sys.path.insert(0, str(ROOT / "scripts"))

MAX_PRICE = 0.60          # 现行下单封顶（执行层扫描对象之一）
PROB_CAP = 67.0           # 现行概率上报封顶%（round36 R2）
WILSON_Z = 1.96

# ------------------------------------------------------------------
# 网格定义：key / 名称 / 基准值 / 扫描值 / 引擎源码替换规则
# （常量类：替换赋值行；内联字面量类：精确表达式替换，count 校验防误伤）
# ------------------------------------------------------------------

def _const_patch(line: str, value) -> list[tuple[str, str, int]]:
    return [(line, line.split("=")[0] + f"= {value}", 1)]


GRID_SPEC: list[dict] = [
    {"key": "rev_price_min", "name": "反转价格门槛 REV_PRICE_MIN", "base": 0.23,
     "values": [0.15, 0.20, 0.23, 0.28, 0.35],
     "patches": lambda v: _const_patch("REV_PRICE_MIN = 0.23", v)},
    {"key": "f1_net", "name": "F1 翻转级净分门槛 CW_F1_DOWN_NET", "base": -4.0,
     "values": [-3.0, -4.0, -5.0, -6.0],
     "patches": lambda v: _const_patch("CW_F1_DOWN_NET = -4.0", v)},
    {"key": "h12_depth", "name": "H12 MACD 同向深度 H12_DEPTH_THRESHOLD", "base": 30.0,
     "values": [20.0, 30.0, 40.0, 50.0],
     "patches": lambda v: _const_patch("H12_DEPTH_THRESHOLD = 30.0", v)},
    {"key": "g4_dev", "name": "G4/H9 偏偏差值 G4_DEVIATION", "base": 0.15,
     "values": [0.10, 0.15, 0.20, 0.25],
     "patches": lambda v: _const_patch("G4_DEVIATION = 0.15", v)},
    {"key": "wall_ratio", "name": "极端墙比（买/卖墙）", "base": 3.0,
     "values": [2.5, 3.0, 4.0, 5.0], "wall_thr": True,
     "patches": lambda v: [("_bwr > 3.0", f"_bwr > {v}", 1),
                           ("_swr > 3.0", f"_swr > {v}", 1)]},
    {"key": "taker_ext", "name": "极端 Taker 档（>上 / <下）", "base": (2.33, 0.43),
     "values": [(2.0, 0.5), (2.33, 0.43), (2.67, 0.37)],
     "patches": lambda v: [( "(_tr > 2.33 or _tr < 0.43)",
                             f"(_tr > {v[0]} or _tr < {v[1]})", 1)]},
]

EXEC_GRID = [
    {"key": "price_cap", "name": "下单封顶价", "base": MAX_PRICE,
     "values": [0.50, 0.55, 0.58, 0.60, 0.62, 0.65, 0.70]},
    {"key": "prob_cap", "name": "概率上报封顶%（R2）", "base": PROB_CAP,
     "values": [60.0, 67.0, 72.0, 80.0]},
]


# ------------------------------------------------------------------
# 引擎源码参数化副本
# ------------------------------------------------------------------

_ENGINE_SRC = ENGINE_PATH.read_text(encoding="utf-8")


def make_patched_engine(patches: list[tuple[str, str, int]] | None):
    """生成应用了源码替换的引擎模块副本。patches=None → 原版副本。
    每条 (old, new, expected_count)：old 出现次数 ≠ expected_count 直接抛错（fail-loud）。"""
    src = _ENGINE_SRC
    for old, new, cnt in (patches or []):
        found = src.count(old)
        if found != cnt:
            raise RuntimeError(f"源码替换目标计数不符（{found}≠{cnt}）：{old[:50]}")
        src = src.replace(old, new)
    mod_name = f"rule_engine_grid_{abs(hash(src)) % 10_000_000}"
    mod = types.ModuleType(mod_name)
    mod.__file__ = str(ENGINE_PATH)
    # @dataclass 装饰器运行时会查 sys.modules[cls.__module__]，必须先注册
    sys.modules[mod_name] = mod
    exec(compile(src, str(ENGINE_PATH), "exec"), mod.__dict__)
    return mod


# ------------------------------------------------------------------
# 数据加载与缓存
# ------------------------------------------------------------------

def wilson(w: int, n: int, z: float = WILSON_Z) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = w / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * (p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5
    return max(0.0, (center - margin) / denom * 100), min(100.0, (center + margin) / denom * 100)


def load_settled(limit: int | None = None):
    """已结算 slug → actual；并缓存每份最新快照的因子提取结果。"""
    results = {}
    if RESULTS_FILE.exists():
        for line in RESULTS_FILE.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("slug") and r.get("actual") in ("UP", "DOWN"):
                results[r["slug"]] = r["actual"]

    slugs: dict[str, list[Path]] = {}
    for p in SNAPSHOT_DIR.glob("*.json"):
        slugs.setdefault(p.name.split("_202")[0], []).append(p)

    from signal_validator import extract_factors

    cache = []
    for slug in sorted(results):
        paths = slugs.get(slug)
        if not paths:
            continue
        path = sorted(paths, key=lambda x: x.stat().st_mtime)[-1]
        try:
            md = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        try:
            factors, inp, _notes, _mode = extract_factors(md)
        except Exception:
            continue
        rec = {
            "slug": slug, "actual": results[slug],
            "inp": dict(inp),
            "buy_wall": inp.get("_buy_wall_ratio"),
            "sell_wall": inp.get("_sell_wall_ratio"),
            "taker_bs": inp.get("taker_bs_ratio"),
            "pm_up": inp.get("pm_up"),
            # C4审计修复(🟡6): 缓存真实 DOWN 价，缺省才退互补式 100-pm_up（与 wf_gate 口径对齐）
            "pm_down": inp.get("pm_down"),
            "regime": inp.get("regime") or "?",
        }
        cache.append(rec)
        if limit and len(cache) >= limit:
            break
    return cache


# ------------------------------------------------------------------
# 重放（与 replay_common.replay_judge_full 完全同口径，墙比参数化）
# ------------------------------------------------------------------

def decide(engine_mod, rec: dict, wall_thr: float = 3.0):
    inp = dict(rec["inp"])
    inp.pop("_buy_wall_ratio", None)
    inp.pop("_sell_wall_ratio", None)
    inp.pop("taker_bs_ratio", None)
    prelim = engine_mod.judge(dict(inp, extreme_wall=False))
    if prelim.final_direction == "DOWN":
        ext = bool(rec["sell_wall"] is not None and rec["sell_wall"] > wall_thr)
    elif prelim.final_direction == "UP":
        ext = bool(rec["buy_wall"] is not None and rec["buy_wall"] > wall_thr)
    else:
        ext = False
    d = engine_mod.judge(dict(inp, extreme_wall=ext,
                              _buy_wall_ratio=rec["buy_wall"],
                              _sell_wall_ratio=rec["sell_wall"],
                              taker_bs_ratio=rec["taker_bs"]))
    return d if d.final_direction else None


def run_point(cache: list[dict], engine_mod, wall_thr: float = 3.0):
    """返回 [(side, win, ask)|None]（ask>封顶或无方向 → None 不成交）。"""
    out = []
    for rec in cache:
        d = decide(engine_mod, rec, wall_thr)
        if d is None:
            out.append(None)
            continue
        side = d.final_direction
        ask = None
        # C4审计修复(🟡6): DOWN 优先真实 pm_down，缺省退互补式（消除与 wf_gate 的口径漂移）
        if side == "UP" and rec["pm_up"] is not None:
            ask = rec["pm_up"] / 100.0
        elif side == "DOWN":
            if rec.get("pm_down") is not None:
                ask = rec["pm_down"] / 100.0
            elif rec["pm_up"] is not None:
                ask = (100 - rec["pm_up"]) / 100.0
        if ask is None or ask <= 0 or ask > MAX_PRICE:
            out.append(None)
            continue
        out.append((side, side == rec["actual"], ask))
    return out


def metrics(trades: list) -> dict:
    n = sum(1 for t in trades if t)
    w = sum(1 for t in trades if t and t[1])
    pnl = sum((1 - t[2]) if t[1] else -t[2] for t in trades if t)
    lo, hi = wilson(w, n)
    return {"n": n, "w": w, "wr": round(w / n * 100, 1) if n else 0.0,
            "wilson_lo": round(lo, 1), "wilson_hi": round(hi, 1),
            "ev": round(pnl / n, 4) if n else 0.0}


def classify(base_wr: float, neighbor_wrs: list[float], flips: int, n: int) -> str:
    """计划书准则：只在一个点上好旁边都差 → 疑似过拟合。"""
    if flips == 0:
        return "⚪网格内零影响"
    valid = [x for x in neighbor_wrs]
    if not valid:
        return "➖中性"
    if abs(base_wr - max(valid)) <= 2.0 and abs(base_wr - min(valid)) <= 2.0:
        return "✅稳健"
    if base_wr - max(valid) >= 5.0 and n >= 30:
        return "🚨疑似过拟合(尖峰)"
    return "➖中性"


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="阈值敏感性网格（C4/任务1.3）")
    ap.add_argument("--quick", action="store_true", help="抽样前 120 场（调试）")
    ap.add_argument("--archive", action="store_true", help="额外保存带时间戳的归档副本")
    args = ap.parse_args()

    ts = datetime.now(timezone.utc)
    cache = load_settled(limit=120 if args.quick else None)
    n_total = len(cache)
    print(f"已结算样本: {n_total}｜决策层阈值 {len(GRID_SPEC)} 项 + 执行层 {len(EXEC_GRID)} 项")

    # ---- 基准（现行引擎原版） ----
    base_engine = make_patched_engine(None)
    baseline = run_point(cache, base_engine, wall_thr=3.0)
    bm = metrics(baseline)
    print(f"基准: 成交{bm['n']} 胜率{bm['wr']}% EV{bm['ev']*100:+.1f}% "
          f"(Wilson [{bm['wilson_lo']}, {bm['wilson_hi']}])")

    lines = [
        "# 阈值敏感性网格报告（C4 / 计划书任务 1.3 补全）",
        "",
        f"- 生成(UTC)：{ts.isoformat()}",
        f"- 样本：{n_total} 已结算｜基准：成交{bm['n']} 胜率{bm['wr']}% EV{bm['ev']*100:+.1f}%",
        "- 方法：每个网格点 = 参数化引擎副本 × 全样本两遍 extreme_wall 重放；"
        "执行层阈值在基准决策上后置计算。",
        "- 判定：✅稳健=与相邻点差≤2pp｜🚨疑似过拟合=基准比最好相邻点高≥5pp且n≥30｜⚪=网格内零影响",
        "",
    ]
    summary_flags: list[str] = []

    for spec in GRID_SPEC:
        name, key = spec["name"], spec["key"]
        wall_thr = spec["base"] if spec.get("wall_thr") else 3.0
        rows = []
        base_idx = spec["values"].index(spec["base"])
        for i, v in enumerate(spec["values"]):
            if v == spec["base"]:
                trades = baseline
            else:
                eng = make_patched_engine(spec["patches"](v))
                wt = v if spec.get("wall_thr") else 3.0
                trades = run_point(cache, eng, wall_thr=wt)
            m = metrics(trades)
            flips = sum(1 for a, b in zip(baseline, trades)
                        if (a and b and a[0] != b[0]) or (a is None) != (b is None))
            rows.append({"v": v, "m": m, "flips": flips})
            mark = "←现行" if v == spec["base"] else ""
            print(f"  [{key}] {v}: n={m['n']} wr={m['wr']}% EV={m['ev']*100:+.1f}% "
                  f"翻转{flips} {mark}")
        # 相邻点 = 基准值左右各一档
        neigh = [rows[j]["m"]["wr"] for j in (base_idx - 1, base_idx + 1)
                 if 0 <= j < len(rows)]
        verdict = classify(rows[base_idx]["m"]["wr"], neigh,
                           sum(r["flips"] for r in rows), bm["n"])
        if verdict.startswith("🚨"):
            summary_flags.append(f"{name}({spec['base']})")
        lines += [f"## {name}（现行 {spec['base']}）→ {verdict}", "",
                  "| 取值 | 成交n | 胜率 | Wilson95 | EV/单 | 对基准翻转场数 | 备注 |",
                  "|---:|---:|---:|---|---:|---:|---|"]
        for r in rows:
            m = r["m"]
            lines.append(f"| {r['v']} | {m['n']} | {m['wr']}% "
                         f"| [{m['wilson_lo']}, {m['wilson_hi']}] "
                         f"| {m['ev']*100:+.1f}% | {r['flips']} | "
                         f"{'←现行' if r['v'] == spec['base'] else ''} |")
        lines.append("")

    # ---- 执行层：封顶价 ----
    lines += ["## 下单封顶价（执行层，基准决策不变）", "",
              "| 封顶 | 成交n | 胜率 | Wilson95 | EV/单 | 备注 |", "|---:|---:|---:|---|---:|---|"]
    for cap in EXEC_GRID[0]["values"]:
        trades = [t for t in baseline if t and t[2] <= cap]
        m = metrics([t if t else None for t in trades])
        lines.append(f"| {cap:.2f} | {m['n']} | {m['wr']}% | [{m['wilson_lo']}, {m['wilson_hi']}] "
                     f"| {m['ev']*100:+.1f}% | {'←现行' if abs(cap - MAX_PRICE) < 1e-9 else ''} |")
    lines.append("")

    # ---- 执行层：概率上报封顶（校准质量） ----
    prob_recs = []
    for rec, t in zip(cache, baseline):
        d = decide(base_engine, rec, 3.0)
        if d is None or d.probability is None:
            continue
        prob_recs.append((d.probability, rec["actual"] == d.final_direction))
    lines += ["## 概率上报封顶%（报告层，R2 校准质量）", "",
              "| 封顶% | 被截断场数 | 截断占比 | 上报MAE(pp，越低越诚实) | 备注 |",
              "|---:|---:|---:|---:|---|"]
    for cap in EXEC_GRID[1]["values"]:
        clipped = sum(1 for p, _ in prob_recs if p > cap)
        mae = (sum(abs(min(p, cap) - (100.0 if win else 0.0)) for p, win in prob_recs)
               / max(len(prob_recs), 1))
        lines.append(f"| {cap:.0f} | {clipped} | {clipped/max(len(prob_recs),1)*100:.1f}% "
                     f"| {mae:.1f} | {'←现行' if abs(cap - PROB_CAP) < 1e-9 else ''} |")
    lines.append("")

    lines += ["## 结论速览", ""]
    if summary_flags:
        lines += [f"- 🚨 疑似过拟合阈值：{'、'.join(summary_flags)}——建议降权或收紧使用场景"]
    else:
        lines += ["- ✅ 未发现尖峰型（疑似过拟合）阈值；各阈值对相邻取值不敏感或影响可解释"]
    lines += ["- ⚪ 标注「零影响」的阈值：历史样本中触发极少，网格无法评估——保持现状并继续监控触发率",
              "", "> 本报告为研究参考，不改任何现行规则；如需调整阈值须走 SYNC_MAP 主版本流程（附 WF 报告）。", ""]

    REPORT_CANONICAL.write_text("\n".join(lines), encoding="utf-8")
    if args.archive:
        arch = DATA_DIR / f"threshold_sensitivity_report_{ts.strftime('%Y%m%d_%H%M')}.md"
        shutil.copyfile(REPORT_CANONICAL, arch)
        print(f"归档: {arch}")
    print(f"\n=== 完成 === 报告: {REPORT_CANONICAL}")
    if summary_flags:
        print(f"🚨 疑似过拟合: {summary_flags}")
    else:
        print("✅ 无尖峰型阈值")
    return 0


if __name__ == "__main__":
    sys.exit(main())
