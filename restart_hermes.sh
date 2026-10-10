#!/bin/bash
# Hermes 一键启动脚本 (8/16 v4 - claude 接入)
# 用法: 在终端复制粘贴执行
#   bash "/Users/tangyijun/Desktop/hermes btc/restart_hermes.sh"

echo "=========================================="
echo "🚀 Hermes 一键启动 (claude 信号接入版)"
echo "=========================================="

# 1. 确保 symlink:让 bot 读 /tmp/hermes_signal.jsonl 时实际读到 claude_signal.jsonl
echo "--- [1/4] 建立信号文件链接 ---"
ln -sf "/Users/tangyijun/Desktop/hermes btc/data/claude_signal.jsonl" /tmp/hermes_signal.jsonl
ls -la /tmp/hermes_signal.jsonl 2>&1
echo "✅ 信号链接就绪"

# 2. 杀掉旧 bot(launchd 若配置 keepalive 会自动拉起;否则手动启动)
echo "--- [2/4] 重启 bot ---"
pkill -f "hermes_1h_bot.py" 2>/dev/null
sleep 2

# 3. 用 launchd 重启 bot(标准方式);失败则手动 nohup 启动
LAUNCHD_OK=0
launchctl kickstart -k gui/$(id -u)/com.hermes.bot 2>/dev/null && LAUNCHD_OK=1
sleep 3
if ps aux | grep -v grep | grep "hermes_1h_bot.py" > /dev/null 2>&1; then
    echo "✅ bot 运行中 (launchd)"
else
    echo "⚠️ launchd 未拉起,手动启动..."
    cd "/Users/tangyijun/Desktop/hermes btc"
    nohup ./.venv/bin/python hermes_1h_bot.py >> data/hermes_bot_launchd.log 2>&1 &
    sleep 4
    if ps aux | grep -v grep | grep "hermes_1h_bot.py" > /dev/null 2>&1; then
        echo "✅ bot 已手动启动"
    else
        echo "❌ bot 启动失败,请手动检查"
    fi
fi

# 4. 确认 dashboard 在跑(UI 用)
echo "--- [3/4] 检查 dashboard ---"
if ps aux | grep -v grep | grep "dashboard.py" > /dev/null 2>&1; then
    echo "✅ dashboard 运行中"
else
    launchctl kickstart -k gui/$(id -u)/com.hermes.dashboard 2>/dev/null && echo "✅ dashboard 已启动(launchd)"
    sleep 2
    ps aux | grep -v grep | grep "dashboard.py" > /dev/null 2>&1 && echo "✅ dashboard 运行中" || echo "⚠️ dashboard 未运行"
fi

echo "--- [4/4] 验证信号文件 ---"
echo "当前信号内容:"
cat "/Users/tangyijun/Desktop/hermes btc/data/claude_signal.jsonl" 2>/dev/null | head -2
echo ""
echo "=========================================="
echo "✅ 完成!等 bot tick(约 3 秒)后会处理信号"
echo "日志: tail -f '/Users/tangyijun/Desktop/hermes btc/data/hermes_bot_launchd.log'"
echo "UI:  http://localhost:5050/"
echo "=========================================="
