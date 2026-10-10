#!/usr/bin/env python3
"""
Hermes 交易时段胜率回测（8/12 用户提供"交易时段权重规则"验证）

目的：用 data/hermes_results.jsonl 真实结算数据，按【北京时间 5 段】口径统计各盘面胜率，
验证用户提供的时段权重（美盘开盘 40 / 美盘尾盘 35 / 欧盘 15 / 亚盘 10 / 真空 0-5）
在本系统 205 场样本上是否成立。成立才考虑升级为正式因子；不成立则维持"仅提示"。

口径：
- 数据源 slug 只有 ET 小时（bitcoin-up-or-down-july-31-2026-7am-et -> ET 7）。
- 样本均在 2026-08（夏令时 EDT, UTC-4），北京时间 = ET + 12h。
- 用户规则时段为半小时粒度，且段间有重叠（如欧盘 14:00-22:30 与美盘开盘 20:30-00:00
  在 20:30-22:30 重叠）。本脚本按"该小时起始的北京时间落在哪个时段"归属，
  重叠时取权重更高时段；仅精确到整点小时（slug 粒度所限）。

用户规则（北京时间）:
  美盘开盘  20:30-00:00  w40
  美盘尾盘  01:00-03:30  w35
  欧盘      14:00-22:30  w15
  亚盘      07:00-15:00  w10
  真空      其余          w0-5

输出：终端摘要 + data/hermes_session_report_20260812.md
纯本地，无网络。
"""
# pyright: reportAny=false, reportExplicitAny=false, reportUnknownVariableType=false, reportUnknownMemberType=false

import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
DATA_DIR = THIS_DIR.parent / "data"
REPORT = DATA_DIR / "hermes_session_report_20260812.md"


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def slug_hour_et(slug: str) -> int | None:
    """bitcoin-up-or-down-july-31-2026-7am-et -> 7 (24h ET)。"""
    m = re.search(r"-(\d{1,2})(am|pm)-et$", slug or "")
    if not m:
        return None
    h = int(m.group(1))
    if m.group(2) == "pm" and h != 12:
        h += 12
    if m.group(2) == "am" and h == 12:
        h = 0
    return h


# 用户规则：北京时间 5 段。用 (start_hour, end_hour) 左闭右开表达整点小时归属。
# 段间重叠时取权重更高者（先列高权重，命中即归）。北京小时 = (et + 12) % 24。
# 归属判定：取该小时起始的北京时间 bj_hour，落到哪个段的 [start, end) 区间。
SESSIONS_CN = [
    ("美盘开盘", 40, (20, 24)),   # 20:30-00:00 → 整点 20-23 起始落内
    ("美盘尾盘", 35, (1, 4)),     # 01:00-03:30 → 整点 1-3
    ("欧盘", 15, (14, 23)),       # 14:00-22:30 → 整点 14-22
    ("亚盘", 10, (7, 15)),        # 07:00-15:00 → 整点 7-14
]
# 注意：列表顺序即重叠时的高权重优先——美盘开盘(40) 排在欧盘(15) 前，
# 故北京 20-22 同时落欧盘与美盘开盘时归美盘开盘。


def bj_session(hour_bj: int) -> str:
    """北京整点小时 -> 用户时段名；未命中任一区间为 '真空时段'。"""
    for name, _w, (lo, hi) in SESSIONS_CN:
        if lo <= hour_bj < hi:
            return name
    return "真空时段"


def z_test_two_prop(w1: int, n1: int, w2: int, n2: int) -> tuple[float, float]:
    if n1 == 0 or n2 == 0:
        return 0.0, 1.0
    p1, p2 = w1 / n1, w2 / n2
    p_pool = (w1 + w2) / (n1 + n2)
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0, 1.0
    z = (p1 - p2) / se
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    return z, p


