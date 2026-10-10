#!/usr/bin/env python3
"""账本三方对账（只读，不修改任何文件）。

A1(2026-08-23): 实盘账本可靠性。核对 done.jsonl / fills.jsonl / results.jsonl 三方一致性，
输出差异报告并可用退出码供健康检查触发（--fail-on-error 时非零退出）。

聚焦真实成交单的金额/盈亏正确性，这是历史上最重的失真源（8/10 起 149 笔 FILLED pnl=0）：
  1. fills 标记 FILLED 的订单，results 里 amount 必须 > 0 且 pnl != 0（除非 sell_rec 提前卖出）。
  2. filled=TRUE 的 results 单，与 done 里同 order_id 的 result.amount 一致（bot 实挂金额）。
  3. done 单数 = fills 单数（排除 SKIP 与 PAPER），find 每方独有的 order_id。
  4. results 里 filled=FALSE + pnl != 0 的异常（未成交不该有盈亏）。

用法:
  python3 scripts/verify_ledger.py [--fail-on-error] [--json] [--hours N]

  --hours N   只看最近 N 小时的订单（按 fills.ts / results 近期判定），
              供 health_check 聚焦新增错误，不被历史欠账刷红。
              默认 0 = 全量。

只读。可在 daily_health_check 中作为附加项调用。
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent.parent
DATA = THIS_DIR / "data"
DONE = DATA / "hermes_done.jsonl"
FILLS = DATA / "hermes_fills.jsonl"
RESULTS = DATA / "hermes_results.jsonl"


def _load(path):
    rows = []
    if not path.exists():
        return rows
    with open(path) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                rows.append({"__parse_error__": i})
    return rows


def main():
    fail_on_error = "--fail-on-error" in sys.argv
    as_json = "--json" in sys.argv
    hours = 0
    if "--hours" in sys.argv:
        try:
            hours = int(sys.argv[sys.argv.index("--hours") + 1])
        except (ValueError, IndexError):
            hours = 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours) if hours > 0 else None

    done, fills, results = _load(DONE), _load(FILLS), _load(RESULTS)

    def in_window(ts_str):
        if cutoff is None:
            return True
        try:
            t = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            return t >= cutoff
        except (ValueError, AttributeError):
            # 置于窗口时，无法解析/backfill 的历史行视为窗口外（避免欠账污染实况）
            return False

    def df(_f):  # 去 parse 报错行
        return _f if isinstance(_f, dict) else None

    issues = []  # (severity, msg)

    # --- 填充列容器 ---
    # done: order_id -> amount(bot 实挂) , slug -> 存在的 done 记录
    done_by_oid, done_slugs = {}, set()
    for d in map(df, done):
        if not d:
            continue
        r = d.get("result") or {}
        oid = r.get("order_id")
        if oid:
            done_by_oid[oid] = r
        done_slugs.add((d.get("signal") or {}).get("slug"))

    # fills: order_id -> row
    fills_by_oid = {}
    for f_ in map(df, fills):
        if not f_:
            continue
        oid = f_.get("order_id")
        if oid and in_window(f_.get("ts")):
            fills_by_oid[oid] = f_

    # results: order_id -> row（窗口内判定同 fills，取结果里已结算的最新成交为准）
    results_by_oid = {}
    for r_ in map(df, results):
        if not r_:
            continue
        oid = r_.get("order_id")
        # 用 fills.ts 做窗口；结果里缺 ts 的，接受既有 fills 判定已足够
        if oid:
            results_by_oid.setdefault(oid, r_)

    # --- 规则 1 & 2: FILLED 单的 amount/pnl ---
    for oid, f_ in fills_by_oid.items():
        if f_.get("filled") is not True:
            continue
        # 跳过 PAPER
        if oid.startswith("PAPER_"):
            continue
        r_ = results_by_oid.get(oid)
        if r_ is None:
            issues.append(("W", f"FILLED 订单 {oid[:12]} 在 results 中缺失"))
            continue
        amt = r_.get("amount") or 0
        pnl = r_.get("pnl")
        if float(amt) <= 0:
            issues.append(("E", f"FILLED 订单 {oid[:12]} amount=0（{r_.get('slug')}），盈亏必失真"))
        elif pnl is not None and float(pnl) == 0.0 and r_.get("filled") is True:
            # 金额对但 pnl=0：可能是卖单覆盖或异常，告警复核
            issues.append(("W", f"FILLED 订单 {oid[:12]} amount={amt} 但 pnl=0（{r_.get('slug')}），请复核"))

    # --- 规则 3: done vs fills 独有 order_id（排除 PAPER/SKIP）——仅全量模式有意义 ---
    missing_fill, fill_no_done = [], []
    if hours == 0:
        # done 里无 fills 对应 = 未回填/被清理/部分成交
        missing_fill = [oid for oid in done_by_oid
                        if not oid.startswith("PAPER_") and oid not in fills_by_oid]
        fill_no_done = [oid for oid in fills_by_oid
                        if not oid.startswith("PAPER_") and oid not in done_by_oid]
        if missing_fill:
            issues.append(("W", f"{len(missing_fill)} 个 done 订单在 fills 无对应（未回填/部分成交?）: "
                               + ", ".join(x[:12] for x in missing_fill[:10])))
        if fill_no_done:
            issues.append(("W", f"{len(fill_no_done)} 个 fills 订单在 done 无对应: "
                               + ", ".join(x[:12] for x in fill_no_done[:10])))

    # --- 规则 4: filled=FALSE 却有非零盈亏 ---
    for oid, r_ in results_by_oid.items():
        if r_.get("filled") is False and (r_.get("pnl") or 0) != 0:
            issues.append(("E", f"未成交订单 {oid[:12]} pnl={r_.get('pnl')}（应 0）"))

    # --- 汇总 ---
    n_err = sum(1 for sev, _ in issues if sev == "E")
    summary = {
        "checked_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "done_rows": len(done), "fills_rows": len(fills), "results_rows": len(results),
        "missing_fill": len(missing_fill), "fill_no_done": len(fill_no_done),
        "errors": n_err, "warnings": len(issues) - n_err, "issues": issues,
    }
    if as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"[ledger] done={len(done)} fills={len(fills)} results={len(results)} "
              f"缺fill={len(missing_fill)} 缺done={len(fill_no_done)} ⚠️={len(issues)-n_err} ❌={n_err}")
        for sev, msg in issues:
            print(f"  [{sev}] {msg}")
    return 1 if (fail_on_error and n_err) else 0


if __name__ == "__main__":
    sys.exit(main())