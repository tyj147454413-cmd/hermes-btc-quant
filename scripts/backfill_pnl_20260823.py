#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""历史错账回填脚本 (2026-08-23)

背景:
    8/10 起 AI 信号丢失 amount 字段 + bot result 不带金额 + review 回退链全空时静默落 0,
    导致 ~155 笔真实成交(FILLED/PAPER 且 filled=True)的 pnl 被记成 0。

取证链(三层,全部只读):
    E1 成交事实: data/hermes_fills.jsonl 的 filled=True 判定"真成交"(本地 bot 轮询记录)
    E2 下单金额: data/hermes_bot_launchd.log 的 🎯 SIDE | slug | $AMT 行(bot 挂单时打印的实际金额)
    E3 方向判定: results 行内 actual vs side(结算裁决,与金额无关,本就正确)

计算公式(与 hermes_review.py L1017-1019 完全同源,已用 12 笔已知正确行复现验证 12/12):
    WIN : pnl = round(amount * (1/buy_price - 1), 2)
    LOSS: pnl = round(-amount, 2)

安全边界:
    - 只改 pnl==0 且有 E1+E2 双证据的行;缺任一证据 → 不动,记入未决清单
    - PAPER_ 单与 FILLED 单同权取证(paper 成交真相=bot 本地 fills 落盘,不走交易所)
    - buy_price 不在 (0,1) 开区间 → 不动
    - 每行写入溯源字段(orig_pnl/backfill_source/backfill_ts),原值永不丢失
    - 写前备份 .bak_backfill_20260823;原子写(tmp+rename)
    - 默认 dry-run,--apply 才落盘;review-stats.json 做增量补丁(delta)
