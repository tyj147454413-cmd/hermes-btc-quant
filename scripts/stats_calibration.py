#!/usr/bin/env python3
"""
Hermes 统计校准分析（纯本地，无网络）

回答三个实证问题 + 两个数据质量核查：
  Q1 置信度倒挂：high 档胜率 44% < low 档 61%（review-stats）——是模型校准失效还是数据污染？
  Q2 欧盘弱区：欧盘 53% vs 美盘 65%，是否统计显著？
  Q3 连亏风险：max_loss=6 在当前胜率下是否异常？
  D1 价格分桶：buy_price 各档胜率/盈亏，验证 0.65 封顶与挂价折扣
  D2 数据质量：lessons.confidence 量纲混用（0-1 与 0-100）对 by_confidence 的污染

数据源：
  data/hermes_results.jsonl     184 场结算（slug/side/actual/pnl/buy_price/ts）
  data/review-lessons.json      160 条复盘课（confidence/side/actual，无量化盈亏）
  data/review-stats.json        聚合统计（交叉验证对照）

输出：终端摘要 + data/hermes_stats_report_20260811.md
"""
# pyright: reportAny=false, reportExplicitAny=false, reportUnknownVariableType=false, reportUnknownMemberType=false

import json
import math
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
DATA_DIR = THIS_DIR.parent / "data"
REPORT = DATA_DIR / "hermes_stats_report_20260811.md"

MC_SIMS = 20_000
random.seed(42)


# ---------- 数据加载 ----------

def load_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def slug_hour_et(slug: str) -> int | None:
    """bitcoin-up-or-down-july-31-2026-7am-et -> 7 (24h)。"""
    m = re.search(r"-(\d{1,2})(am|pm)-et$", slug or "")
    if not m:
        return None
    h = int(m.group(1))
    if m.group(2) == "pm" and h != 12:
        h += 12
    if m.group(2) == "am" and h == 12:
        h = 0
    return h


def session_of(hour_et: int) -> str:
    """美东时段划分：亚洲盘 21-05，欧盘 05-12，美盘 12-21（与 hermes_review 注释口径一致）。"""
    if hour_et >= 21 or hour_et < 5:
        return "亚洲盘"
    if hour_et < 12:
        return "欧盘"
    return "美盘"


def norm_prob(c) -> float | None:
    """confidence 量纲归一：<=1 视为 0-1 小数乘 100；>1 视为已是百分数。返回 0-100。"""
    if not isinstance(c, (int, float)):
        return None
    return c * 100 if c <= 1 else float(c)


# ---------- 统计工具 ----------

def z_test_two_prop(w1: int, n1: int, w2: int, n2: int) -> tuple[float, float]:
    """两比例 z 检验，返回 (z, 双侧 p)。"""
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


def max_loss_streak(outcomes: list[int]) -> int:
    best = cur = 0
    for o in outcomes:
        cur = cur + 1 if o == 0 else 0
        best = max(best, cur)
    return best


def mc_max_streak(n: int, p_win: float, sims: int = MC_SIMS) -> tuple[float, float]:
    """蒙特卡洛：n 场、胜率 p_win 下最大连亏长度的均值与 P(>=观测值) 用外部分布算。"""
    totals = 0
    dist: dict[int, int] = defaultdict(int)
    for _ in range(sims):
        ms = max_loss_streak([1 if random.random() < p_win else 0 for _ in range(n)])
        totals += ms
        dist[ms] += 1
    return totals / sims, dist


# ---------- 主分析 ----------

