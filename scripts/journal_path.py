#!/usr/bin/env python3
"""
journal_path.py — 复盘笔记 canonical 路径 / 上一小时路径 的确定性助手
（消除 0600/6am 命名漂移 + 跨日小时算错的随机性）

canonical 命名铁律：  BTC_1H_Trade_Journal/YYYY-MM-DD_{HHMM}_复盘.md   (24h %H%M)
  6am→0600 / 7am→0700 / 1pm→1300。禁止 6am/7am/6AM/12AM 等别名。

用法：
  python3 scripts/journal_path.py <slug>                 # 打印该 slug 的 canonical 笔记路径(相对库根)
  python3 scripts/journal_path.py <slug> --prev          # 打印上一小时 slug 及其 canonical 路径
  python3 scripts/journal_path.py <slug> --json          # 结构输出
"""
from __future__ import annotations
import re, sys, json
from datetime import date, timedelta

MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}
SLUG_RE = re.compile(r"^bitcoin-up-or-down-(\w+)-(\d+)-(\d{4})-(\d{1,2})(am|pm)-et$")
JOURNAL = "BTC_1H_Trade_Journal"


def _parts(slug):
    m = SLUG_RE.match(slug or "")
    if not m:
        return None
    mon, day, year, h12, ampm = m.groups()
    if MONTHS.get(mon) is None:
        return None
    h = int(h12) % 12
    if ampm == "pm":
        h += 12
    return date(int(year), MONTHS[mon], int(day)), h


def to_date(slug):
    p = _parts(slug)
    return p[0] if p else None


def hour_from(slug):
    p = _parts(slug)
    return p[1] if p else None


def slug_for(d, hour24):
    mon = next(n for n, i in MONTHS.items() if i == d.month)
    ampm = "am" if hour24 < 12 else "pm"
    h12 = hour24 % 12 or 12
    return f"bitcoin-up-or-down-{mon}-{d.day}-{d.year}-{h12}{ampm}-et"


def prev_slug(slug):
    d, h = _parts(slug)
    prev_h = h - 1
    prev_d = d
    if prev_h < 0:
        prev_h = 23
        prev_d = d - timedelta(days=1)
    return slug_for(prev_d, prev_h)


def canon_path(d, h):
    return f"{JOURNAL}/{d.isoformat()}_{h:02d}00_复盘.md"


def main():
    args = [a for a in sys.argv[1:]]
    if not args:
        print(__doc__)
        sys.exit(1)
    slug = args[0]
    as_json = "--json" in args
    want_prev = "--prev" in args or "--backfill" in args
    parts = _parts(slug)
    if parts is None:
        print(f"❌ 无法解析 slug: {slug}", file=sys.stderr)
        sys.exit(2)
    d, h = parts
    out = {"slug": slug, "date": d.isoformat(), "hour24": h,
           "canonical": canon_path(d, h)}
    if want_prev:
        ps = prev_slug(slug)
        pd, ph = _parts(ps)
        out["prev_slug"] = ps
        out["prev_canonical"] = canon_path(pd, ph)
        out["prev_date"] = pd.isoformat()
        out["prev_hour24"] = ph
    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print(f"slug        : {slug}")
        print(f"canonical   : {out['canonical']}   # 24h {h:02d}00")
        if want_prev:
            print(f"prev slug   : {ps}")
            print(f"prev canon  : {out['prev_canonical']}")


if __name__ == "__main__":
    main()