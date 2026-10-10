#!/usr/bin/env bash
# scripts/backup_data.sh — Hermes BTC 数据自动备份（D1/2026-08-23）
#
# 用途: 把交易数据备份到第二位置(项目文件夹之外)，防单点损坏。
#   1) rsync 镜像 data/ + BTC_1H_Trade_Journal/ → ~/hermes_btc_backup/
#      (不带 --delete: 项目里删掉的东西在备份侧保留, 符合"不允许删除"精神)
#   2) 核心台账(done/fills/results/signal/pending/sells/stats/reviewed 等 jsonl/json)
#      打带时间戳的 tar.gz 快照 → ~/hermes_btc_backup/snapshots/
#   3) 快照保留最近 KEEP=30 份，更旧的自动清理(仅清备份区, 不动项目文件夹)
#
# 用法:
#   bash scripts/backup_data.sh            # 手动跑一次
#   crontab: 30 3 * * * /bin/bash "/Users/tangyijun/Desktop/hermes btc/scripts/backup_data.sh" >> ... 2>&1
#     (每日 03:30 自动跑; 若用 launchd 见 scripts/com.hermes.backup.plist 模板)
#
# 数据真实性: 纯文件复制, 不改写任何数据内容。

set -u
KEEP=30

# 路径含空格 → 全程引号；脚本自定位项目根(scripts/ 的上级)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_DIR="$(dirname "$SCRIPT_DIR")"
DEST="$HOME/hermes_btc_backup"
SNAP_DIR="$DEST/snapshots"
LOG="$DEST/backup.log"

mkdir -p "$DEST/data" "$DEST/BTC_1H_Trade_Journal" "$SNAP_DIR"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

log "=== 备份开始 | 源=$HERMES_DIR → 目标=$DEST ==="

# 1. rsync 镜像(增量, 只传变化)
rsync -a --exclude '.DS_Store' --exclude '__pycache__' \
    "$HERMES_DIR/data/" "$DEST/data/"
RSYNC_DATA_RC=$?
rsync -a --exclude '.DS_Store' \
    "$HERMES_DIR/BTC_1H_Trade_Journal/" "$DEST/BTC_1H_Trade_Journal/"
RSYNC_JOURNAL_RC=$?
log "rsync data rc=$RSYNC_DATA_RC, journal rc=$RSYNC_JOURNAL_RC"

# 2. 核心台账快照 tar.gz(带时间戳)
TS="$(date '+%Y%m%d_%H%M')"
SNAP="$SNAP_DIR/hermes_ledgers_$TS.tar.gz"
CORE_FILES=""
for f in hermes_done.jsonl hermes_fills.jsonl hermes_results.jsonl \
         claude_signal.jsonl hermes_pending.jsonl hermes_sells.jsonl \
         hermes_reviewed.json hermes_stats.json hermes_lessons.json \
         hermes_chase_alerts.jsonl bot_config.json; do
    [ -f "$HERMES_DIR/data/$f" ] && CORE_FILES="$CORE_FILES data/$f"
done
if [ -n "$CORE_FILES" ]; then
    # -C 进项目根, 归档内路径保持 data/xxx 相对结构
    if tar czf "$SNAP" -C "$HERMES_DIR" $CORE_FILES 2>/dev/null; then
        log "台账快照 OK: $(basename "$SNAP") ($(du -h "$SNAP" | cut -f1))"
    else
        log "⚠️ 台账快照失败 rc=$?"
    fi
else
    log "⚠️ 未找到任何核心台账文件, 跳过快照"
fi

# 3. 清理旧快照(仅备份区), 保留最近 KEEP 份
OLD_COUNT=$(ls -1t "$SNAP_DIR"/hermes_ledgers_*.tar.gz 2>/dev/null | tail -n +$((KEEP+1)) | wc -l | tr -d ' ')
if [ "$OLD_COUNT" -gt 0 ]; then
    ls -1t "$SNAP_DIR"/hermes_ledgers_*.tar.gz | tail -n +$((KEEP+1)) | while IFS= read -r old; do
        rm -f "$old"
    done
    log "清理 $OLD_COUNT 份过期快照(保留最近 $KEEP 份)"
fi

# 4. 结果摘要
DATA_SIZE=$(du -sh "$DEST/data" 2>/dev/null | cut -f1)
SNAP_TOTAL=$(ls -1 "$SNAP_DIR"/hermes_ledgers_*.tar.gz 2>/dev/null | wc -l | tr -d ' ')
log "=== 备份完成 | data镜像=$DATA_SIZE | 快照累计=${SNAP_TOTAL}份 ==="