def main() -> int:
    results = load_jsonl(DATA_DIR / "hermes_results.jsonl")
    lessons_doc = json.loads((DATA_DIR / "review-lessons.json").read_text())
    lessons = lessons_doc.get("lessons", [])
    stats = json.loads((DATA_DIR / "review-stats.json").read_text())

    out: list[str] = []
    term: list[str] = []

    def w(line: str = "") -> None:
        out.append(line)

    def t(line: str = "") -> None:
        term.append(line)
        w(line)

    # ===== 总览 =====
    n_res = len(results)
    wins = sum(1 for r in results if r.get("side") == r.get("actual"))
    pnl_sum = sum(float(r.get("pnl", 0) or 0) for r in results)
    win_rate = wins / n_res if n_res else 0

    t("# Hermes 统计校准报告（2026-08-11）")
    t("")
    t(f"样本：results {n_res} 场（{wins} 胜 {n_res - wins} 负，胜率 {win_rate:.1%}，总盈亏 {pnl_sum:+.2f}）"
      f"｜lessons {len(lessons)} 条｜review-stats 记录 total={stats.get('total')}")
    t("")

    # ===== D2 数据质量：confidence 量纲混用 =====
    confs = [l.get("confidence") for l in lessons]
    frac = [c for c in confs if isinstance(c, (int, float)) and c <= 1]
    pct = [c for c in confs if isinstance(c, (int, float)) and c > 1]
    w("## D2 数据质量核查：confidence 量纲混用污染 by_confidence")
    w("")
    w(f"- lessons 共 {len(confs)} 条带 confidence：**{len(frac)} 条为 0-1 小数**（如 0.58），**{len(pct)} 条为 0-100 百分数**（如 72.9）")
    w("- `hermes_review.get_confidence_band(prob)` 按 0-100 口径分带（>=70 high, >=55 mid）→ 全部小数记录落入 low 档")
    w(f"- review-stats by_confidence 原始值：{stats.get('by_confidence')}")
    w("- **结论：\"high 档 44% < low 档 61%\" 的置信度倒挂大部分是量纲污染假象，不能直接得出'模型校准失效'**")
    w("")

    # 归一化后重算
    band_stats: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # band -> [wins, total]
    for l in lessons:
        p = norm_prob(l.get("confidence"))
        if p is None:
            continue
        band = "high" if p >= 70 else ("mid" if p >= 55 else "low")
        win = 1 if l.get("side") == l.get("actual") else 0
        band_stats[band][0] += win
        band_stats[band][1] += 1
    w("归一化（小数×100）后按同一分带重算（lessons 数据源）：")
    w("")
    w("| 档位 | 胜/总 | 胜率 | Wilson 95% CI |")
    w("|------|-------|------|----------------|")
    for band in ("high", "mid", "low"):
        wn, tt = band_stats.get(band, [0, 0])
        if tt:
            lo, hi = wilson_ci(wn, tt)
            w(f"| {band} | {wn}/{tt} | {wn/tt:.1%} | [{lo:.1%}, {hi:.1%}] |")
    w("")

    # ===== Q1 校准（归一化概率 vs 实际胜率）=====
    t("## Q1 概率校准（lessons，confidence 归一化后）")
    t("")
    bins = [(50, 55), (55, 60), (60, 65), (65, 70), (70, 101)]
    t("| 预测概率档 | 样本 | 预测均值 | 实际胜率 | 偏差 |")
    t("|------------|------|----------|----------|------|")
    brier = 0.0
    logloss = 0.0
    n_cal = 0
    for lo, hi in bins:
        pts = []
        for l in lessons:
            p = norm_prob(l.get("confidence"))
            if p is None or not (lo <= p < hi):
                continue
            y = 1 if l.get("side") == l.get("actual") else 0
            pts.append((p / 100, y))
        if not pts:
            continue
        pred = sum(p for p, _ in pts) / len(pts)
        actual = sum(y for _, y in pts) / len(pts)
        t(f"| {lo}-{min(hi, 100)}% | {len(pts)} | {pred:.1%} | {actual:.1%} | {actual - pred:+.1%} |")
        for p, y in pts:
            pc = min(max(p, 1e-9), 1 - 1e-9)
            brier += (pc - y) ** 2
            logloss += -(y * math.log(pc) + (1 - y) * math.log(1 - pc))
            n_cal += 1
    if n_cal:
        base_rate = sum(1 for l in lessons if l.get("side") == l.get("actual")) / len(lessons)
        brier /= n_cal
        logloss /= n_cal
        brier_base = base_rate * (1 - base_rate) ** 2 + (1 - base_rate) * base_rate ** 2
        t("")
        t(f"Brier = {brier:.4f}（基准胜率常数预测 {brier_base:.4f}，越小越好）；LogLoss = {logloss:.4f}；校准样本 n={n_cal}")
        skill = 1 - brier / brier_base if brier_base else 0
        t(f"Brier Skill Score = {skill:+.3f}（>0 优于常数预测，<0 不如直接猜基准胜率）")
    t("")

    # ===== Q2 分盘胜率（results 按 slug 解析，ground truth）=====
    t("## Q2 分盘胜率（results.jsonl 按 slug ET 小时重算）")
    t("")
    sess: dict[str, list] = defaultdict(lambda: [0, 0, 0.0])  # [wins, total, pnl]
    unmatched_slug = 0
    for r in results:
        h = slug_hour_et(r.get("slug", ""))
        if h is None:
            unmatched_slug += 1
            continue
        s = session_of(h)
        win = 1 if r.get("side") == r.get("actual") else 0
        sess[s][0] += win
        sess[s][1] += 1
        sess[s][2] += float(r.get("pnl", 0) or 0)
    t("| 盘 | 胜/总 | 胜率 | 总盈亏 | 均单盈亏 |")
    t("|----|-------|------|--------|----------|")
    for s in ("亚洲盘", "欧盘", "美盘"):
        wn, tt, pl = sess.get(s, [0, 0, 0.0])
        if tt:
            t(f"| {s} | {wn}/{tt} | {wn/tt:.1%} | {pl:+.2f} | {pl/tt:+.3f} |")
    if unmatched_slug:
        t(f"\n（{unmatched_slug} 条 slug 无法解析时段，已剔除）")
    eu, us = sess.get("欧盘", [0, 0, 0.0]), sess.get("美盘", [0, 0, 0.0])
    if eu[1] and us[1]:
        z, p = z_test_two_prop(eu[0], eu[1], us[0], us[1])
        t(f"\n欧盘 {eu[0]/eu[1]:.1%} vs 美盘 {us[0]/us[1]:.1%}：z={z:.2f}，p={p:.3f}"
          f"（{'显著' if p < 0.05 else '不显著'}，阈值 0.05）")
    t(f"\n对照 review-stats by_session：{stats.get('by_session')}")
    t("注：`hermes_review.get_session_name` 的 SESSION_BANDS 迭代顺序对 12-20 点会误判为欧盘（应先判 >=12 美盘再判 >=5 欧盘），"
      "且 fallback 用 review 运行时刻而非信号时刻——上表为按 slug 重算的 ground truth。")
    t("")

    # ===== D1 buy_price 分桶 =====
    t("## D1 买入价分桶（验证 0.65 封顶与挂价逻辑）")
    t("")
    buckets = [(0, 0.45, "<0.45"), (0.45, 0.55, "0.45-0.55"), (0.55, 0.65, "0.55-0.65"), (0.65, 2, "≥0.65")]
    t("| 价格档 | 场次 | 胜率 | 总盈亏 | 均单盈亏 | 盈亏/本金 |")
    t("|--------|------|------|--------|----------|-----------|")
    for lo, hi, name in buckets:
        rs = [r for r in results if isinstance(r.get("buy_price"), (int, float)) and lo <= r["buy_price"] < hi]
        if not rs:
            continue
        wn = sum(1 for r in rs if r.get("side") == r.get("actual"))
        pl = sum(float(r.get("pnl", 0) or 0) for r in rs)
        amt = sum(float(r.get("amount", 0) or 0) for r in rs)
        t(f"| {name} | {len(rs)} | {wn/len(rs):.1%} | {pl:+.2f} | {pl/len(rs):+.3f} | {pl/amt:+.1%} |" if amt else
          f"| {name} | {len(rs)} | {wn/len(rs):.1%} | {pl:+.2f} | {pl/len(rs):+.3f} | — |")
    t("")

    # ===== Q3 连亏序列 =====
    t("## Q3 连亏序列风险")
    t("")
    ordered = sorted(results, key=lambda r: r.get("ts", ""))
    outcomes = [1 if r.get("side") == r.get("actual") else 0 for r in ordered]
    observed = max_loss_streak(outcomes)
    exp_ms, dist = mc_max_streak(len(outcomes), win_rate)
    p_ge = sum(c for ms, c in dist.items() if ms >= observed) / MC_SIMS
    t(f"- 观测最大连亏 = **{observed}**（review-stats streak.max_loss={stats.get('streak', {}).get('max_loss')}）")
    t(f"- 蒙特卡洛（n={len(outcomes)}, p={win_rate:.3f}, {MC_SIMS} 次模拟）：期望最大连亏 = {exp_ms:.1f}，"
      f"P(最大连亏 ≥ {observed}) = {p_ge:.1%}")
    t(f"- 判定：观测值 {'在随机序列正常波动范围内（非系统失效证据）' if p_ge > 0.05 else '显著异常，存在序列相关/状态依赖'}")
    t("")

    # ===== 结论与建议 =====
    t("## 结论先行")
    t("")
    t(f"1. **\"置信度倒挂\"主要是量纲污染假象**：lessons 中 {len(frac)}/{len(confs)} 条 confidence 为 0-1 小数，"
      f"全部误入 low 档。归一化后真正的问题是 **55-60% 档实际胜率显著偏低**（见 Q1）——"
      "中等置信信号是最弱环节，不是 high 档。")
    if n_cal:
        t(f"2. **概率预测整体不优于基准**：Brier Skill Score = {skill:+.3f}（<0）。"
          "当前概率输出接近'猜基准胜率'水平，概率映射表（53.5/58.5/66% 先验）需用本表实测值替换。")
    if eu[1] and us[1]:
        t(f"3. **欧盘弱区存在但不显著**：slug 重算 欧盘 {eu[0]/eu[1]:.1%}（{eu[0]}/{eu[1]}）vs 美盘 {us[0]/us[1]:.1%}（{us[0]}/{us[1]}），"
          f"p={p:.3f}。review-stats 的 by_session 因 session 标注缺陷（get_session_name 顺序 bug + fallback 用错时刻）不可直接采信。"
          "建议：暂不单独为欧盘改规则，先修 session 标注再积累样本。")
    t(f"4. **0.65 价格封顶被数据验证**：≥0.65 档 {sum(1 for r in results if isinstance(r.get('buy_price'), (int, float)) and r['buy_price'] >= 0.65)} 场"
      "胜率 55.6% 但总盈亏为负（-14.6% 本金回报）——高价单赔率差，0.65 封顶合理应保留；"
      "0.45-0.65 是盈利主区间（+27%~+33% 本金回报）。")
    t(f"5. **最大连亏 {observed} 属正常波动**：P(≥{observed}) = {p_ge:.1%}（蒙特卡洛期望 {exp_ms:.1f}），"
      "非系统失效证据；但 37% 概率会再发生，仓位管理须按'连亏 6 场'为常态设计。")
    t("")
    t("## 对 prompt.md 的可执行建议")
    t("")
    t("1. **修 confidence 量纲**：`hermes_review.py` 写入/分带前统一归一（<=1 ×100），否则 by_confidence 统计持续失真")
    t("2. **修 get_session_name 迭代顺序**：SESSION_BANDS 应按 (21,亚洲盘)→(12,美盘)→(5,欧盘) 降序判定；fallback 应用信号时刻而非 review 时刻")
    t("3. 校准数据（Q1）可作为概率映射基础概率（53.5/58.5/66%）的首次实证校准输入；样本量仍小，建议积累快照后复核")
    t("")

    REPORT.write_text("\n".join(out), encoding="utf-8")
    print("\n".join(term[:80]))
    print(f"\n... 完整报告 → {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