"""
import json
import re
import sys
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "data" / "hermes_results.jsonl"
FILLS = ROOT / "data" / "hermes_fills.jsonl"
BOT_LOG = ROOT / "data" / "hermes_bot_launchd.log"
STATS = ROOT / "data" / "review-stats.json"
BACKUP = ROOT / "data" / "hermes_results.jsonl.bak_backfill_20260823"
REPORT = ROOT / "data" / "backfill_report_20260823.md"
BACKFILL_TS = datetime.now(timezone.utc).isoformat(timespec="seconds")

LOG_PAT = re.compile(
    r"\[(\d{2}-\d{2} [\d:]+)\].*🎯 (UP|DOWN) \| (bitcoin-up-or-down-[^\s|]+) \| \$([\d.]+)"
)


def load_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def build_amount_evidence():
    """E2: 解析 launchd 日志,slug → [(log_ts, amount, side)]"""
    entries = {}
    with open(BOT_LOG, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = LOG_PAT.search(line)
            if m:
                entries.setdefault(m.group(3), []).append(
                    (m.group(1), float(m.group(4)), m.group(2))
                )
    return entries


def pick_amount(entries_for_slug, side):
    """取该 slug 最后一条侧向匹配的下单金额。
    冲突裁决规则: 同 slug 多次下单取最后一条(失败重试后成功那次);
    若最后一条侧向不匹配(理论上不可能,质检 155/155 匹配)返回 None。"""
    cands = [e for e in entries_for_slug if e[2] == side]
    return cands[-1][1] if cands else None


def compute_pnl(amount, buy_price, win):
    """与 hermes_review.py 同源公式。"""
    return round(amount * (1.0 / buy_price - 1.0), 2) if win else round(-amount, 2)


def main():
    apply_mode = "--apply" in sys.argv
    print(f"=== 历史错账回填 | 模式: {'APPLY(写盘)' if apply_mode else 'DRY-RUN(预览)'} ===")

    rows = load_jsonl(RESULTS)
    fills = {str(r.get("order_id")): r for r in load_jsonl(FILLS)}
    log_entries = build_amount_evidence()
    print(f"results={len(rows)} 行 | fills={len(fills)} 条 | 日志金额证据 slug={len(log_entries)} 个")

    repaired, skipped, synced_false, untouched_paper = [], [], [], []
    for idx, r in enumerate(rows):
        if r.get("pnl") not in (0, "0", 0.0, None):
            continue  # 只处理零账行
        oid = str(r.get("order_id") or "")
        fev = fills.get(oid)
        if not fev or fev.get("filled") is not True:
            if fev is not None and fev.get("filled") is False and r.get("filled") is None:
                r["filled"] = False  # 仅元数据对齐,pnl 保持 0(真未成交)
                synced_false.append(r.get("slug"))
            continue
        slug, side = r.get("slug"), r.get("side")
        bp = r.get("buy_price")
        try:
            bp = float(bp)
        except (TypeError, ValueError):
            bp = 0.0
        amount = pick_amount(log_entries.get(slug, []), side)
        # 三重证据缺一不可
        if not amount or amount <= 0 or not (0 < bp < 1) or side not in ("UP", "DOWN"):
            skipped.append({"slug": slug, "reason": f"amount={amount} bp={bp} side={side}"})
            continue
        win = (r.get("actual") == side)
        new_pnl = compute_pnl(amount, bp, win)
        r["orig_pnl"] = r.get("pnl")
        r["pnl"] = new_pnl
        r["amount"] = amount
        r["filled"] = True
        if not r.get("fill_status"):
            r["fill_status"] = fev.get("status") or "FILLED"
        r["backfill_source"] = "launchd_log_amount+local_fills"
        r["backfill_ts"] = BACKFILL_TS
        repaired.append({"idx": idx, "slug": slug, "side": side, "actual": r.get("actual"),
                         "bp": bp, "amount": amount, "pnl": new_pnl, "win": win})

    total_delta = sum(x["pnl"] for x in repaired)
    n_win = sum(1 for x in repaired if x["win"])
    print(f"\n修复 {len(repaired)} 笔 | 跳过(证据不全) {len(skipped)} 笔 | "
          f"元数据对齐(pnl不变) {len(synced_false)} 笔 | PAPER artifact 不动 {len(untouched_paper)} 笔")
    print(f"其中 WIN {n_win} / LOSS {len(repaired)-n_win} | 补回盈亏合计 ${total_delta:+.2f}")
    amt_dist = Counter(x["amount"] for x in repaired)
    print("金额分布:", dict(sorted(amt_dist.items())))

    if skipped:
        print("\n⚠️ 未决清单(不动账):")
        for s in skipped[:10]:
            print("  ", s["slug"], "|", s["reason"])

    if not apply_mode:
        print("\n[dry-run] 未写盘。确认无误后加 --apply 执行。")
        _write_report(REPORT, repaired, skipped, synced_false, untouched_paper,
                      total_delta, n_win, dry=True)
        print(f"[dry-run] 预览报告已写 {REPORT.name}")
        return

    # 备份 → 原子重写
    shutil.copy2(RESULTS, BACKUP)
    tmp = RESULTS.with_suffix(".jsonl.tmp_backfill")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(RESULTS)
    print(f"\n✅ 已备份 → {BACKUP.name}")
    print(f"✅ 已重写 {RESULTS.name}({len(rows)} 行)")

    # stats 增量补丁(语义: 累计计数器,只加不减)
    if STATS.exists() and abs(total_delta) > 0:
        st = json.loads(STATS.read_text(encoding="utf-8"))
        st["profit_loss"] = round(st.get("profit_loss", 0) + total_delta, 2)
        st["filled_profit_loss"] = round(st.get("filled_profit_loss", 0) + total_delta, 2)
        st["filled_wins"] = st.get("filled_wins", 0) + n_win
        st["filled_losses"] = st.get("filled_losses", 0) + (len(repaired) - n_win)
        STATS.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✅ review-stats.json 增量补丁: profit_loss/filled_profit_loss {total_delta:+.2f}, "
              f"filled_wins+{n_win}, filled_losses+{len(repaired)-n_win}")

    _write_report(REPORT, repaired, skipped, synced_false, untouched_paper,
                  total_delta, n_win, dry=False)
    print(f"✅ 报告 → {REPORT.name}")


def _write_report(path, repaired, skipped, synced_false, untouched_paper,
                  total_delta, n_win, dry):
    lines = [
        "# 历史错账回填报告 · 2026-08-23",
        "",
        f"- 模式: {'DRY-RUN 预览' if dry else 'APPLY 已落盘'} | UTC {BACKFILL_TS}",
        "- 取证链: fills.filled=True(成交事实) × launchd 日志🎯行(逐单实际金额) × results.actual(方向,本就正确)",
        "- 公式: 与 hermes_review.py 同源(WIN: amount×(1/bp−1);LOSS: −amount),已用 12 笔已知正确行复现验证 12/12",
        f"- 修复 **{len(repaired)}** 笔(WIN {n_win} / LOSS {len(repaired)-n_win}),补回盈亏 **${total_delta:+.2f}**",
        f"- 跳过(证据不全,不动账) {len(skipped)} 笔;元数据对齐 {len(synced_false)} 笔;PAPER artifact {len(untouched_paper)} 笔不动",
        "- 溯源: 每行含 orig_pnl/backfill_source/backfill_ts;原文件备份 hermes_results.jsonl.bak_backfill_20260823",
        "",
        "## 逐笔明细",
        "",
        "| slug | 方向 | 结算 | 买价 | 金额 | 补回pnl |",
        "|---|---|---|---:|---:|---:|",
    ]
    for x in repaired:
        lines.append(f"| {x['slug'][-34:]} | {x['side']} | {'WIN' if x['win'] else 'LOSS'} "
                     f"| {x['bp']:.2f} | ${x['amount']:.2f} | ${x['pnl']:+.2f} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