def wilson_ci(w: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = w / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return center - half, center + half


def main() -> int:
    results = load_jsonl(DATA_DIR / "hermes_results.jsonl")
    out: list[str] = []
    term: list[str] = []

    def t(line: str = "") -> None:
        term.append(line)
        out.append(line)

    n_res = len(results)
    wins_all = sum(1 for r in results if r.get("side") == r.get("actual"))
    t("# Hermes 交易时段胜率回测（2026-08-12，用户规则 5 段验证）")
    t("")
    t(f"样本：results {n_res} 场（总胜率 {wins_all / n_res:.1%}）｜口径：北京时间 = ET + 12h（8月夏令时）")
    t("")
    t("> 目的：验证用户提供时段权重（美盘开盘40/尾盘35/欧盘15/亚盘10/真空0-5）在真实结算数据上是否成立。")
    t("> 方法：slug 解析 ET 小时 → 北京时间 → 按用户 5 段归属（重叠取高权重，整点小时粒度）。")
    t("")

    # ---- 各段统计 ----
    sess: dict[str, list] = defaultdict(lambda: [0, 0, 0.0])  # [wins, total, pnl]
    # 每小时明细，用于发现高发误判小时
    hourly: dict[int, list] = defaultdict(lambda: [0, 0, 0.0])
    unmatched = 0
    for r in results:
        h_et = slug_hour_et(r.get("slug", ""))
        if h_et is None:
            unmatched += 1
            continue
        bj = (h_et + 12) % 24
        s = bj_session(bj)
        win = 1 if r.get("side") == r.get("actual") else 0
        sess[s][0] += win
        sess[s][1] += 1
        sess[s][2] += float(r.get("pnl", 0) or 0)
        hourly[bj][0] += win
        hourly[bj][1] += 1
        hourly[bj][2] += float(r.get("pnl", 0) or 0)

    t("## 各时段胜率/盈亏（按用户北京时间 5 段）")
    t("")
    t("| 时段 | 权重(用户) | 胜/总 | 胜率 | Wilson 95% CI | 总盈亏 | 均单盈亏 |")
    t("|------|-----------|-------|------|----------------|--------|----------|")
    order = ["美盘开盘", "美盘尾盘", "欧盘", "亚盘", "真空时段"]
    w_map = {"美盘开盘": "40", "美盘尾盘": "35", "欧盘": "15", "亚盘": "10", "真空时段": "0-5"}
    table = {}
    for s in order:
        wn, tt, pl = sess.get(s, [0, 0, 0.0])
        if not tt:
            continue
        lo, hi = wilson_ci(wn, tt)
        table[s] = (wn, tt)
        t(f"| {s} | {w_map[s]} | {wn}/{tt} | {wn/tt:.1%} | [{lo:.1%}, {hi:.1%}] | {pl:+.2f} | {pl/tt:+.3f} |")
    if unmatched:
        t(f"\n（{unmatched} 条 slug 无法解析时段，已剔除）")
    t("")

    # ---- 关键对比：用户高权重段 vs 低权重段 ----
    t("## 关键假设检验")
    t("")
    high = ("美盘开盘", "美盘尾盘")
    low = ("亚盘", "真空时段")
    for name, pair in (("高权重段(开盘+尾盘)", high), ("低权重段(亚盘+真空)", low)):
        wn = sum(sess.get(p, [0, 0, 0])[0] for p in pair)
        tt = sum(sess.get(p, [0, 0, 0])[1] for p in pair)
        if tt:
            t(f"- {name}：{wn}/{tt} = {wn/tt:.1%}")
    eu = sess.get("欧盘", [0, 0, 0.0])
    if high and eu[1]:
        hw = sum(sess.get(p, [0, 0, 0])[0] for p in high)
        hn = sum(sess.get(p, [0, 0, 0])[1] for p in high)
        z, p = z_test_two_prop(hw, hn, eu[0], eu[1])
        t(f"- 高权重段 {hw/hn:.1%} vs 欧盘 {eu[0]/eu[1]:.1%}：z={z:.2f}，p={p:.3f}（{'显著' if p < 0.05 else '不显著'}）")
    # 美盘尾盘（历史误判高发区）vs 总体
    us_tail = sess.get("美盘尾盘", [0, 0, 0.0])
    if us_tail[1]:
        z, p = z_test_two_prop(us_tail[0], us_tail[1], wins_all - us_tail[0], n_res - us_tail[1])
        t(f"- 美盘尾盘(北京01-03) {us_tail[0]/us_tail[1]:.1%} vs 其余时段 {(wins_all-us_tail[0])/(n_res-us_tail[1]):.1%}：z={z:.2f}，p={p:.3f}（{'显著' if p<0.05 else '不显著'}）")
    t("")

    # ---- 每小时胜率明细 ----
    t("## 北京时间逐小时胜率明细（发现高发误判小时）")
    t("")
    t("| 北京时 | 时段 | 胜/总 | 胜率 | 均单盈亏 |")
    t("|--------|------|-------|------|----------|")
    for bj in range(24):
        wn, tt, pl = hourly.get(bj, [0, 0, 0.0])
        if not tt:
            continue
        t(f"| {bj:02d}:00 | {bj_session(bj)} | {wn}/{tt} | {wn/tt:.1%} | {pl/tt:+.3f} |")
    t("")

    # ---- 结论 ----
    t("## 结论")
    t("")
    worst = None
    for s, (wn, tt) in table.items():
        if tt >= 15 and (worst is None or wn / tt < worst[1]):
            worst = (s, wn / tt, wn, tt)
    best = None
    for s, (wn, tt) in table.items():
        if tt >= 15 and (best is None or wn / tt > best[1]):
            best = (s, wn / tt, wn, tt)
    if worst:
        t(f"- 最弱时段：**{worst[0]}** {worst[2]}/{worst[3]} = {worst[1]:.1%}")
    if best:
        t(f"- 最强时段：**{best[0]}** {best[2]}/{best[3]} = {best[1]:.1%}")
    # 逐段给结论
    for s in order:
        wn, tt = table.get(s, (0, 0))
        if not tt:
            continue
        wr = wn / tt
        w_usr = w_map[s]
        tag = "✓ 支撑用户权重" if wr >= wins_all / n_res else "✗ 弱于总体（不支持该时段高权重预期）"
        t(f"- **{s}**（用户权重 {w_usr}）：实测 {wn/tt:.1%}（总体 {wins_all/n_res:.1%}）→ {tag}")
    t("")

    REPORT.write_text("\n".join(out), encoding="utf-8")
    print("\n".join(term))
    print(f"\n... 完整报告 → {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
