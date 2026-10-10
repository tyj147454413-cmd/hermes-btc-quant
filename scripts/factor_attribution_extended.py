#!/usr/bin/env python3
"""扩展归因脚本 — 把沉睡字段扫描出的 7 个 ≥70% 候选外挂式接入归因统计

设计原则：
  ① 不动 signal_validator.extract_factors() 主逻辑（S₀ 评分表零变更）
  ② 外挂读取快照字段，把候选方向投票并入 factors dict（独立命名空间 ext_*）
  ③ 复用 factor_attribution.py 的全套统计/切片/基线对照逻辑
  ④ 附加：7 候选 vs 现有13 因子的触发相关性矩阵（独立性快速检验）

运行：python3 scripts/factor_attribution_extended.py
输出：
  - 控制台摘要（现有因子 + 候选因子对比表 + 独立性矩阵）
  - data/hermes_factor_stats_extended_<YYYYMMDD>.json（全量机器可读）
  - data/hermes_factor_attribution_extended_<YYYYMMDD>.md（人读报告）

用法对比 factor_attribution.py：
  - factor_attribution.py = 主归因（19 个引擎机算因子）
  - factor_attribution_extended.py = 主归因 + 7 候选外挂（不污染 S₀）
"""
from __future__ import annotations
import argparse
import json
import math
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from factor_attribution import (
    load_results,
    load_latest_snapshot_map,
    hit,
    sign,
    slug_of_snapshot,
    FACTOR_LABELS,
)
from signal_validator import extract_factors  # noqa: E402

DATA = ROOT / "data"
RESULTS_FILE = DATA / "hermes_results.jsonl"
SNAPSHOT_DIR = DATA / "snapshots"

