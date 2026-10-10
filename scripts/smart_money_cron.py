#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round50 v3 聪明钱账本定时入口 (launchd 每小时 :05 调用)

每小时结算上一小时市场 + 滚动更新盈利池(赶在 :30 分析前, 采集器拿到最新池子)。
不用 bash wrapper: macOS TCC 限制 launchd 执行 Desktop 下 shell 脚本
(实测 Operation not permitted), 照 com.hermes.bot 模式直接用 .venv python 跑。
日志: data/hermes_smart_money_cron.log
"""
import contextlib
import io
import sys
from datetime import datetime
from pathlib import Path

THIS = Path(__file__).resolve().parent
ROOT = THIS.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(THIS))

LOG = ROOT / "data" / "hermes_smart_money_cron.log"


def run_once() -> None:
    from smart_money_settle import main as settle_main
    from smart_money_pool import main as pool_main

    ts = datetime.now().strftime("%m-%d %H:%M:%S")
    lines = [f"[{ts}] === smart_money settle+pool 开始 ==="]
    buf = io.StringIO()
    # settle 失败不阻断 pool(幂等, 各自记录), 网络抖动下也尽量出池子
    with contextlib.redirect_stdout(buf):
        try:
            settle_main()
            settle_rc = 0
        except Exception as e:  # noqa: BLE001
            settle_rc = 1
            buf.write(f"settle 异常: {e}\n")
        try:
            pool_main()
            pool_rc = 0
        except Exception as e:  # noqa: BLE001
            pool_rc = 1
            buf.write(f"pool 异常: {e}\n")
    lines.append(buf.getvalue().rstrip())
    lines.append(f"[{datetime.now().strftime('%m-%d %H:%M:%S')}] === 结束 settle_rc={settle_rc} pool_rc={pool_rc} ===")
    with LOG.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    run_once()
