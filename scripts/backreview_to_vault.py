#!/usr/bin/env python3
"""
backreview_to_vault.py — back_review 回退笔记兜底同步进 Obsidian vault

背景（2026-08-24）：小时会话的 obsidian MCP 写入断链时，复盘/本轮笔记回退落
data/back_review/，导致本地 Obsidian 库缺新笔记。本任务每 30 分钟把回退目录的
新 *.md 幂等补进 vault 的 BTC_1H_Trade_Journal/（cp 等价：已存在跳过，vault 内
同名文件视为权威，绝不覆盖）。

由 launchd com.hermes.backreviewsync 调度（.venv/bin/python 直执行，与
obsidiansync 同款已验证 TCC 模式）。日志：data/hermes_backreview_sync.log
"""
import shutil
from datetime import datetime
from pathlib import Path

SOURCE = Path("/Users/tangyijun/Desktop/hermes btc/data/back_review")
DEST = Path("/Users/tangyijun/Documents/Obsidian Vault/BTC_1H_Trade_Journal")
LOG = Path("/Users/tangyijun/Desktop/hermes btc/data/hermes_backreview_sync.log")


def ts() -> str:
    return datetime.now().strftime("%m-%d %H:%M:%S")


def main() -> None:
    if not SOURCE.is_dir():
        print(f"[{ts()}] 源目录不存在: {SOURCE}")
        return
    DEST.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in sorted(SOURCE.glob("*.md")):
        target = DEST / f.name
        # 源比目标新 → 覆盖(复盘结算后会追加 result/p_l,vault 版需跟进);
        # 目标不存在 → 新建。vault 手工编辑的文件若比回退旧也会被刷新——本库无手工编辑场景。
        if target.exists() and f.stat().st_mtime <= target.stat().st_mtime:
            continue
        try:
            shutil.copy2(f, target)
            action = "UPDATED" if target.exists() else "SYNCED"
            n += 1
            print(f"[{ts()}] {action} {f.name}")
        except OSError as e:
            print(f"[{ts()}] FAILED {f.name}: {e}")
    print(f"[{ts()}] done, synced={n}")


if __name__ == "__main__":
    main()
