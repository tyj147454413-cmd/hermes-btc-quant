#!/bin/bash
# Hermes 1H Bot 启动脚本(同 dashboard.py 的 venv)
cd "$(dirname "$0")"

# 清掉所有可能冲突的 proxy + Python 路径污染
unset ALL_PROXY all_proxy HTTP_PROXY HTTPS_PROXY http_proxy https_proxy SOCKS_PROXY socks_proxy
# 注意: 不能设 NO_PROXY=*,会让 urllib 跳过代理导致 Polymarket 直连失败
# export NO_PROXY="*" no_proxy="*"  # 已禁用:会让 bot 无法访问 Polymarket
unset PYTHONPATH  # 防止 hermes 旧 venv 污染

# 用 .venv 的 Python(装 polymarket-client + py_clob_client_v2)
PY="./.venv/bin/python"

# 后台启动
nohup "$PY" hermes_1h_bot.py > data/hermes_bot_launchd.log 2>&1 &
sleep 5

ps aux | grep -v grep | grep "hermes_1h_bot.py" | head -1
echo "✅ Bot 启动(用 .venv 的 Python 3.13 + polymarket-client)"
echo "📄 日志: tail -f data/hermes_bot_launchd.log"
echo "📋 队列: data/claude_signal.jsonl (信号写入器写; hermes_last_signal.json 仅供 UI 展示)"
echo "🌐 仪表盘: http://localhost:5050 (dashboard.py 在另一个端口)"