# ============================================================
# 7 个外挂候选（来自 scan_dormant_factors.py 命中率 ≥70%）
# 命名空间前缀 ext_ 避免与主因子键冲突
# ============================================================
def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def _path_get(d, path):
    """简化版路径解析: a.b[0].c"""
    cur = d
    parts = re.split(r'\.|\[(\d+)\]', path)
    for p in parts:
        if not p:
            continue
        if isinstance(cur, dict):
            cur = cur.get(p)
        elif isinstance(cur, list):
            try:
                cur = cur[int(p)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if cur is None:
            return None
    return cur

EXTRA_FACTOR_DEFS = [
    {
        "key": "ext_rsi_1m_momentum",
        "label": "1m.rsi 顺势",
        "family": "趋势类",
        "path": "timeframes.1m.rsi",
        "fn": lambda v: 1 if (v is not None and v >= 70) else (-1 if (v is not None and v <= 30) else 0),
        "note": "RSI>=70=+1(顺势续涨), RSI<=30=-1(顺势续跌); 动量延续市场铁证",
        "category": "trend_candidate",
    },
    {
        "key": "ext_rsi_5m_momentum",
        "label": "5m.rsi 顺势",
        "family": "趋势类",
        "path": "timeframes.5m.rsi",
        "fn": lambda v: 1 if (v is not None and v >= 70) else (-1 if (v is not None and v <= 30) else 0),
        "note": "同上(5m 视角, 顺势判据)",
        "category": "trend_candidate",
    },
    {
        "key": "ext_rsi_15m_momentum",
        "label": "15m.rsi 顺势",
        "family": "趋势类",
        "path": "timeframes.15m.rsi",
        "fn": lambda v: 1 if (v is not None and v >= 70) else (-1 if (v is not None and v <= 30) else 0),
        "note": "同上(15m 视角, 顺势判据)",
        "category": "trend_candidate",
    },
    {
        "key": "ext_book_imbalance",
        "label": "PM 盘口不平衡",
        "family": "PM类",
        "path": "polymarket.book_imbalance",
        "fn": lambda v: sign(v) if v is not None else 0,
        "note": "PM 盘口不平衡 sign(imb) -1..+1; 可能与 PM 动量共线, 待独立性检验",
        "category": "pm_candidate",
    },
    {
        "key": "ext_book_imbalance_strong",
        "label": "PM 盘口极强不平衡",
        "family": "PM类",
        "path": "polymarket.book_imbalance",
        "fn": lambda v: 1 if (v is not None and v > 0.5) else (-1 if (v is not None and v < -0.5) else 0),
        "note": "盘口不平衡 >0.5 极强信号; 与 ext_book_imbalance 可能共线",
        "category": "pm_candidate",
    },
    {
        "key": "ext_pm_mark_book_skew",
        "label": "PM 标记价失真方向",
        "family": "PM类",
        "path": "polymarket",
        "fn": lambda v: 1 if (isinstance(v, dict) and v.get("book_up_mid") and v.get("up_price") and v.get("book_up_mid") > v.get("up_price")) else (-1 if (isinstance(v, dict) and v.get("book_up_mid") and v.get("up_price") and v.get("book_up_mid") < v.get("up_price")) else 0),
        "note": "book > mark 多头失真, book < mark 空头失真; 与 ext_book_imbalance 共线预测",
        "category": "pm_candidate",
    },
    {
        "key": "ext_price_1h_change",
        "label": "上小时价格变化方向",
        "family": "OI/惯性类",
        "path": "oi_price_relation.price_1h_change_pct",
        "fn": lambda v: sign(v) if v is not None else 0,
        "note": "来自 oi_price_relation, 与 prev_hour.change_pct 数据源不同但语义重叠",
        "category": "inertia_candidate",
    },
    # ========== 60-70% 子集补充（多观察项，迟早验证）==========
    {
        "key": "ext_v_reversal_15m",
        "label": "15m V 反检测",
        "family": "形态类",
        "path": "v_reversal_15m.detected",
        "fn": lambda v: 1 if v is True else 0,
        "note": "v_reversal_15m.detected 真值=+1; 与 AI 盘感 fake_breakout/divergence 局部重叠",
        "category": "形态_candidate",
    },
    {
        "key": "ext_macd_hist_5m",
        "label": "5m MACD hist",
        "family": "趋势类",
        "path": "timeframes.5m.macd_hist",
        "fn": lambda v: sign(v) if v is not None else 0,
        "note": "5m MACD hist 直接方向; 15m 已废除计分, 5m 是新视角",
        "category": "trend_candidate",
    },
    {
        "key": "ext_macd_accel_15m",
        "label": "15m MACD acceleration",
        "family": "趋势类",
        "path": "timeframes.15m.macd_acceleration",
        "fn": lambda v: sign(v) if v is not None else 0,
        "note": "MACD 加速方向 = hist 变化率; 4H acceleration 已在盘感默认 ×0",
        "category": "trend_candidate",
    },
    {
        "key": "ext_dev_trend_accel",
        "label": "偏差 3min 加速度",
        "family": "动量类",
        "path": "deviation_trend.last_3min_dev_pct",
        "fn": lambda v: sign(v[-1] - v[0]) if isinstance(v, list) and len(v) >= 2 else 0,
        "note": "近 3min 偏差加速度; 价格位置 + momentum_burst 之外的第三维度",
        "category": "trend_candidate",
    },
]

EXTRA_FACTOR_KEYS = [d["key"] for d in EXTRA_FACTOR_DEFS]

def compute_extra_factors(md: dict) -> dict[str, float]:
    """外挂计算所有候选因子方向投票,返回 {ext_*: ±1/0}"""
    out = {}
    for d in EXTRA_FACTOR_DEFS:
        try:
            raw = _path_get(md, d["path"])
            out[d["key"]] = float(d["fn"](raw))
        except Exception:
            out[d["key"]] = 0.0
    return out


def wilson(p, n, z=1.96):
    if n == 0:
        return 0, 0
    denom = 1 + z**2/n
    center = (p + z**2/(2*n)) / denom
    half = z * math.sqrt((p*(1-p) + z**2/(4*n)) / n) / denom
    return max(0, center - half), min(1, center + half)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-samples", type=int, default=20)
    args = ap.parse_args()

    results = load_results()
    snap_map = load_latest_snapshot_map()

    # 合并标签: 现有 19 个 + 候选 11 个
    all_labels = dict(FACTOR_LABELS)
    for d in EXTRA_FACTOR_DEFS:
        all_labels[d["key"]] = d["label"]

    # 统计容器
    factor_stat = defaultdict(lambda: {"correct": 0, "wrong": 0, "neutral": 0})
    cond_pm = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    cond_vol = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    cond_pct = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    dir_stat = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    win_stat = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    contrib = defaultdict(lambda: [0.0, 0.0])

    # 独立性矩阵: 当 ext_* 触发时, 主因子是否同触发且同向
    # 关注 PM 候选与 PM 动量 (pm_momentum), RSI 候选与 short_bounce / momentum_burst
    INDEP_PAIRS = [
        ("ext_book_imbalance", "pm_momentum"),
        ("ext_book_imbalance_strong", "pm_momentum"),
        ("ext_pm_mark_book_skew", "pm_momentum"),
        ("ext_price_1h_change", "kline_1h"),
        ("ext_price_1h_change", "momentum_burst"),
        ("ext_v_reversal_15m", "short_bounce_factor"),
        ("ext_macd_hist_5m", "kline_15m"),
        ("ext_macd_accel_15m", "momentum_burst"),
        ("ext_dev_trend_accel", "price_position"),
    ]
    co_trig = defaultdict(lambda: [0, 0, 0])  # [同触发且同向, 同触发但反向, 候选触发主因子未触发]

    model_correct = model_total = joined = skipped = 0
    vote_correct = vote_total = 0
    s0_correct = s0_total = 0

    for slug, r in results.items():
        actual = r["actual"]
        side = r.get("side")
        if slug not in snap_map:
            skipped += 1
            continue
        try:
            md = json.loads(snap_map[slug].read_text())
            factors, inp, notes, mode = extract_factors(md)
        except Exception:
            skipped += 1
            continue
        joined += 1

        # 把候选因子并入 factors
        extras = compute_extra_factors(md)
        factors_full = {**factors, **extras}

        # 模型整体方向
        if side in ("UP", "DOWN"):
            model_total += 1
            model_correct += int(side == actual)

        # 多数投票 + S₀(含候选)
        pos = sum(1 for v in factors_full.values() if v > 0)
        neg = sum(1 for v in factors_full.values() if v < 0)
        vote_total += 1
        vote_correct += int(sign(pos - neg) == sign(1 if actual == "UP" else -1))
        s0_total += 1
        s0_correct += int(sign(sum(factors_full.values())) == sign(1 if actual == "UP" else -1))

        # 切片桶
        pm = md.get("polymarket") or {}
        pm_up, pm_down = pm.get("up_price"), pm.get("down_price")
        if pm_up is not None and pm_up >= 0.60:
            pm_bucket = "PM_UP≥60%"
        elif pm_down is not None and pm_down >= 0.60:
            pm_bucket = "PM_DOWN≥60%"
        else:
            pm_bucket = "PM弱共识"
        vol = md.get("volatility_5m")
        vol_bucket = "vol缺" if vol is None else ("vol≤0.5%" if vol <= 0.5 else "vol>0.5%")
        pct = (md.get("price_position_24h") or {}).get("current_percentile")
        if pct is None:
            pct_bucket = "pct缺"
        elif pct >= 85:
            pct_bucket = "pct≥85"
        elif pct <= 15:
            pct_bucket = "pct≤15"
        else:
            pct_bucket = "pct中间"

        # 逐因子统计
        for fname, fval in factors_full.items():
            h = hit(fval, actual)
            if h == "correct":
                factor_stat[fname]["correct"] += 1
                cond_pm[fname][pm_bucket][0] += 1
                cond_vol[fname][vol_bucket][0] += 1
                cond_pct[fname][pct_bucket][0] += 1
                contrib[fname][0] += abs(fval)
            elif h == "wrong":
                factor_stat[fname]["wrong"] += 1
                cond_pm[fname][pm_bucket][1] += 1
                cond_vol[fname][vol_bucket][1] += 1
                cond_pct[fname][pct_bucket][1] += 1
                contrib[fname][1] += abs(fval)
            else:
                factor_stat[fname]["neutral"] += 1
            if fval > 0:
                dir_stat[fname]["看多"][0 if h == "correct" else 1] += 1
            elif fval < 0:
                dir_stat[fname]["看空"][0 if h == "correct" else 1] += 1
            if side in ("UP", "DOWN") and h != "neutral":
                win_stat[fname]["赢单" if side == actual else "输单"][0 if h == "correct" else 1] += 1

        # 独立性检验
        for ext_key, main_key in INDEP_PAIRS:
            ev = extras.get(ext_key, 0.0)
            mv = factors.get(main_key, 0.0)
            if ev != 0 and mv != 0:
                # 同触发
                if sign(ev) == sign(mv):
                    co_trig[(ext_key, main_key)][0] += 1
                else:
                    co_trig[(ext_key, main_key)][1] += 1
            elif ev != 0 and mv == 0:
                co_trig[(ext_key, main_key)][2] += 1
            # mv != 0 and ev == 0 不计入"候选触发"

    # === 输出控制台 ===
    rows = []
    for fname, st in factor_stat.items():
        n_signal = st["correct"] + st["wrong"]
        if n_signal < args.min_samples:
            continue
        rate = st["correct"] / n_signal
        lo, hi = wilson(rate, n_signal)
        score = rate * math.sqrt(n_signal)
        rows.append({
            "key": fname,
            "label": all_labels.get(fname, fname),
            "n": n_signal,
            "hit_rate": rate,
            "lo": lo, "hi": hi,
            "correct": st["correct"],
            "wrong": st["wrong"],
            "neutral": st["neutral"],
            "score": score,
            "is_candidate": fname in EXTRA_FACTOR_KEYS,
            "category": next((d["category"] for d in EXTRA_FACTOR_DEFS if d["key"] == fname), "engine"),
        })
    rows.sort(key=lambda r: -r["score"])

    print("=" * 80)
    print(f"扩展归因统计 — 现有 13 引擎因子 + {len(EXTRA_FACTOR_DEFS)} 外挂候选")
    print(f"join {joined} / skipped {skipped}")
    print("=" * 80)

    print("\n### 一、模型基线对照(含候选)")
    print(f"模型方向(AI 推理):   {model_correct}/{model_total} = {model_correct/model_total*100:.1f}%")
    print(f"因子多数投票(含候选): {vote_correct}/{vote_total} = {vote_correct/vote_total*100:.1f}%")
    print(f"S₀ 净分(含候选):     {s0_correct}/{s0_total} = {s0_correct/s0_total*100:.1f}%")

    # 分两类输出
    engine_rows = [r for r in rows if not r["is_candidate"]]
    cand_rows = [r for r in rows if r["is_candidate"]]

    print(f"\n### 二、现有 {len(engine_rows)} 个引擎机算因子(归因基线, n≥{args.min_samples})")
    print(f"{'因子':<35} {'命中率':>7} {'n':>4} {'lo':>5} {'hi':>5} {'score':>6}")
    for r in engine_rows:
        flag = " ⭐" if r["hit_rate"] >= 0.70 else ""
        print(f"{r['label'][:33]:<35} {r['hit_rate']*100:>6.1f}% {r['n']:>4} {r['lo']*100:>4.1f}% {r['hi']*100:>4.1f}% {r['score']:>5.2f}{flag}")

    print(f"\n### 三、外挂 {len(cand_rows)} 个候选因子(待裁决)")
    print(f"{'因子':<40} {'命中率':>7} {'n':>4} {'lo':>5} {'hi':>5} {'score':>6}  类别")
    for r in cand_rows:
        flag = " ⭐≥70%" if r["hit_rate"] >= 0.70 else ""
        print(f"{r['label'][:38]:<40} {r['hit_rate']*100:>6.1f}% {r['n']:>4} {r['lo']*100:>4.1f}% {r['hi']*100:>4.1f}% {r['score']:>5.2f}{flag}  {r['category']}")

    # 独立性矩阵
    print(f"\n### 四、独立性检验 — 候选 vs 现有因子(同触发样本中的同向率)")
    print(f"{'候选 vs 主因子':<55} {'同触发同向':>10} {'同触发反向':>10} {'候选触发主因子未触发':>16} {'同向率':>8}")
    for (ext_key, main_key), (same, opp, only_ext) in sorted(co_trig.items(), key=lambda x: -x[1][0]):
        total_co = same + opp
        if total_co < 5:
            continue
        ext_label = next((d["label"][:25] for d in EXTRA_FACTOR_DEFS if d["key"] == ext_key), ext_key)
        main_label = FACTOR_LABELS.get(main_key, main_key)
        same_rate = same / total_co * 100
        flag = " ⚠️高共线" if same_rate >= 70 else (" 🔄反向" if same_rate <= 30 else "")
        print(f"{ext_label:<25} vs {main_label:<25} {same:>10} {opp:>10} {only_ext:>16} {same_rate:>7.1f}%{flag}")

    # === 落盘 JSON ===
    out_json = {
        "generated_at": datetime.now().isoformat(),
        "joined": joined,
        "skipped": skipped,
        "model_accuracy": model_correct/model_total if model_total else 0,
        "vote_accuracy_extended": vote_correct/vote_total if vote_total else 0,
        "s0_accuracy_extended": s0_correct/s0_total if s0_total else 0,
        "factors": {},
        "independence_pairs": {f"{e}|{m}": {"same": v[0], "opposite": v[1], "only_candidate": v[2]} for (e, m), v in co_trig.items()},
    }
    for r in rows:
        out_json["factors"][r["key"]] = {
            "label": r["label"],
            "n": r["n"],
            "hit_rate": r["hit_rate"],
            "lo": r["lo"], "hi": r["hi"],
            "correct": r["correct"],
            "wrong": r["wrong"],
            "neutral": r["neutral"],
            "score": r["score"],
            "is_candidate": r["is_candidate"],
            "category": r["category"],
        }
    json_path = DATA / f"hermes_factor_stats_extended_{datetime.now().strftime('%Y%m%d')}.json"
    json_path.write_text(json.dumps(out_json, ensure_ascii=False, indent=2), encoding="utf-8")

    # === 落盘 Markdown 报告 ===
    md_lines = []
    md_lines.append(f"# 扩展归因报告 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    md_lines.append("")
    md_lines.append(f"**数据**: join {joined} / 跳过 {skipped}")
    md_lines.append(f"**引擎机算因子**: 13 个(主归因基线)")
    md_lines.append(f"**外挂候选因子**: {len(EXTRA_FACTOR_DEFS)} 个(命名空间 `ext_*`, 不污染 S₀)")
    md_lines.append(f"**脚本**: `scripts/factor_attribution_extended.py`")
    md_lines.append("")
    md_lines.append("## 模型基线")
    md_lines.append(f"- 模型方向(AI 推理): **{model_correct/model_total*100:.1f}%** ({model_correct}/{model_total})")
    md_lines.append(f"- 因子多数投票(含候选): {vote_correct/vote_total*100:.1f}%")
    md_lines.append(f"- S₀ 净分(含候选): {s0_correct/s0_total*100:.1f}%")
    md_lines.append("")
    md_lines.append("## 引擎机算因子(归因基线, 按 score 排序)")
    md_lines.append("")
    md_lines.append("| 因子 | 命中率 | n | 95% CI | 对/错/中 | score |")
    md_lines.append("|------|--------|---|--------|----------|-------|")
    for r in engine_rows:
        flag = " ⭐" if r["hit_rate"] >= 0.70 else ""
        md_lines.append(f"| {r['label']} | {r['hit_rate']*100:.1f}%{flag} | {r['n']} | [{r['lo']*100:.0f}%, {r['hi']*100:.0f}%] | {r['correct']}/{r['wrong']}/{r['neutral']} | {r['score']:.2f} |")
    md_lines.append("")
    md_lines.append("## 外挂候选因子(待裁决)")
    md_lines.append("")
    md_lines.append("| 因子 | 类别 | 命中率 | n | 95% CI | 对/错/中 | score |")
    md_lines.append("|------|------|--------|---|--------|----------|-------|")
    for r in cand_rows:
        flag = " ⭐≥70%" if r["hit_rate"] >= 0.70 else ""
        md_lines.append(f"| {r['label']} | {r['category']} | {r['hit_rate']*100:.1f}%{flag} | {r['n']} | [{r['lo']*100:.0f}%, {r['hi']*100:.0f}%] | {r['correct']}/{r['wrong']}/{r['neutral']} | {r['score']:.2f} |")
    md_lines.append("")
    md_lines.append("## 独立性矩阵 — 候选 vs 现有因子")
    md_lines.append("")
    md_lines.append("**判定**: 同触发时,候选因子与主因子方向是否一致。")
    md_lines.append("- **同向率 ≥70%**: ⚠️ 高共线 → 候选几乎重复主因子,不该进 S0")
    md_lines.append("- **同向率 ≤30%**: 🔄 反向 → 候选可能在主因子失效时互补,值得保留观察")
    md_lines.append("- **30%-70%**: 中性 → 独立性 OK,但需评估边际贡献")
    md_lines.append("")
    md_lines.append("| 候选因子 | 主因子 | 同触发同向 | 同触发反向 | 候选触发主因子未触发 | 同向率 |")
    md_lines.append("|----------|--------|-----------|-----------|---------------------|--------|")
    for (ext_key, main_key), (same, opp, only_ext) in sorted(co_trig.items(), key=lambda x: -x[1][0]):
        total_co = same + opp
        if total_co < 5:
            continue
        ext_label = next((d["label"] for d in EXTRA_FACTOR_DEFS if d["key"] == ext_key), ext_key)
        main_label = FACTOR_LABELS.get(main_key, main_key)
        same_rate = same / total_co * 100
        flag = " ⚠️共线" if same_rate >= 70 else (" 🔄反向" if same_rate <= 30 else "")
        md_lines.append(f"| {ext_label} | {main_label} | {same} | {opp} | {only_ext} | {same_rate:.1f}%{flag} |")
    md_lines.append("")

    # 决策建议
    md_lines.append("## 决策建议(基于本次归因 + 独立性检验)")
    md_lines.append("")
    md_lines.append("### 优先级 P1 — 立即可试(独立性高 + 命中率高)")
    md_lines.append("- 待独立性矩阵输出后填入")
    md_lines.append("")
    md_lines.append("### 优先级 P2 — 黑名单观察(暂不进 S0)")
    md_lines.append("- 1m/5m/15m RSI 超买/超卖反向 — 命中率高但与动量市铁证矛盾,加 G5.4 预警位机制观察")
    md_lines.append("")
    md_lines.append("### 优先级 P3 — 共线剔除(不应进 S0)")
    md_lines.append("- 与 PM 动量同向率 ≥70% 的 PM 候选 → 不进 S0,功能已被 PM 动量覆盖")
    md_lines.append("- 与 prev_hour.change_pct 高度共线 → 不进 S0")

    md_path = DATA / f"hermes_factor_attribution_extended_{datetime.now().strftime('%Y%m%d')}.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    print(f"\n报告落盘:")
    print(f"  JSON: {json_path}")
    print(f"  MD:   {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
