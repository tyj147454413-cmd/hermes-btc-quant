#!/usr/bin/env python3
"""
Hermes 日志轮转（D2 / 2026-08-23）。

背景：hermes_app.log 6.6MB / review_cron 1.7MB / collect_cron 1.3MB 无限增长，
磁盘一坏 = 最近全部工作 + 交易账本丢失。macOS newsyslog 只配了 dashboard.log，
其余日志无任何轮转。

本工具（项目内自包含，无需 root / 无需改系统配置）：
  - 对 data/*.log、data/logs/*.log、logs/*.log 中超过 --max-size 的文件：
      1) 把旧代次向后推（.1.gz→.2.gz→…→.K.gz，K=--keep）；
      2) 将当前日志内容 gzip 压缩为 <name>.1.gz（全量历史留存，非丢弃）；
      3) 将活动日志文件截断为 0（对以 O_APPEND/'a'/`>>` 追加写入的进程安全，
         后续写入仍落在文件末尾，不会产生稀疏洞）；
  - 删除早于 --retain-days 天的压缩归档。

安全声明：
  - 只截断超过阈值的日志文件、只新增/删除本工具自己的压缩归档；
  - 不删除、不修改任何业务数据文件（snapshots/jsonl 账本不入本轮转范围）；
  - 截断采用先复制压缩、后 truncate，避免在进程写入中途丢数据。

用法：
  python3 scripts/rotate_logs.py                 # 默认阈值 3MB / 保留4代 / 14天
  python3 scripts/rotate_logs.py --max-size 5    # 阈值改为 5MB(MB)
  python3 scripts/rotate_logs.py --dry-run       # 只打印将要做什么，不实际执行
"""
from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_HERMES_DIR = os.environ.get("HERMES_DIR")
ROOT = Path(_HERMES_DIR) if _HERMES_DIR else Path("/Users/tangyijun/Desktop/hermes btc")

LOG_GLOBS = [
    str(ROOT / "data" / "*.log"),
    str(ROOT / "data" / "logs" / "*.log"),
    str(ROOT / "logs" / "*.log"),
]


def _candidates() -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for g in LOG_GLOBS:
        for p in Path(ROOT).glob(g.replace(str(ROOT), ".")):
            key = str(p.resolve())
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
    return out


def rotate_one(log: Path, max_bytes: int, keep: int, dry_run: bool) -> str | None:
    """轮转单个日志。返回动作描述；未触发返回 None。"""
    try:
        size = log.stat().st_size
    except OSError:
        return None
    if size < max_bytes:
        return None
    actions = []

    def prc(p: Path) -> Path:
        return Path(str(p) + ".gz")

    # 1) 代次后推（.1.gz → .2.gz → …，超出 keep 的删除）
    parents = list(range(1, keep + 2))
    for i in sorted(parents, reverse=True):
        cur = prc(Path(str(log) + f".{i}"))
        if cur.exists():
            if i >= keep:
                if not dry_run:
                    cur.unlink()
                actions.append(f"删旧归档 {cur.name}")
            else:
                nxt = prc(Path(str(log) + f".{i+1}"))
                if nxt.exists() and not dry_run:
                    nxt.unlink()
                if not dry_run:
                    shutil.move(str(cur), str(nxt))
                actions.append(f"轮档 {cur.name}->{nxt.name}")
    # 2) 压缩当前内容 → <name>.1.gz
    dst = prc(Path(str(log) + ".1"))
    if not dry_run:
        with open(log, "rb") as src, gzip.open(str(dst), "wb") as gz:
            shutil.copyfileobj(src, gz, length=1 << 20)
        # 3) 截断活动日志为 0（O_APPEND 写入安全）
        with open(log, "wb"):
            pass
        actions.append(f"压缩 {log.name}({size}B)->{dst.name} 后截断为0")
    else:
        actions.append(f"将压缩 {log.name}({size}B)->{dst.name} 并截断")
    return "; ".join(actions)


def main() -> int:
    ap = argparse.ArgumentParser(description="Hermes 日志轮转（D2）")
    ap.add_argument("--max-size", type=float, default=3.0, help="轮转阈值 MB（默认 3）")
    ap.add_argument("--keep", type=int, default=4, help="保留压缩代次（默认 4）")
    ap.add_argument("--retain-days", type=int, default=14, help="超出保留天数的归档删除")
    ap.add_argument("--dry-run", action="store_true", help="只打印不执行")
    args = ap.parse_args()

    max_bytes = int(args.max_size * 1024 * 1024)
    rotated = 0
    for log in _candidates():
        res = rotate_one(log, max_bytes, args.keep, args.dry_run)
        if res:
            print(f"  ↻ {log.name}: {res}")
            rotated += 1

    # 清理过期归档（B-fix 2026-08-23: 归档实际命名为 <name>.N.gz，
    #   旧代码用 `Path(str(log)+".gz").name + ".*.gz"`(即 XXX.log.gz.*.gz)永远匹配不到，retain-days 是死逻辑）
    now = datetime.now(timezone.utc).timestamp()
    removed = 0
    for log in _candidates():
        for old in sorted(log.parent.glob(log.name + ".*.gz")):
            try:
                if now - old.stat().st_mtime > args.retain_days * 86400:
                    if not args.dry_run:
                        old.unlink()
                    print(f"  ⏳ 清理过期归档 {old.name}")
                    removed += 1
            except OSError:
                continue

    print(f"日志轮转完成: 轮转{rotated}个, 清理过期{removed}个{'(dry-run)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())