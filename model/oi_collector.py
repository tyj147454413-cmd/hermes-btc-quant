#!/usr/bin/env python3
"""OI（持仓量）+ 期现基差 持续采集器（alpha 候选·信息型，ROADMAP P1.3）。

为什么必须持续采：
    - fapi openInterestHist 只保留最近 30 天 → 不可回补，现在不采以后永远没有。
    - OI 是「拥挤度/变盘前兆」类特征的核心原料（配合资金费率看多空拥挤）。

两条数据流（幂等追加，重复触发不写双份）：
    data/oi_hist.jsonl      每 5 分钟一行（hist 端点可回看 ~41h，容错窗口大）
        {ts_utc, oi_btc, oi_usd}
    data/premium_hist.jsonl 每次运行一行当前快照（计划任务每小时 1 次 → 1h 粒度）
        {ts_utc, mark_price, index_price, basis_bp, funding_rate}
        basis_bp = (mark - index) / index * 10000，正=期货升水

调度（生产机，与现有任务同风格）：
    Windows 计划任务 HermesBTCOICollect，每小时触发：
    D:\hermes-btc\.venv\Scripts\python.exe D:\hermes-btc\model\oi_collector.py

用法：
    .venv/Scripts/python model/oi_collector.py            # 正常采集
    .venv/Scripts/python model/oi_collector.py --limit 24 # 拉最近24个5m周期（默认500）
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hermes_common import http_get_json, log  # noqa: E402

FAPI = "https://fapi.binance.com"
OI_HIST = ROOT / "data" / "oi_hist.jsonl"
PREMIUM = ROOT / "data" / "premium_hist.jsonl"
SYMBOL = "BTCUSDT"


def load_ts_set(path: Path) -> set[str]:
    """已有记录的 ts_utc 集合（幂等去重用）。"""
    if not path.exists():
        return set()
    import json
    out = set()
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        try:
            out.add(json.loads(line)["ts_utc"])
        except (json.JSONDecodeError, KeyError):
            continue
    return out


def fetch_oi_hist(limit: int) -> list[dict]:
    """拉 5m 粒度 OI 历史（最多回看 ~41h）。"""
    data = http_get_json(
        f"{FAPI}/futures/data/openInterestHist"
        f"?symbol={SYMBOL}&period=5m&limit={limit}", timeout=15)
    if not data:
        return []
    rows = []
    for r in data:
        ts = datetime.fromtimestamp(int(r["timestamp"]) / 1000, tz=timezone.utc)
        rows.append({"ts_utc": ts.isoformat(),
                     "oi_btc": float(r["sumOpenInterest"]),
                     "oi_usd": float(r["sumOpenInterestValue"])})
    return rows


def fetch_premium() -> dict | None:
    """当前 mark/index/资金费率 → 基差快照。"""
    r = http_get_json(f"{FAPI}/fapi/v1/premiumIndex?symbol={SYMBOL}", timeout=10)
    if not r or "markPrice" not in r:
        return None
    mark, index = float(r["markPrice"]), float(r["indexPrice"])
    ts = datetime.fromtimestamp(int(r["time"]) / 1000, tz=timezone.utc)
    return {"ts_utc": ts.isoformat(),
            "mark_price": mark,
            "index_price": index,
            "basis_bp": round((mark - index) / index * 10000, 2),
            "funding_rate": float(r.get("lastFundingRate", 0))}


def append_jsonl(path: Path, rows: list[dict]) -> None:
    import json
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main() -> int:
    import json

    ap = argparse.ArgumentParser(description="OI+基差持续采集")
    ap.add_argument("--limit", type=int, default=500, help="OI hist 拉取条数（默认500≈41h）")
    args = ap.parse_args()

    # ---- OI 5m 历史（幂等） ----
    hist = fetch_oi_hist(args.limit)
    if not hist:
        log("[oi] [X] openInterestHist 拉取失败（网络/代理）", level="E")
        return 1
    known = load_ts_set(OI_HIST)
    new_hist = [r for r in hist if r["ts_utc"] not in known]
    append_jsonl(OI_HIST, new_hist)

    # ---- premium 快照（每次一行，ts 唯一） ----
    prem = fetch_premium()
    new_prem = []
    if prem and prem["ts_utc"] not in load_ts_set(PREMIUM):
        new_prem = [prem]
        append_jsonl(PREMIUM, new_prem)

    total_oi = len(known) + len(new_hist)
    latest_oi = hist[-1]
    log(f"[oi] [OK] +{len(new_hist)} OI行(共{total_oi}) | 最新 oi_btc="
        f"{latest_oi['oi_btc']:.0f} @ {latest_oi['ts_utc'][:16]}"
        + (f" | basis={prem['basis_bp']:+.1f}bp funding={prem['funding_rate']:+.5f}"
           if prem else ""))
    print(json.dumps({"new_oi": len(new_hist), "total_oi": total_oi,
                      "new_premium": len(new_prem),
                      "latest_oi_btc": latest_oi["oi_btc"],
                      "latest_basis_bp": prem["basis_bp"] if prem else None},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
