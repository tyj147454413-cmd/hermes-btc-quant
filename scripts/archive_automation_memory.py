#!/usr/bin/env python3
"""归档 automation memory.md：将完整历史追加到 archive 文件，重建精简版。

策略（遵循 .codebuddy/memory/MEMORY.md 铁律）：
- 完整历史 → memory_archive_YYYYMMDD.md（按起始日期命名）
- 重建精简版 memory.md = 头部警告 + 规则锚点区 + 最近 N 场执行记录

用法：python scripts/archive_automation_memory.py [--keep N]
"""
import os
import re
import sys
from datetime import date

MEM_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".codebuddy", "automations", "hermes-btc-hourly-analysis")
MEM_PATH = os.path.join(MEM_DIR, "memory.md")


def split_records(text):
    """把 memory.md 切分为 (header_block, records)。
    records 为列表，每项是包含 '## 2026-...' 起的完整记录段。
    规则锚点区在第一条 '## 2026-' 之前。
    """
    lines = text.splitlines()
    header = []
    records = []
    cur = []
    in_record = False
    for ln in lines:
        if ln.startswith("## 2026-"):
            if cur:
                records.append(cur)
            cur = [ln]
            in_record = True
        else:
            if in_record:
                cur.append(ln)
            else:
                header.append(ln)
    if cur:
        records.append(cur)
    return header, records


def record_date(rec):
    """从记录段第一行提取日期，如 '## 2026-08-15 23:32 (...)' → '2026-08-15'。"""
    m = re.match(r"## (\d{4}-\d{2}-\d{2})", rec[0])
    return m.group(1) if m else None


def main():
    keep = int(sys.argv[sys.argv.index("--keep") + 1]) if "--keep" in sys.argv else 6

    with open(MEM_PATH, "r", encoding="utf-8") as f:
        text = f.read()

    header, records = split_records(text)
    if not records:
        print("无执行记录，跳过归档")
        return

    # 最近一条记录作为 archive 命名基准
    newest = record_date(records[0])
    archive_path = os.path.join(MEM_DIR, f"memory_archive_{newest.replace('-', '')}.md")

    # 归档：追加完整记录（倒序 → 转为正序存档，最新在上则保持原样即可）
    with open(archive_path, "a", encoding="utf-8") as f:
        for rec in records:
            f.write("\n".join(rec) + "\n\n")
    archive_bytes = os.path.getsize(archive_path)

    # 重建精简版 memory.md
    keep_records = records[:keep]
    out = []
    out.append("# hermes-btc-hourly-analysis 执行记录")
    out.append("")
    out.append(f"> ⚠️ 本文件于 {date.today().isoformat()} 归档精简（完整历史已追加至同目录 `{os.path.basename(archive_path)}`）。")
    out.append("> 规则一律以 `prompt_exec.md` 为准（AI 执行版，当前锚点 round23），勿凭本文件记忆执行；完整变更历史见 `prompt.md`。")
    out.append("")
    # 头部（规则锚点区）若含旧的归档警告，替换为新警告
    new_header = [l for l in header if not l.startswith("> ⚠️ 本文件于")]
    for l in new_header:
        out.append(l)
    out.append("")
    out.append(f"## 最近执行记录（最近 {keep} 场，完整历史见归档）")
    for rec in keep_records:
        out.append("")
        out.append("\n".join(rec))

    with open(MEM_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")

    new_bytes = os.path.getsize(MEM_PATH)
    print(f"归档完成：")
    print(f"  archive 文件: {archive_path} ({archive_bytes} bytes)")
    print(f"  memory.md 重建: {new_bytes} bytes ({keep} 场记录)")


if __name__ == "__main__":
    main()
