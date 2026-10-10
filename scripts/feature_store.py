#!/usr/bin/env python3
"""
Hermes 特征库构建器（升级计划书 任务 0.3，2026-08-22）。

定位：
    把 data/snapshots/*.json 原始快照 + data/hermes_results.jsonl 结算标签
    转成结构化、可回测的特征数据集（CSV），并生成特征字典与数据质量报告。

安全声明（零风险承诺）：
    - 只【读】snapshots / results，不修改、不删除任何现有文件。
    - 所有输出写入 data/features/ 与 docs/features.md（新建目录/新文件）。
    - 无状态可重放：每次运行全量重建，同一输入永远得到同一输出。

用法：
    python3 scripts/feature_store.py                 # 默认 pick=first（信号前窗口快照）
    python3 scripts/feature_store.py --pick last     # 每 slug 最新快照（与旧回放口径对齐）
    python3 scripts/feature_store.py --no-dict       # 跳过 docs/features.md 生成

输出：
    data/features/hermes_features_full.csv      全量特征表（一行 = 一个 slug）
    data/features/hermes_features_YYYYMMDD.csv  按合约日期切分的日文件
    data/features/manifest.json                 构建清单
    data/features/quality_report.md             缺失值/覆盖率质量报告
    docs/features.md                            特征字典

设计要点：
    pick=first：每 slug 取最早快照（≈ ET 整点后 :12-:17 例行采集），即写信号时刻
    的数据面 —— 对回测而言前瞻偏差最小。
    pick=last ：每 slug 取最新快照，兼容 replay_common.get_latest_snapshots 口径，
    但部分 slug 的"最新"可能已接近结算（PM 价格含结果信息），仅用于对照。
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

_HERMES_DIR = os.environ.get("HERMES_DIR")
ROOT = Path(_HERMES_DIR) if _HERMES_DIR else Path("/Users/tangyijun/Desktop/hermes btc")
DATA = ROOT / "data"
SNAP_DIR = DATA / "snapshots"
RESULTS_FILE = DATA / "hermes_results.jsonl"
OUT_DIR = DATA / "features"
DICT_FILE = ROOT / "docs" / "features.md"

SLUG_RE = re.compile(r"^(?P<slug>.+)_(?P<dt>\d{8}_\d{6})\.json$")
MONTHS = {v: i + 1 for i, v in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}


def _f(x):
    """宽松转 float；不可转返回 NaN。"""
    try:
        if x is None or x == "":
            return float("nan")
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def _b(x):
    """bool → 1.0/0.0；缺失 NaN。"""
    if x is None:
        return float("nan")
    return 1.0 if x else 0.0


def _dir01(x):
    """UP→+1, DOWN→-1, 其他→NaN。"""
    if x == "UP":
        return 1.0
    if x == "DOWN":
        return -1.0
    return float("nan")


def _tf_col(tf, key):
    def get(d):
        return _f((d.get("timeframes") or {}).get(tf, {}).get(key))
    return get


def _tf_dir(tf):
    def get(d):
        return _dir01((d.get("timeframes") or {}).get(tf, {}).get("recent_dir"))
    return get


# ---------------------------------------------------------------- 特征规格
# (列名, 提取函数)。顺序即 CSV 列序 + 字典生成序。提取函数签名: (d: dict) -> float
SPEC = [
    # --- 价格与偏差 ---
    ("anchor_price",          lambda d: _f(d.get("anchor_price"))),
    ("current_price",         lambda d: _f(d.get("current_price"))),
    ("deviation_pct",         lambda d: (_f(d.get("deviation")) / _f(d.get("anchor_price")) * 100)
                              if _f(d.get("anchor_price")) else float("nan")),
    ("volatility_5m",         lambda d: _f(d.get("volatility_5m"))),
    ("dev_expanding",         lambda d: _b((d.get("deviation_trend") or {}).get("expanding"))),
    ("dev_stable",            lambda d: _b((d.get("deviation_trend") or {}).get("stable"))),
    ("dev_shrinking",         lambda d: _b((d.get("deviation_trend") or {}).get("shrinking"))),
    # --- 24h 位置 ---
    ("pos24h_percentile",     lambda d: _f((d.get("price_position_24h") or {}).get("current_percentile"))),
    ("pos24h_upper_half",     lambda d: _b((d.get("price_position_24h") or {}).get("in_upper_half"))),
    ("chg_pct_24h",           lambda d: _f((d.get("ticker_24h") or {}).get("change_pct"))),
    # --- 资金面 ---
    ("taker_bs_ratio",        lambda d: _f((d.get("capital_flow") or {}).get("taker_bs_ratio"))),
    ("taker_bs_avg_1h",       lambda d: _f((d.get("capital_flow") or {}).get("taker_bs_avg_1h"))),
    ("oi_change_24h_pct",     lambda d: _f((d.get("capital_flow") or {}).get("oi_change_24h_pct"))),
    ("long_short_ratio",      lambda d: _f((d.get("capital_flow") or {}).get("long_short_ratio"))),
    ("ob_bid_vol_top10",      lambda d: _f(((d.get("capital_flow") or {}).get("order_book") or {}).get("bid_volume_top10"))),
    ("ob_ask_vol_top10",      lambda d: _f(((d.get("capital_flow") or {}).get("order_book") or {}).get("ask_volume_top10"))),
    ("ob_wall_ratio",         lambda d: (lambda b, a: (b / a) if a and a > 0 else float("nan"))(
                              _f(((d.get("capital_flow") or {}).get("order_book") or {}).get("bid_volume_top10")),
                              _f(((d.get("capital_flow") or {}).get("order_book") or {}).get("ask_volume_top10")))),
    ("flow_imbalance_5m",     lambda d: _f(((d.get("capital_flow") or {}).get("order_flow") or {}).get("imbalance_5m"))),
    ("flow_imbalance_15m",    lambda d: _f(((d.get("capital_flow") or {}).get("order_flow") or {}).get("imbalance_15m"))),
    # --- 情绪 / 历史 ---
    ("fear_greed",            lambda d: _f((d.get("sentiment") or {}).get("fear_greed_index"))),
    ("hist_up_rate",          lambda d: _f((d.get("historical") or {}).get("up_rate"))),
    ("hist_samples",          lambda d: _f((d.get("historical") or {}).get("total_samples"))),
    # --- Polymarket ---
    ("pm_up",                 lambda d: _f((d.get("polymarket") or {}).get("up_price"))),
    ("pm_down",               lambda d: _f((d.get("polymarket") or {}).get("down_price"))),
    ("pm_volume",             lambda d: _f((d.get("polymarket") or {}).get("volume"))),
    ("pm_liquidity",          lambda d: _f((d.get("polymarket") or {}).get("liquidity"))),
    # --- 关键位距离 ---
    ("dist_weak_support_pct", lambda d: (lambda c, s: (c - s) / s * 100 if s and c else float("nan"))(
                              _f(d.get("current_price")), _f((d.get("key_levels") or {}).get("weak_support")))),
    ("dist_weak_resist_pct",  lambda d: (lambda c, r: (r - c) / r * 100 if r and c else float("nan"))(
                              _f(d.get("current_price")), _f((d.get("key_levels") or {}).get("weak_resistance")))),
    # --- 开盘形态 ---
    ("open_dir",              lambda d: _dir01((d.get("opening") or {}).get("dir"))),
    ("open_range_pct",        lambda d: _f((d.get("opening") or {}).get("range_pct"))),
    ("open_vol_ratio",        lambda d: _f((d.get("opening") or {}).get("vol_ratio"))),
    ("open_fakeout",          lambda d: _b((d.get("opening") or {}).get("has_fakeout"))),
    ("open_vreversal",        lambda d: _b((d.get("opening") or {}).get("has_v_reversal"))),
    ("vreversal_15m",         lambda d: _b((d.get("v_reversal_15m") or {}).get("detected"))),
    # --- 上一小时 ---
    ("prev_side",             lambda d: _dir01((d.get("prev_hour") or {}).get("side"))),
    ("prev_chg_pct",          lambda d: _f((d.get("prev_hour") or {}).get("change_pct"))),
    ("prev_consec_up",        lambda d: _f((d.get("prev_hour") or {}).get("consecutive_up"))),
    ("prev_consec_down",      lambda d: _f((d.get("prev_hour") or {}).get("consecutive_down"))),
    ("prev_flips",            lambda d: _f((d.get("prev_hour") or {}).get("flips"))),
    # --- 宏观趋势族（round41+ 新增，旧快照缺失=NaN）---
    ("macro_up_count",        lambda d: _f((d.get("macro_trend_1h_4h") or {}).get("up_count"))),
    ("macro_down_count",      lambda d: _f((d.get("macro_trend_1h_4h") or {}).get("down_count"))),
    # --- OI-价格关系 ---
    ("oi_rel_signal_neutral", lambda d: _b(((d.get("oi_price_relation") or {}).get("signal")) == "neutral")),
]

TF_KEYS = [
    ("rsi",            lambda tf: _tf_col(tf, "rsi")),
    ("bb_pos",         lambda tf: _tf_col(tf, "bb_pos")),
    ("vol_ratio",      lambda tf: _tf_col(tf, "vol_ratio")),
    ("macd_hist",      lambda tf: _tf_col(tf, "macd_hist")),
    ("macd_accel",     lambda tf: _tf_col(tf, "macd_acceleration")),
    ("recent_dir",     lambda tf: _tf_dir(tf)),
    ("consec_up",      lambda tf: _tf_col(tf, "consecutive_up")),
    ("consec_down",    lambda tf: _tf_col(tf, "consecutive_down")),
    ("taker_pct_last", lambda tf: _tf_col(tf, "taker_pct_last")),
]
TFS = ["1m", "5m", "15m", "1H", "4H", "1D"]
for _tf in TFS:
    for _name, _mk in TF_KEYS:
        SPEC.append((f"{_name}_{_tf.lower()}", _mk(_tf)))

FEATURE_COLS = [c for c, _ in SPEC]
META_COLS = ["slug", "hour_et", "snapshot_file", "collect_time_utc", "pick"]
LABEL_COLS = ["side", "actual", "win", "buy_price", "filled", "pnl", "strategy_version"]


# ---------------------------------------------------------------- 加载与抽取

def parse_snapshot_file(path):
    m = SLUG_RE.match(path.name)
    if not m:
        return None
    return m.group("slug"), m.group("dt")


def hour_et_from_slug(slug):
    """bitcoin-up-or-down-august-22-2026-9am-et -> 9am"""
    m = re.search(r"\d+(?:am|pm)-et$", slug)
    return m.group(0).replace("-et", "") if m else ""


def date_key_from_slug(slug):
    """bitcoin-up-or-down-august-22-2026-9am-et -> 20260822"""
    m = re.search(r"-([a-z]+)-(\d+)-(\d{4})-", slug)
    if not m:
        return "unknown"
    mon, day, year = m.group(1), int(m.group(2)), int(m.group(3))
    return f"{year}{MONTHS.get(mon, 0):02d}{day:02d}"


def load_labels():
    """hermes_results.jsonl 每 slug 取最后一条（最新结算状态）。"""
    labels = {}
    if not RESULTS_FILE.exists():
        return labels
    with open(RESULTS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            slug = r.get("slug")
            if not slug:
                continue
            actual = r.get("actual")
            side = r.get("side")
            labels[slug] = {
                "side": side,
                "actual": actual,
                "win": (1.0 if side == actual else 0.0) if side and actual else float("nan"),
                "buy_price": _f(r.get("buy_price")),
                "filled": _b(r.get("filled")),
                "pnl": _f(r.get("pnl")),
                "strategy_version": r.get("strategy_version") or "",
            }
    return labels


def extract_row(d):
    return {col: fn(d) for col, fn in SPEC}


def build(pick):
    files = sorted(SNAP_DIR.glob("*.json"))
    by_slug = defaultdict(list)
    for p in files:
        parsed = parse_snapshot_file(p)
        if parsed:
            by_slug[parsed[0]].append((parsed[1], p))

    labels = load_labels()
    rows = []
    n_unparsed = 0
    for slug, items in sorted(by_slug.items()):
        items.sort()
        path = items[0][1] if pick == "first" else items[-1][1]
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            n_unparsed += 1
            continue
        row = {"slug": slug,
               "hour_et": hour_et_from_slug(slug),
               "snapshot_file": path.name,
               "collect_time_utc": d.get("collect_time_utc", ""),
               "pick": pick}
        row.update(extract_row(d))
        lab = labels.get(slug, {})
        for c in LABEL_COLS:
            v = lab.get(c)
            row[c] = v if v not in (None,) else (
                "" if c in ("side", "actual", "strategy_version") else float("nan"))
        rows.append(row)

    df = pd.DataFrame(rows, columns=META_COLS + FEATURE_COLS + LABEL_COLS)
    df.attrs["n_unparsed_json"] = n_unparsed
    df.attrs["n_snapshots"] = len(files)
    df.attrs["n_slugs"] = len(by_slug)
    return df


# ---------------------------------------------------------------- 输出

def write_outputs(df, out_dir, emit_dict=True):
    out_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_dir / "hermes_features_full.csv", index=False)

    daily_keys = []
    for key in sorted({date_key_from_slug(s) for s in df["slug"]}):
        sub = df[df["slug"].map(date_key_from_slug) == key]
        sub.to_csv(out_dir / f"hermes_features_{key}.csv", index=False)
        daily_keys.append(key)

    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "pick_mode": str(df["pick"].iloc[0]) if len(df) else "?",
        "n_rows": len(df),
        "n_feature_cols": len(FEATURE_COLS),
        "n_labelled": int(df["actual"].notna().sum()) if len(df) else 0,
        "n_labelled_win": int((df["win"] == 1).sum()) if len(df) else 0,
        "n_snapshots_scanned": df.attrs.get("n_snapshots", 0),
        "n_slugs_found": df.attrs.get("n_slugs", 0),
        "n_unparsable_json": df.attrs.get("n_unparsed_json", 0),
        "daily_files": daily_keys,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    write_quality_report(df, out_dir)
    if emit_dict:
        write_dict(DICT_FILE)


def write_quality_report(df, out_dir):
    n = len(df)
    lines = [
        "# 数据质量报告（feature_store 自动生成）", "",
        f"- 生成时间(UTC)：{datetime.now(timezone.utc).isoformat()}",
        f"- 扫描快照文件：{df.attrs.get('n_snapshots', 0)}",
        f"- slug 数（=特征行数）：{n}",
        f"- 无法解析的 JSON：{df.attrs.get('n_unparsed_json', 0)}",
        f"- 已带结算标签的行：{int(df['actual'].notna().sum()) if n else 0}", "",
        "## 列覆盖率（非缺失比例）", "",
        "| 列 | 非缺失 | 覆盖率 |", "|---|---:|---:|",
    ]
    if n:
        for col in FEATURE_COLS + ["win", "buy_price"]:
            s = df[col]
            nn = int(s.notna().sum())
            lines.append(f"| {col} | {nn} | {nn / n * 100:.1f}% |")
    else:
        lines.append("（无数据行）")
    (out_dir / "quality_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# 特征分组说明（用于字典文档；与 SPEC 顺序对应，按列名前缀匹配）
DICT_SECTIONS = [
    ("价格与偏差", ["anchor_price", "current_price", "deviation_pct", "volatility_5m",
                    "dev_expanding", "dev_stable", "dev_shrinking"]),
    ("24h 价格位置", ["pos24h_percentile", "pos24h_upper_half", "chg_pct_24h"]),
    ("资金面（Binance fapi）", ["taker_bs_ratio", "taker_bs_avg_1h", "oi_change_24h_pct",
                                "long_short_ratio", "ob_bid_vol_top10", "ob_ask_vol_top10",
                                "ob_wall_ratio", "flow_imbalance_5m", "flow_imbalance_15m"]),
    ("情绪与历史季节性", ["fear_greed", "hist_up_rate", "hist_samples"]),
    ("Polymarket 盘口", ["pm_up", "pm_down", "pm_volume", "pm_liquidity"]),
    ("关键位距离", ["dist_weak_support_pct", "dist_weak_resist_pct"]),
    ("开盘微观形态", ["open_dir", "open_range_pct", "open_vol_ratio",
                      "open_fakeout", "open_vreversal", "vreversal_15m"]),
    ("上一小时惯性", ["prev_side", "prev_chg_pct", "prev_consec_up",
                      "prev_consec_down", "prev_flips"]),
    ("宏观趋势族（round41+）", ["macro_up_count", "macro_down_count"]),
    ("OI-价格关系", ["oi_rel_signal_neutral"]),
]

TF_KEY_DESC = {
    "rsi": "RSI(14)", "bb_pos": "布林带位置%", "vol_ratio": "成交量比率",
    "macd_hist": "MACD 柱", "macd_accel": "MACD 加速度(hist-前值)",
    "recent_dir": "最近方向 UP+1/DOWN-1", "consec_up": "连续上涨根数",
    "consec_down": "连续下跌根数", "taker_pct_last": "Taker 主动买占比末值",
}


def write_dict(dict_file):
    """从 SPEC 自动生成特征字典，保证与代码零漂移。"""
    covered = set()
    parts = [
        "# Hermes 特征字典",
        "",
        "> 由 `scripts/feature_store.py --emit-dict` 自动生成，请勿手改列名表；",
        "> 改特征定义请改 feature_store.py 的 SPEC 后重新生成。",
        "",
        "- 一行 = 一个 slug（一个 1H 合约），取该 slug 的信号窗口快照（pick 模式见脚本头注释）。",
        "- 标签列来自 `data/hermes_results.jsonl`：side=信号方向，actual=实际结算方向，",
        "  win=1/0，buy_price=bot 成交价，pnl=模拟盈亏。",
        "- 缺失值统一为空（NaN）；bool 因子 1/0；方向因子 UP=+1/DOWN=-1。",
        "",
    ]
    for title, cols in DICT_SECTIONS:
        parts.append(f"## {title}")
        parts.append("")
        parts.append("| 列 | 含义 |")
        parts.append("|---|---|")
        for col in cols:
            fn = dict(SPEC).get(col)
            note = ""
            parts.append(f"| {col} | {note} |")
            covered.add(col)
        parts.append("")

    parts.append("## 时间框架因子（6 个 TF × 9 个指标 = 54 列）")
    parts.append("")
    parts.append("列名格式 `{指标}_{tf}`，tf ∈ 1m/5m/15m/1h/4h/1d（快照内为 Binance K 线）。")
    parts.append("")
    parts.append("| 指标 | 含义 |")
    parts.append("|---|---|")
    for name, desc in TF_KEY_DESC.items():
        parts.append(f"| {name}_* | {desc} |")
    parts.append("")

    rest = [c for c in FEATURE_COLS if c not in covered
            and not re.match(r"^[a-z0-9]+_(1m|5m|15m|1h|4h|1d)$", c)]
    if rest:
        parts.append("## 其他（未分组）")
        parts.append("")
        parts.append(", ".join(rest))
        parts.append("")

    parts.append("## 标签列")
    parts.append("")
    parts.append("| 列 | 含义 |")
    parts.append("|---|---|")
    parts.append("| side | AI 信号方向 UP/DOWN |")
    parts.append("| actual | 实际结算方向 UP/DOWN |")
    parts.append("| win | 1=方向对，0=错 |")
    parts.append("| buy_price | bot 成交价（PAPER 模式为挂单价） |")
    parts.append("| filled | 是否成交 1/0 |")
    parts.append("| pnl | 模拟盈亏（美元） |")
    parts.append("| strategy_version | 规则版本锚点 |")
    parts.append("")

    dict_file.parent.mkdir(parents=True, exist_ok=True)
    dict_file.write_text("\n".join(parts), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Hermes 快照 → CSV 特征库（任务0.3）")
    ap.add_argument("--pick", choices=["first", "last"], default="first",
                    help="每 slug 取最早(first,默认)/最新(last)快照")
    ap.add_argument("--no-dict", action="store_true", help="不重写 docs/features.md")
    args = ap.parse_args()

    df = build(args.pick)
    write_outputs(df, OUT_DIR, emit_dict=not args.no_dict)

    n_lab = int(df["actual"].notna().sum())
    n_win = int((df["win"] == 1).sum())
    print(f"[feature_store] 快照扫描 {df.attrs.get('n_snapshots', 0)} 个文件 → "
          f"{len(df)} 行 × {len(FEATURE_COLS)} 特征列 (pick={args.pick})")
    print(f"[feature_store] 已结算标签 {n_lab} 行，其中 win={n_win}"
          + (f"（胜率 {n_win / n_lab * 100:.1f}%）" if n_lab else ""))
    print(f"[feature_store] 输出目录: {OUT_DIR}")


if __name__ == "__main__":
    main()