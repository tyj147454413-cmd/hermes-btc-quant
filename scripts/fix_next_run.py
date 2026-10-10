#!/usr/bin/env python3
"""
Watchdog: 每小时校准 Hermes automation 的 next_run_at 到 rrule 指定的 BYMINUTE 分钟。
CodeBuddy 引擎在每次执行完成后会用"完成时间 + INTERVAL"覆盖 next_run_at，
导致调度漂移（并因 BYMINUTE 时区解释缺陷无法准点触发）。本脚本提前于目标分钟执行，
强制 next_run_at = 当前 CST 小时的下一个 BYMINUTE 对应的 epoch 毫秒（绝对时间戳）。

冻结策略（修复 2026-08-10 漂移/漏跑两类竞态）：
- 若 next_run_at 的分钟已是 BYMINUTE（合法 :13）→ 永不覆盖（冻结）。
  * Bug A（倒退）：引擎漂移值被设回"已过期的 :13"→ 引擎 30s tick 见 14:13<now 下一 tick 才触发 → 迟到。
  * Bug B（越位）：合法 :13 在引擎触发前被推进到下一小时（15:13→16:13）→ 整点漏跑。
- 仅当 next_run_at 是非 :13 的引擎漂移值时，才校正为"now 之后严格未来的下一个 :13"（绝不写过去）。
"""
import re
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

DB_PATH = "/Users/tangyijun/Library/Application Support/CodeBuddy CN/automations/automations.db"
AUTOMATION_ID = "hermes-btc-hourly-analysis"
CST = timezone(timedelta(hours=8))
DEFAULT_MINUTE = 28


def extract_minute(rrule):
    """从 rrule 解析 BYMINUTE 值，缺省回退到 DEFAULT_MINUTE。"""
    if not rrule:
        return DEFAULT_MINUTE
    m = re.search(r"BYMINUTE=(\d+)", rrule, re.IGNORECASE)
    if not m:
        return DEFAULT_MINUTE
    try:
        minute = int(m.group(1))
    except ValueError:
        return DEFAULT_MINUTE
    return minute if 0 <= minute <= 59 else DEFAULT_MINUTE


def main():
    try:
        # timeout=5 (busy_timeout)：与引擎进程并发写库时最多等待 5s，避免 "database is locked"
        conn = sqlite3.connect(DB_PATH, timeout=5)
        cur = conn.execute(
            "SELECT next_run_at, rrule FROM automations WHERE id = ?",
            (AUTOMATION_ID,),
        )
        row = cur.fetchone()
        if row is None:
            print(f"[fix_next_run] 未找到 automation: {AUTOMATION_ID}", file=sys.stderr)
            return 1

        old_ts, rrule = row
        minute = extract_minute(rrule)

        now = datetime.now(CST)
        # 计算当前小时的下一个 :BYMINUTE（例如 :05 且 22:03 → 22:05，22:39 → 22:05 已过 → 23:05）
        target = now.replace(minute=minute, second=0, microsecond=0)
        if target <= now:
            # 当前小时的 :BYMINUTE 已过，取下一小时
            target = target + timedelta(hours=1)

        ts_ms = int(target.timestamp() * 1000)

        old_dt = datetime.fromtimestamp(old_ts / 1000, tz=CST)
        if old_ts == ts_ms:
            # 无需校准：静默返回，避免每分钟 ~52000 条日志膨胀
            return 0
        if old_dt.minute == minute:
            # 冻结：next_run_at 已是合法 :BYMINUTE → 绝不覆盖。
            # 同时消除 Bug A（不倒退已对齐的 :13）与 Bug B（不在引擎触发前
            # 把待触发的 :13 越位推进到下一小时，避免整点漏跑）。
            return 0

        conn.execute(
            "UPDATE automations SET next_run_at = ? WHERE id = ?",
            (ts_ms, AUTOMATION_ID),
        )
        conn.commit()
        print(
            f"[fix_next_run] 已校准: {old_dt.strftime('%H:%M:%S')} → {target.strftime('%H:%M:%S')}"
        )
        return 0
    except Exception as e:
        print(f"[fix_next_run] 错误: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
